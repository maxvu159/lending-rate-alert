"""Borrow rate alert. Run every 10 minutes from cron.

Rules (one alert type, either condition fires it):
  * Spike: borrow rate >= 24h median + spike_pts. Fires even while still below the reference yield
    (a vault at 0.2% jumping to 1.2% is news for loopers even if the LST pays 2%).
  * Above yield: borrow rate > the reference yield of the market's collateral, held for hold_min minutes.
  * Re-alert when the rate rises another escalate_pts above the last alert.
  * Back to normal: below the reference yield AND below median + spike_pts/2, one message.
Each alert is its own message with its own image (see notify.py).

python bot/alert.py [--dry-run] [--force <market-key> [--demo-rate 12.5]]
  --dry-run : do not write state
  --force   : treat this market as spiking, to preview the message and image (no state written)
"""
import argparse
import json
import statistics
import sys
import time
import traceback
from datetime import datetime

import charts
import core
import markets as mk
import notify

STATE, HIST, OUT = core.DATA / "state.json", core.DATA / "history.jsonl", core.DATA / "out"
YIELD_TTL = 3600                  # reference yields: recompute at most once an hour


def rules():
    a = core.config().get("alerts", {})
    return a.get("spike_pts", 1.0), a.get("escalate_pts", 1.0)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"markets": {}, "yields": {}}


def fmt_dur(sec):
    h = sec / 3600
    return f"{h:.0f}h" if h >= 1 else f"{sec / 60:.0f}m"


def ts_local(t):
    return datetime.fromtimestamp(t, core.tz()).strftime("%b %d %H:%M")


def own_history(key):
    pts = []
    if HIST.exists():
        for line in HIST.read_text().splitlines()[-20000:]:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("m") == key:
                pts.append((d["t"], d["r"]))
    return pts


def median24(m, snap, now):
    """Median borrow rate over the previous 24h. The bot's own samples first; an API while they are too few."""
    pts = [r for t, r in own_history(m["key"]) if now - 86400 <= t < now - 1]
    if len(pts) < 6 and m["kind"] != "euler":
        cut = datetime.fromtimestamp(now - 86400, core.tz()).replace(tzinfo=None)
        pts = [r for t, r in mk.history(m, snap, [])[:-1] if t >= cut]
    return statistics.median(pts) if pts else snap["rate"]


def source(m):
    if m.get("history") == "hyperlend_api":
        return "HyperLend API + on-chain"
    return {"aave": "on-chain · bot's own 10-min samples", "morpho": "Morpho API",
            "euler": "on-chain · bot's own 10-min samples"}[m["kind"]]


def image(m, snap, y, med, above):
    pts = mk.history(m, snap, own_history(m["key"]))
    if len(pts) < 3:
        return None                   # not enough samples yet for a chart
    ref, r = m["yield"], snap["rate"]
    headroom = max(snap["supplied"] - snap["borrowed"] / (snap["kink"] / 100), 0)
    sub = (f"Borrow rate {r:.2f}% is above the {ref} yield ({y:.2f}%): looping costs more than it earns" if above
           else f"Borrow rate jumped to {r:.2f}% ({r - med:+.1f} pts vs 24h median {med:.2f}%), still below the {ref} yield")
    return charts.alert(title=m["name"], subtitle=sub, t=[p[0] for p in pts], rate=[p[1] for p in pts],
                        util_now=snap["util"], kink=snap["kink"], kink_label=snap["kink_label"], headroom=headroom,
                        breakeven=y, breakeven_label=f"{ref} yield {y:.2f}%\n(7d)", supplied=snap["supplied"],
                        borrowed=snap["borrowed"], source=source(m), out=OUT / f"alert-{m['key']}.png",
                        gauge_min=m.get("gauge_min", 0))


