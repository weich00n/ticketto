"""Confirmations per 10 s after the first buyer, and the longest gap with none, for each run given.

    python3 experiments/timeline.py n-control n-c7
"""
import json
import sys
from datetime import datetime
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "driver" / "state" / "runs"
STEPS = ("queue", "hold", "order", "pay", "settle")


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


for run in sys.argv[1:]:
    rows = [json.loads(line) for line in (RUNS / run / "outcomes.jsonl").read_text().splitlines() if line]
    t0 = min(ts(r["started_at"]) for r in rows)
    done = sorted((ts(r["started_at"]) - t0).total_seconds()
                  + sum(r["steps"].get(s, {}).get("ms", 0) for s in STEPS) / 1000
                  for r in rows if r["outcome"] == "confirmed")
    buckets: dict[int, int] = {}
    for t in done:
        buckets[int(t // 10) * 10] = buckets.get(int(t // 10) * 10, 0) + 1
    gap = max((b - a, a) for a, b in zip(done, done[1:])) if len(done) > 1 else (0, 0)
    print(f"{run}: {len(done)} confirmed, last at {done[-1]:.0f} s, longest gap {gap[0]:.0f} s (from {gap[1]:.0f} s)")
    print("  " + " ".join(f"{k}:{v}" for k, v in sorted(buckets.items())))
