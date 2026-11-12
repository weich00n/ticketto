"""Drive buyer traffic through the API Gateway for a chaos experiment.

    python -m driver users --count 200          once: test organiser + buyers (Cognito + profiles)
    python -m driver event --run c3             a test event; tickets release in 16 minutes
    python -m driver buy --run c3 --rate 6 --duration 10
                                                S2: buyers/minute for N minutes, while FIS runs
    python -m driver cancel --run c3            S3: the organiser cancels the event (refunds)
    python -m driver report --run c3            the summary again
    python -m driver cleanup                    delete every test account

Environment: GATEWAY_URL, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, AWS credentials
(AWS_PROFILE or similar), optionally STRIPE_SECRET_ID. See env.example.

State (accounts with passwords, event ids, outcomes) lives in driver/state/, which git ignores.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


from . import journey
from .api import Gateway, Step, StepFailed, call
from .aws import Account, Aws
from .report import summarise

STATE = Path(__file__).resolve().parent.parent / "state"
USERS = STATE / "users.json"
# SES is in its sandbox: only verified addresses receive mail (200 a day, 1 a second). Ticket
# emails to anything else fail and log ERROR, which would trip the experiments' abort alarm.
DEFAULT_DELIVERY_EMAIL = "ticketto.alerts@gmail.com"
QUEUE_JOIN_LEAD = timedelta(minutes=15)  # event-service's lead, sent as queue_join_opens_at


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gateway_url() -> str:
    return os.environ["GATEWAY_URL"].rstrip("/")


def load_users() -> dict:
    return json.loads(USERS.read_text()) if USERS.exists() else {"organiser": None, "buyers": []}


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def run_dir(name: str) -> Path:
    return STATE / "runs" / name


def account(data: dict) -> Account:
    return Account(email=data["email"], password=data["password"], name=data["name"])


# ---------- users ----------


def _profile(aws: Aws, acc: Account, organiser: bool = False) -> None:
    """Create the user-service profile (OrderConfirmed is dead-lettered without one) and turn
    off new-event emails, which are on by default and would go to every test account."""
    gw = Gateway(gateway_url(), aws.sign_in(acc))
    step = Step()
    call(gw.session, "POST", gw.url("/api/v1/users/me"), step)
    call(gw.session, "PATCH", gw.url("/api/v1/users/me"), step, json={"notify_new_events": False})
    if organiser:
        call(gw.session, "POST", gw.url("/api/v1/users/me/organiser"), step)


def cmd_users(args: argparse.Namespace) -> int:
    aws = Aws()
    users = load_users()
    lock = threading.Lock()
    if users["organiser"] is None:
        org = Account.new("organiser")
        aws.create(org)
        _profile(aws, org, organiser=True)
        users["organiser"] = org.__dict__
        save_json(USERS, users)
        print(f"organiser {org.email}")

    def make(_: int) -> None:
        acc = Account.new("buyer")
        aws.create(acc)
        try:
            _profile(aws, acc)
        except Exception:
            aws.delete(acc.email)
            raise
        with lock:
            users["buyers"].append(acc.__dict__)
            save_json(USERS, users)

    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:  # Cognito's admin API is rate limited
        for future in [pool.submit(make, i) for i in range(args.count)]:
            try:
                future.result()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                failed += 1
                print(f"  buyer failed: {exc!r}"[:200], file=sys.stderr)
    print(f"{len(users['buyers'])} buyers in {USERS} ({failed} failed this time)")
    return 1 if failed else 0


# ---------- event ----------


def cmd_event(args: argparse.Namespace) -> int:
    aws = Aws()
    users = load_users()
    if users["organiser"] is None:
        raise SystemExit("Run `python -m driver users` first.")
    path = run_dir(args.run) / "event.json"
    if path.exists():
        raise SystemExit(f"{path} exists: pick another --run name.")
    gw = Gateway(gateway_url(), aws.sign_in(account(users["organiser"])))
    release = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=args.release_in)
    step = Step()
    event = call(gw.session, "POST", gw.url("/api/v1/organizer/events"), step, ok=(201,), json={
        "title": f"Chaos test {args.run} {release:%m-%d %H:%M}",
        "description": "Created by the chaos-engineering driver. Not a real event.",
        "venue": "Chaos test venue",
        "starts_at": iso(release + timedelta(days=2)),
        "ticket_release_at": iso(release),
        "ticket_categories": [{"name": "CHAOS", "price_amount": args.price, "currency": "SGD",
                               "total_quantity": args.capacity}],
    }).json()
    call(gw.session, "POST", gw.url(f"/api/v1/organizer/events/{event['event_id']}/publish"), step)
    data = {"event_id": event["event_id"], "category_id": event["ticket_categories"][0]["category_id"],
            "release_at": iso(release), "capacity": args.capacity}
    save_json(path, data)
    print(f"event {data['event_id']} published; tickets release at {data['release_at']} "
          f"({args.release_in} min). Queue opens now.")
    return 0


# ---------- buy ----------


def cmd_buy(args: argparse.Namespace) -> int:
    aws = Aws()
    stripe_key = aws.stripe_key
    folder = run_dir(args.run)
    event = json.loads((folder / "event.json").read_text())
    used_path = folder / "used.txt"
    used = set(used_path.read_text().split()) if used_path.exists() else set()
    buyers = [b for b in load_users()["buyers"] if b["email"] not in used]
    count = int(args.rate * args.duration)
    if count > len(buyers):
        raise SystemExit(f"Need {count} unused buyers for this event, have {len(buyers)}: "
                         f"`python -m driver users --count {count - len(buyers)}`.")

    release = datetime.fromisoformat(event["release_at"].replace("Z", "+00:00"))
    # The waiting room opens QUEUE_JOIN_LEAD before the release; joining sooner is QUEUE_NOT_OPEN.
    start_at = release - QUEUE_JOIN_LEAD if args.join_early else release
    wait = (start_at - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        what = "the waiting room opens" if args.join_early else "tickets release"
        print(f"Waiting {wait / 60:.1f} min until {what} at {iso(start_at)}.")
        time.sleep(wait + 5)

    limits = journey.Limits(queue=args.queue_timeout, settle=args.settle_timeout,
                            ticket=args.ticket_timeout,
                            pay_slots=threading.Semaphore(args.pay_concurrency))
    out_path = folder / "outcomes.jsonl"
    lock = threading.Lock()
    results: list[dict] = []

    # Sign everyone in before the window, so Cognito isn't part of the measured journey.
    # Access tokens last 60 minutes: keep a run (with its queue wait) shorter than that.
    tokens: dict[str, str | Exception] = {}

    def sign_in(buyer: dict) -> None:
        try:
            tokens[buyer["email"]] = aws.sign_in(account(buyer))
        except Exception as exc:  # pylint: disable=broad-exception-caught  # Cognito trouble
            tokens[buyer["email"]] = exc

    print(f"Signing in {count} buyers.")
    with ThreadPoolExecutor(max_workers=16) as pool:  # under Cognito's sign-in rate limit
        list(pool.map(sign_in, buyers[:count]))
    failed_sign_ins = sum(isinstance(t, Exception) for t in tokens.values())
    print(f"Signed in {count - failed_sign_ins}/{count}.")

    def one(buyer: dict) -> None:
        acc = account(buyer)
        token = tokens[acc.email]
        if isinstance(token, Exception):
            res = journey.Result(buyer=acc.email, started_at=iso(datetime.now(timezone.utc)),
                                 outcome="sign_in_failed", detail=repr(token)[:300]).as_dict()
        else:
            res = journey.run(Gateway(gateway_url(), token), stripe_key, event["event_id"],
                              event["category_id"], args.delivery_email, acc.email, limits).as_dict()
        with lock:
            results.append(res)
            with out_path.open("a") as f:
                f.write(json.dumps(res) + "\n")
            done = len(results)
        print(f"[{datetime.now():%H:%M:%S}] {done}/{count} {res['outcome']:<22} {res['detail']}"[:160])

    started = datetime.now(timezone.utc)
    if args.pay_at is not None:
        limits.pay_at = started + timedelta(minutes=args.pay_at)
        print(f"Every buyer holds and orders, then pays at {iso(limits.pay_at)}.")
    print(f"{count} buyers at {args.rate}/min from {started:%H:%M:%S} UTC on event {event['event_id']}")
    interval = 60.0 / args.rate
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        for i, buyer in enumerate(buyers[:count]):
            time.sleep(max(0.0, t0 + i * interval - time.monotonic()))
            with used_path.open("a") as f:
                f.write(buyer["email"] + "\n")
            pool.submit(one, buyer)
    ended = datetime.now(timezone.utc)

    meta_path = folder / "windows.json"
    windows = json.loads(meta_path.read_text()) if meta_path.exists() else []
    windows.append({"started": iso(started), "ended": iso(ended), "buyers": count, "rate": args.rate})
    save_json(meta_path, windows)
    summary = summarise(results)
    (folder / "summary.txt").write_text(summary + "\n")
    print(f"\n{summary}\n\nTraffic window {iso(started)} .. {iso(ended)}. "
          f"Now: python -m checker --event-id {event['event_id']}")
    return 0


# ---------- cancel, report, cleanup ----------


def cmd_cancel(args: argparse.Namespace) -> int:
    aws = Aws()
    event = json.loads((run_dir(args.run) / "event.json").read_text())
    gw = Gateway(gateway_url(), aws.sign_in(account(load_users()["organiser"])))
    try:
        call(gw.session, "POST", gw.url(f"/api/v1/organizer/events/{event['event_id']}/cancel"),
             Step(), json={"reason": args.reason})
    except StepFailed as exc:
        print(f"cancel failed: {exc.status} {exc.tag} {exc.body}")
        return 1
    print(f"event {event['event_id']} cancelled at {iso(datetime.now(timezone.utc))}; "
          "EventCancelled -> RefundRequired -> refunds should follow")
    return 0


def cmd_refunds(args: argparse.Namespace) -> int:
    """C5's check: after a cancel, every paid order has exactly one refund, and it succeeded."""
    aws = Aws()
    lines = (run_dir(args.run) / "outcomes.jsonl").read_text().splitlines()
    paid = [r for r in map(json.loads, filter(None, lines)) if r.get("order_status") == "CONFIRMED"]
    by_email = {b["email"]: b for b in load_users()["buyers"]}

    def one(res: dict) -> tuple[str, str, int, list[str]]:
        gw = Gateway(gateway_url(), aws.sign_in(account(by_email[res["buyer"]])))
        order = call(gw.session, "GET", gw.url(f"/api/v1/orders/{res['order_id']}"), Step(), ok=(200,)).json()
        return res["order_id"], order["status"], len(order["refunds"]), [f["status"] for f in order["refunds"]]

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, paid))
    tally: dict[str, int] = {}
    for _, status, count, statuses in rows:
        key = f"order {status}, {count} refund(s) {'/'.join(statuses) or '-'}"
        tally[key] = tally.get(key, 0) + 1
    for key, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"{n:5}  {key}")
    good = sum(1 for _, _, count, statuses in rows if count == 1 and statuses == ["SUCCEEDED"])
    print(f"{'PASS' if good == len(rows) else 'FAIL'}: {good}/{len(rows)} paid orders have exactly one succeeded refund")
    return 0 if good == len(rows) else 1


