"""Images: the per-market alert chart and the daily digest table. Black on white, red only for risk."""
import glob
import logging
import os
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Wedge  # noqa: E402

import core  # noqa: E402

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
K, G1, G2, G3, R = "#111111", "#374151", "#d1d5db", "#f3f4f6", "#dc2626"
FONT_DIRS = ["~/Library/Fonts", "~/.local/share/fonts", "/usr/share/fonts", "/usr/local/share/fonts"]


def setup_font():
    """Use config 'font' (or env RATE_BOT_FONT) if installed, else DejaVu Sans."""
    name = os.environ.get("RATE_BOT_FONT") or core.config().get("font", "DejaVu Sans")
    for d in FONT_DIRS:
        for f in glob.glob(os.path.expanduser(f"{d}/**/{name.replace(' ', '')}*.[ot]tf"), recursive=True):
            try:
                font_manager.fontManager.addfont(f)
            except Exception:  # noqa: BLE001
                pass
    try:
        font_manager.findfont(font_manager.FontProperties(family=name), fallback_to_default=False)
    except Exception:  # noqa: BLE001
        name = "DejaVu Sans"
    plt.rcParams.update({"font.size": 12, "font.family": name, "text.color": K,
                         "axes.labelcolor": K, "xtick.color": K, "ytick.color": K})


def fmt(v):
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:,.0f}"


def stamp():
    return f"{datetime.now(core.tz()):%Y-%m-%d %H:%M} ({core.tz_label()})"


