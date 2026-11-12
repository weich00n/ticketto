"""Summarise a run's outcomes.jsonl into the numbers RESULTS.md asks for."""

from __future__ import annotations

from collections import Counter


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def summarise(results: list[dict]) -> str:
    total = len(results)
    if not total:
        return "No buyers finished."
    outcomes = Counter(r["outcome"] for r in results)
    lines = [f"{total} buyers"]
    for outcome, n in outcomes.most_common():
        lines.append(f"  {outcome:<24} {n:>5}  {100 * n / total:5.1f}%")
    confirmed = outcomes.get("confirmed", 0)
    lines.append(f"success rate (confirmed with ticket): {100 * confirmed / total:.1f}%")

    details = Counter(f"{r['outcome']}: {r['detail']}" for r in results if r.get("detail"))
    if details:
        lines.append("failure details")
        lines += [f"  {n:>5}  {d}" for d, n in details.most_common(10)]

    lines.append(f"{'step':<8} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'buyers w/ errors':>17} {'errors':>7}")
    steps = ["queue", "hold", "order", "pay", "settle", "ticket"]
    for step in steps:
        rows = [r["steps"][step] for r in results if step in r["steps"]]
        if not rows:
            continue
        ms = [row["ms"] for row in rows]
        hit = sum(1 for row in rows if row["errors"])
        errors = sum(len(row["errors"]) for row in rows)
        lines.append(f"{step:<8} {percentile(ms, 50):>8} {percentile(ms, 95):>8} {max(ms):>8} "
                     f"{hit:>17} {errors:>7}")

    kinds = Counter(e for r in results for s in r["steps"].values() for e in s["errors"])
    if kinds:
        lines.append("errors seen (retried)")
        lines += [f"  {n:>5}  {k}" for k, n in kinds.most_common(10)]
    return "\n".join(lines)