def cmd_report(args: argparse.Namespace) -> int:
    path = run_dir(args.run) / "outcomes.jsonl"
    print(summarise([json.loads(line) for line in path.read_text().splitlines() if line]))
    return 0


def cmd_cleanup(_args: argparse.Namespace) -> int:
    aws = Aws()
    users = load_users()
    accounts = [u for u in [users["organiser"], *users["buyers"]] if u]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda u: aws.delete(u["email"]), accounts))
    USERS.unlink(missing_ok=True)
    print(f"deleted {len(accounts)} Cognito accounts. Their user-service profiles stay (no delete "
          "endpoint); test events stay too, so cancel any still on sale with `cancel --run`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="driver", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("users", help="create the test organiser and buyers")
    p.add_argument("--count", type=int, default=100, help="buyers to add (default 100)")
    p.add_argument("--workers", type=int, default=4, help="accounts made at once (default 4)")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("event", help="create and publish a test event")
    p.add_argument("--run", required=True, help="name for this experiment run, e.g. c3")
    p.add_argument("--capacity", type=int, default=2000, help="tickets (default 2000; pool = 1/5)")
    p.add_argument("--price", type=int, default=1000, help="cents (default 1000 = SGD 10)")
    p.add_argument("--release-in", type=int, default=16, help="minutes, at least 15 (default 16)")
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("buy", help="S2: buyers through the whole purchase")
    p.add_argument("--run", required=True)
    p.add_argument("--rate", type=float, default=6, help="buyers per minute (default 6)")
    p.add_argument("--duration", type=float, default=10, help="minutes of arrivals (default 10)")
    p.add_argument("--delivery-email", default=DEFAULT_DELIVERY_EMAIL)
    p.add_argument("--join-early", action="store_true", help="queue as soon as the waiting room opens (15 min before release)")
    p.add_argument("--queue-timeout", type=float, default=900)
    p.add_argument("--settle-timeout", type=float, default=600)
    p.add_argument("--ticket-timeout", type=float, default=300)
    p.add_argument("--pay-concurrency", type=int, default=10,
                   help="Stripe calls at once (default 10; test mode allows about 25 a second)")
    p.add_argument("--pay-at", type=float, help="minutes after the first buyer: everyone pays then "
                   "(C3: stop reservation just before, so confirmations hit the outage; holds last 6 min)")
    p.set_defaults(func=cmd_buy)

    p = sub.add_parser("cancel", help="S3: cancel the run's event")
    p.add_argument("--run", required=True)
    p.add_argument("--reason", default="Chaos test S3: cancelled by the driver.")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("refunds", help="C5: check every paid order of a cancelled run got one refund")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_refunds)

    p = sub.add_parser("report", help="print a run's summary")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("cleanup", help="delete every test Cognito account")
    p.set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    # One connection per buyer: a few thousand buyers need more than macOS's default 256 files.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, max(soft, 10240)), hard))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
