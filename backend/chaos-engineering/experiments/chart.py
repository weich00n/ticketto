"""Figure: cumulative tickets confirmed vs seconds after the first buyer (2026-11-05 runs).

    pip install matplotlib && python3 experiments/chart.py results/2026-11-05/chaos-timeline.png

Reads driver/state/runs/<run>/outcomes.jsonl, so it only works where those runs were made.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RUNS = Path(__file__).resolve().parent.parent / "driver" / "state" / "runs"
OUT = Path(sys.argv[1])
STEPS = ("queue", "hold", "order", "pay", "settle")


def ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def confirm_times(run: str) -> list[float]:
    rows = [json.loads(l) for l in (RUNS / run / "outcomes.jsonl").read_text().splitlines() if l]
    t0 = min(ts(r["started_at"]) for r in rows)
    return sorted((ts(r["started_at"]) - t0).total_seconds()
                  + sum(r["steps"].get(s, {}).get("ms", 0) for s in STEPS) / 1000
                  for r in rows if r["outcome"] == "confirmed")


SERIES = [
    ("n-control", "Control (no fault)", "#52514e"),
    ("n-c1", "C1: broker stopped for 3 min", "#2a78d6"),
    ("n-c3", "C3: every reservation task stopped", "#eb6834"),
    ("n-c8", "C8: every checkout API task stopped", "#1baf7a"),
]

INK, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9})
fig, ax = plt.subplots(figsize=(6.8, 3.6), dpi=220)
fig.patch.set_facecolor("white")
ax.set_facecolor("white")

for run, label, colour in SERIES:
    t = confirm_times(run)
    xs = [0.0] + [x for x in t for _ in (0, 1)] + [740]
    ys = [0] + [y for i in range(len(t)) for y in (i, i + 1)] + [len(t)]
    ax.plot(xs, ys, color=colour, linewidth=1.4, solid_capstyle="round", label=label)

def note(xy, xytext, text):
    ax.annotate(text, xy=xy, xytext=xytext, fontsize=7.5, color=INK, va="center",
                arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7, shrinkA=2, shrinkB=2))

note((125, 1250), (136, 1205), "Control: sold out at 125 s")
note((250, 602), (320, 470), "C1: waiting room admits nobody for 199 s\nwhile the broker is down; sold out at 322 s")
note((470, 1160), (330, 815), "C3 / C8: failed orders' holds keep their tickets\noff sale for ~8 min; sold out at 695 s / 704 s")
ax.annotate("", xy=(560, 1067), xytext=(560, 868), arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7, shrinkA=0, shrinkB=2))

ax.set_xlim(0, 740)
ax.set_ylim(0, 1300)
ax.set_xlabel("Seconds after the first buyer", color=MUTED)
ax.set_ylabel("Tickets confirmed", color=MUTED)
ax.set_yticks([0, 250, 500, 750, 1000, 1250])
ax.grid(axis="y", color=GRID, linewidth=0.6)
ax.set_axisbelow(True)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
for side in ("left", "bottom"):
    ax.spines[side].set_color(AXIS)
ax.tick_params(colors=MUTED, length=0)
leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2, frameon=False, fontsize=7.5, handlelength=1.6)
for txt in leg.get_texts():
    txt.set_color(INK)
ax.set_title("Tickets confirmed over time, 2,500 buyers for 1,250 tickets (2026-11-05)",
             loc="left", fontsize=9.5, color=INK, pad=8)
fig.tight_layout()
fig.savefig(OUT, facecolor="white")
print(OUT)
