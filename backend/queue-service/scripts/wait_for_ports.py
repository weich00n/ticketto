"""Block until every host:port accepts TCP connections (used by CI before tests).

python -m scripts.wait_for_ports mysql:3306 redis:6379 rabbitmq:5672 --timeout 120
"""

from __future__ import annotations

import argparse
import socket
import sys
import time


def wait_for(host: str, port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            time.sleep(1)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("addresses", nargs="+", metavar="HOST:PORT")
    parser.add_argument("--timeout", type=float, default=120, help="seconds, for all addresses")
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.timeout
    for address in args.addresses:
        host, _, port = address.rpartition(":")
        if not wait_for(host, int(port), deadline):
            print(f"timed out waiting for {address}", file=sys.stderr)
            return 1
        print(f"{address} is up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