def alert(*, title, subtitle, t, rate, util_now, kink, kink_label, headroom, breakeven, breakeven_label,
          supplied, borrowed, source, out, gauge_min=0, warn=20000, headroom_full=60000):
    """Left: borrow rate over 7 days with the reference yield. Right: utilization gauge and headroom
    (how much can be withdrawn before utilization passes the kink)."""
    setup_font()
    unit = core.config()["asset"]["symbol"]
    rate = np.asarray(rate)
    fig = plt.figure(figsize=(14, 6.2), dpi=160)
    fig.text(0.045, 0.94, title, fontsize=18, fontweight="bold")
    fig.text(0.045, 0.885, subtitle, fontsize=12.5, color=G1)

    a = fig.add_axes([0.065, 0.14, 0.47, 0.62])
    ycap = max(4.0, breakeven * 2.2)
    clipped = rate.max() > ycap
    a.plot(t, np.minimum(rate, ycap), color=K, lw=1.8, clip_on=False)
    a.scatter(t[-1], rate[-1], color=K, s=40, zorder=5)
    a.axhline(breakeven, color=R, ls="--", lw=1.2)
    a.text(1.015, breakeven, breakeven_label, color=R, fontweight="bold", va="center", fontsize=10.5,
           transform=a.get_yaxis_transform(), clip_on=False)
    i = int(rate.argmax())
    if clipped:
        a.annotate(f"peak {rate[i]:.2f}% · {t[i]:%b %d %H:%M} (off scale)", (t[i], ycap), xytext=(0, 6),
                   textcoords="offset points", fontsize=10.5, fontweight="bold", va="bottom", ha="center",
                   annotation_clip=False)
    elif rate[i] > rate[-1] * 1.3:
        a.annotate(f"peak {rate[i]:.2f}%\n{t[i]:%b %d %H:%M}", (t[i], rate[i]), xytext=(10, 4),
                   textcoords="offset points", fontsize=10.5, fontweight="bold")
    a.annotate(f"now {rate[-1]:.2f}%", (t[-1], rate[-1]), xytext=(0, -16), textcoords="offset points",
               fontweight="bold", ha="right", va="top")
    a.set_ylim(0, ycap if clipped else max(3.0, rate.max() * 1.25))
    a.set_ylabel("Borrow rate (%)")
    a.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b %d"))
    a.set_title("Borrow rate · last 7 days", loc="left", fontsize=12.5, fontweight="bold", pad=28)
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        a.spines[s].set_color(G2)

    b = fig.add_axes([0.69, 0.42, 0.26, 0.38])
    b.axis("off")
    b.set_xlim(-1.3, 1.3)
    b.set_ylim(-0.35, 1.3)
    b.set_aspect("equal")
    lo, hi, kp = gauge_min, 100, kink
    ang = lambda v: 180 - (v - lo) / (hi - lo) * 180  # noqa: E731
    b.add_patch(Wedge((0, 0), 1, ang(kp), ang(lo), width=0.2, color=G2))
    b.add_patch(Wedge((0, 0), 1, ang(hi), ang(kp), width=0.2, color=R, alpha=.85))
    b.add_patch(Wedge((0, 0), 1, ang(min(max(util_now, lo), hi)), ang(lo), width=0.2, color=K))
    th = np.radians(ang(kp))
    b.text(1.18 * np.cos(th), 1.14 * np.sin(th), f"{kink_label} {kp:.0f}%", color=R, fontsize=10.5,
           ha="left" if kp >= 80 else "center", fontweight="bold")
    b.text(-0.9, -0.2, f"{lo}%", fontsize=10, ha="center")
    b.text(0.9, -0.2, "100%", fontsize=10, ha="center")
    b.text(0, 0.24, f"{util_now:.1f}%", ha="center", fontsize=26, fontweight="bold")
    b.text(0, 0.03, "pool utilization", ha="center", fontsize=11)
    fig.text(0.82, 0.84, "Utilization now", ha="center", fontsize=12.5, fontweight="bold")

    c = fig.add_axes([0.665, 0.14, 0.29, 0.20])
    c.axis("off")
    c.set_xlim(0, 1)
    c.set_ylim(0, 1)
    warn = min(warn, 0.10 * supplied)              # scale to the pool: a fixed threshold is always red on a small pool
    headroom_full = min(headroom_full, max(0.30 * supplied, warn * 1.5))
    f = min(headroom / headroom_full, 1) if headroom_full else 0
    col = R if headroom < warn else K
    c.text(0, 1.18, "Headroom before rates spike", fontsize=12.5, fontweight="bold")
    c.text(0, 0.80, f"{headroom:,.0f} {unit}" + ("+" if headroom >= headroom_full else ""), fontsize=20,
           fontweight="bold", color=col, va="center")
    c.add_patch(plt.Rectangle((0, 0.40), 1, 0.16, fc=G3, ec=G2, lw=0.8))
    c.add_patch(plt.Rectangle((0, 0.40), f, 0.16, fc=col))
    k = lambda v: f"{v / 1000:.0f}k" if v >= 1000 else f"{v:,.0f}"  # noqa: E731
    if headroom_full:
        c.plot([warn / headroom_full] * 2, [0.36, 0.60], color=R, lw=1.2)
        c.text(warn / headroom_full, 0.27, k(warn), fontsize=9.5, color=R, ha="center")
    c.text(0, 0.27, "0", fontsize=9.5)
    c.text(1, 0.27, k(headroom_full) + "+", fontsize=9.5, ha="right")
    c.text(0, 0.10, f"Withdrawals of this size push utilization past the {kink_label}.\n"
                    f"Pool: {supplied:,.0f} supplied · {borrowed:,.0f} borrowed ({unit})",
           fontsize=10, color=G1, va="top")
    fig.text(0.045, 0.02, f"Source: {source} · {stamp()}", fontsize=9, color=G1)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out