def message(kind, m, snap, y, med, above, spike, st, now):
    ref, r = m["yield"], snap["rate"]
    if kind == "normal":
        return "\n".join([f"**Back to normal · {m['name']}**", ts_local(now), "",
                          "**Borrow rate**", f"{r:.2f}% · 24h median {med:.2f}%", "",
                          f"**vs {ref} yield**", f"{y:.2f}% · borrow is below", "",
                          "**Alert lasted**", fmt_dur(now - st["alert_since"])])
    title = "Borrow rate rising" if kind == "rising" else ("Borrow rate spike" if spike else "Borrow rate above yield")
    rate_line = f"{r:.2f}% · 24h median {med:.2f}%"
    if kind == "rising":
        rate_line += f" · {st['last_rate']:.2f}% at last alert"
    vs = (f"{y:.2f}% · above since {ts_local(st['above_since'])} ({fmt_dur(now - st['above_since'])})" if above
          else f"{y:.2f}% · borrow still {y - r:.2f} pts below")
    tail = (f"Borrowing to loop {ref} now costs more than it earns." if above
            else "Loops still earn, but the margin is shrinking.")
    return "\n".join([f"**{title} · {m['name']}**", ts_local(now), "", "**Borrow rate**", rate_line, "",
                      f"**vs {ref} yield**", vs, "", "**Utilization**",
                      f"{snap['util']:.1f}% · {snap['kink_label']} {snap['kink']:.0f}%", "", tail])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", default="", help="market key: treat as spiking (preview message + image, no state)")
    ap.add_argument("--demo-rate", type=float, default=None, help="with --force: replace the current borrow rate")
    a = ap.parse_args()
    spike_pts, escalate_pts = rules()
    now = time.time()
    core.DATA.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if now - state.get("yields", {}).get("t", 0) > YIELD_TTL:
        state["yields"] = {"t": now, **core.yields()}
    msgs, errors, hist_lines = [], [], []
    for m in core.config()["markets"]:
        try:
            snap = mk.snapshot(m)
            med = median24(m, snap, now)
            if a.force == m["key"] and a.demo_rate is not None:
                snap["rate"] = a.demo_rate
        except Exception as e:  # noqa: BLE001  one failing market must not block the others
            errors.append(f"{m['key']}: {e}")
            continue
        hist_lines.append(json.dumps({"t": int(now), "m": m["key"], "r": round(snap["rate"], 4),
                                      "u": round(snap["util"], 3)}))
        y, r = state["yields"][m["yield"]], snap["rate"]
        st = state["markets"].setdefault(m["key"], {})
        for k in ("above_since", "last_rate", "alert_since"):
            st.setdefault(k, None)
        st.setdefault("alerted", False)
        above = r > y
        if not above:
            st["above_since"] = None
        elif st["above_since"] is None:
            st["above_since"] = now
        held = above and now - st["above_since"] >= m.get("hold_min", 30) * 60
        spike = r >= med + spike_pts or a.force == m["key"]
        kind = None
        if held or spike:
            if not st["alerted"]:
                kind = "first"
            elif r >= st["last_rate"] + escalate_pts:
                kind = "rising"
        elif st["alerted"] and not above and r < med + spike_pts / 2:
            kind = "normal"
        if not kind:
            continue
        text = message(kind, m, snap, y, med, above, spike, st, now)
        if a.force == m["key"]:
            text = "**TEST** · simulated numbers\n" + text
        img = None
        if kind != "normal":
            try:
                img = image(m, snap, y, med, above)
            except Exception:  # noqa: BLE001
                errors.append(f"{m['key']} image: {traceback.format_exc(limit=1).strip()}")
        msgs.append(text + (f"\nMEDIA:{img}" if img else ""))
        if kind == "normal":
            st.update(alerted=False, last_rate=None, alert_since=None)
        else:
            st.update(alerted=True, last_rate=r)
            if kind == "first":
                st["alert_since"] = now
    if not (a.dry_run or a.force):                   # --force is a preview, it must not touch state
        STATE.write_text(json.dumps(state, indent=1))
        with HIST.open("a") as f:
            f.write("".join(line + "\n" for line in hist_lines))
    if errors:                                       # errors go to stderr (cron log), not to the group
        print("\n".join(errors), file=sys.stderr)
    if msgs:
        notify.send_all(msgs)


if __name__ == "__main__":
    main()