def _table(fig, top, title, cols, rows, bar_x, bar_max, k):
    fig.text(0.04, top, title, fontsize=13.5, fontweight="bold", va="top")
    y = top - 0.05 * k
    for h, x, ha in cols:
        fig.text(x, y, h, fontsize=9.5, color=G1, ha=ha, va="center")
    fig.add_artist(plt.Line2D([0.04, 0.96], [y - 0.018 * k] * 2, color=G2, lw=0.8))
    rh = 0.038 * k
    for i, (cells, bar) in enumerate(rows):
        yy = y - 0.045 * k - i * rh
        if i % 2 == 0:
            fig.patches.append(plt.Rectangle((0.04, yy - rh / 2), 0.92, rh, transform=fig.transFigure,
                                             fc=G3, ec="none", zorder=0))
        for (txt, bold, col), (_, x, ha) in zip(cells, cols):
            fig.text(x, yy, txt, fontsize=11.5, fontweight="bold" if bold else "normal", color=col, ha=ha, va="center")
        fig.patches.append(plt.Rectangle((bar_x, yy - 0.011 * k), 0.11 * max(bar, 0) / (bar_max or 1), 0.022 * k,
                                         transform=fig.transFigure, fc=K, ec="none"))
    return y - 0.045 * k - len(rows) * rh


def digest(d, sup, loops, out):
    """Top supply venues and top loops as two tables."""
    setup_font()
    unit = core.config()["asset"]["symbol"]
    day = datetime.fromtimestamp(d["ts"], core.tz())
    H = 8.2
    k = 12 / H
    fig = plt.figure(figsize=(13, H), dpi=160)
    fig.text(0.04, 1 - 0.05 * k, f"{unit} opportunities · {day:%b %d, %Y}", fontsize=19, fontweight="bold", va="top")
    ys = " · ".join(f"{n} {v:.2f}%" for n, v in d["yields"].items())
    fig.text(0.04, 1 - 0.105 * k, f"Reference yields today (7d): {ys}", fontsize=12.5, color=G1, va="top")
    cols = [("SUPPLY VENUE", 0.05, "left"), ("7d APY", 0.42, "right"), ("", 0.435, "left"),
            ("NOW", 0.66, "right"), ("TVL", 0.80, "right"), ("LIQUIDITY", 0.95, "right")]
    rows = [([(f"{s['name']}  ·  {s['proto']}", True, K), (f"{s['apy7']:.2f}%", True, K), ("", False, K),
              (f"{s['now']:.2f}%" if s["now"] is not None else "–", False, G1), (fmt(s["tvl"]), False, K),
              (fmt(s["withdrawable"]) if s["withdrawable"] is not None else "–", False, K)], s["apy7"]) for s in sup]
    y = _table(fig, 1 - 0.16 * k, f"Supply {unit} · top {len(sup)}", cols, rows, 0.435,
               max((s["apy7"] for s in sup), default=1), k)
    if loops:
        cols2 = [(f"LOOP (BORROW {unit} · COLLATERAL)", 0.05, "left"), ("PROFIT / YR", 0.42, "right"),
                 ("", 0.435, "left"), ("BEST SIZE", 0.66, "right"), ("RATE AFTER", 0.80, "right"),
                 ("UTILIZATION", 0.95, "right")]
        rows2 = [([(f"{l['name']} · {l['coll']}", True, K), (f"{l['profit']:,.0f} {unit}", True, K), ("", False, K),
                   (fmt(l["best"]), False, K), (f"{l['best_rate']:.2f}%", False, G1),
                   (f"{l['u'] * 100:.1f}%", False, R if l["near_kink"] else G1)], l["profit"])
                 for l in loops]
        _table(fig, y - 0.03 * k, f"Loop {unit} · top {len(loops)}  (borrow {unit} against the reference asset)",
               cols2, rows2, 0.435, max(l["profit"] for l in loops), k)
    fig.text(0.04, 0.02, f"7d APY = share-price change over 7 days, after fees. TVL = total deposits. Liquidity = {unit} "
             "withdrawable now (all users).\nBest size = extra borrow that maximises profit (the new rate applies to the "
             f"whole loan). Rate after = borrow rate at best size.\nSource: on-chain ({core.config()['chain']['name']}), "
             f"Morpho API · {stamp()}", fontsize=8.5, color=G1, linespacing=1.4)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out
