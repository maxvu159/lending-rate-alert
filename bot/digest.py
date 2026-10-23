"""Daily digest: where to supply the asset and where to loop it. One image (two tables) plus a short caption.

Supply: ERC-4626 vaults and Aave markets, ranked by 7-day APY from share-price (or liquidity index) growth,
after fees. Loops: borrow the asset against a reference asset; profit per year at the best extra size, where the
new rate applies to the whole loan. Market data only, no positions.

python bot/digest.py [--no-image] [--test] [--json out.json]
"""
import argparse
import json
import math
import time

import charts
import core
import markets as mk
import notify

SY = 365 * 86400


def cfg():
    return core.config().get("digest", {})


def collect():
    head, old, secs = core.week_window()
    d = core.decimals()
    yields = core.yields((head, old, secs))
    asset = mk.asset()
    mv = core.gql('{ vaults(first:100, where:{chainId_in:[%d], assetAddress_in:["%s"]}){ items{ address '
                  'state{ netApy } liquidity{ underlying } } } }' % (mk.chain_id(), asset))["vaults"]["items"]
    mv = {v["address"].lower(): v for v in mv}
    supply = []
    for v in cfg().get("supply_vaults", []):
        addr = v["vault"]
        tvl = core.call(addr, "totalAssets()") / d
        pps = lambda b, addr=addr: core.call(addr, "convertToAssets(uint256)", 10 ** 18, block=b)  # noqa: E731
        row = dict(name=v["name"], proto=v.get("protocol", ""), tvl=tvl, apy7=core.annualise(pps(head), pps(old), secs),
                   now=None, withdrawable=None)
        if addr.lower() in mv:
            row["now"] = mv[addr.lower()]["state"]["netApy"] * 100
            row["withdrawable"] = int(mv[addr.lower()]["liquidity"]["underlying"]) / d
        elif v.get("protocol", "").lower() == "euler":
            tb = core.call(addr, "totalBorrows()") / d
            r = core.call(addr, "interestRate()")
            fee = core.call(addr, "interestFee()") / 1e4
            row["now"] = (math.exp(r / mk.RAY * SY) - 1) * (tb / tvl if tvl else 0) * (1 - fee) * 100
            row["withdrawable"] = core.call(addr, "cash()") / d
        supply.append(row)
    markets = core.config()["markets"]
    for m in (m for m in markets if m["kind"] == "aave"):
        pool = mk.aave_pool(m["atoken"])
        idx = lambda b: int(core.words(core.call_raw(pool, "getReserveData(address)", asset, block=b))[1], 16)  # noqa: E731
        s = mk.snapshot(m)
        supply.append(dict(name=m["name"], proto="Aave fork", tvl=s["supplied"],
                           apy7=core.annualise(idx(head), idx(old), secs), now=s["supply_rate"],
                           withdrawable=s["supplied"] - s["borrowed"]))
    plans = [(m, c) for m in markets for c in m.get("loop_collateral", [m["yield"]])]
    plans += [(dict(x, key=x["name"]), x["collateral"]) for x in cfg().get("extra_loops", [])]
    loops, snaps = [], {}
    for m, coll in plans:
        k = m.get("key")
        snaps[k] = snaps.get(k) or mk.snapshot(m)
        s, y = snaps[k], yields[coll]
        f, S, B = s["curve"], s["supplied"], s["borrowed"]
        if not S:
            continue
        lo, hi = B, S                 # borrow more until the rate meets the reference yield, or liquidity runs out
        if f(lo / S) >= y:
            room = 0.0
        else:
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if f(mid / S) < y else (lo, mid)
            room = lo - B
        step = max(25.0, room / 2000)  # best size: the new rate applies to the whole loan, so all of room earns 0
        best = max((i * step for i in range(int(room / step) + 1)), key=lambda x: x * (y - f((B + x) / S)), default=0.0)
        best_rate = f((B + best) / S)
        u0 = B / S
        loops.append(dict(name=m["name"], coll=coll, now=s["rate"], ref=y, spread=y - s["rate"], room=room, best=best,
                          best_rate=best_rate, profit=best * (y - best_rate) / 100, liquidity=S - B, u=u0,
                          near_kink=s["kink_label"] == "kink" and u0 > 0.75))
    return dict(ts=int(time.time()), yields=yields, supply=supply, loops=loops)


def top3(d):
    sup = sorted([s for s in d["supply"] if s["tvl"] >= cfg().get("min_tvl", 0)], key=lambda s: -s["apy7"])[:3]
    seen, loops = set(), []
    for lp in sorted(d["loops"], key=lambda lp: -lp["profit"]):   # one row per market: keep the more profitable one
        if lp["name"] not in seen and lp["profit"] > 0:
            seen.add(lp["name"])
            loops.append(lp)
    return sup, loops[:3]


def picks(d):
    c = cfg()
    sup = [s for s in d["supply"] if s["tvl"] >= c.get("min_tvl", 0)]
    best = max((s for s in sup if s["tvl"] >= c.get("best_min_tvl", 0)), key=lambda s: s["apy7"], default=None)
    safe = max((s for s in sup if s["tvl"] >= c.get("safe_min_tvl", 0)
                and (s["withdrawable"] or 0) >= c.get("safe_min_withdrawable", 0)), key=lambda s: s["apy7"], default=None)
    return best, safe


def caption(d):
    best, safe = picks(d)
    _, loops = top3(d)
    unit = core.config()["asset"]["symbol"]
    from datetime import datetime
    day = datetime.fromtimestamp(d["ts"], core.tz())
    lines = [f"**{unit} opportunities · {day:%b %d}**"]
    if best:
        lines += ["", "**Best supply**", f"{best['name']} · {best['apy7']:.2f}%"]
    if safe:
        lines += ["", "**Safest supply**", f"{safe['name']} · {safe['apy7']:.2f}%"]
    if loops:
        lp = loops[0]
        lines += ["", "**Best loop**", f"{lp['name']} {lp['coll']} · +{lp['profit']:,.0f} {unit}/yr"]
        if lp["near_kink"]:
            lines.append(f"Pool near kink (U {lp['u'] * 100:.1f}%)")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-image", action="store_true")
    ap.add_argument("--test", action="store_true", help="prefix the message with a bold TEST label")
    ap.add_argument("--json", help="also write the collected numbers to this file")
    a = ap.parse_args()
    d = collect()
    if a.json:
        open(a.json, "w").write(json.dumps(d, indent=1))
    text = ("**TEST**\n" if a.test else "") + caption(d)
    if not a.no_image:
        sup, loops = top3(d)
        text += f"\nMEDIA:{charts.digest(d, sup, loops, core.DATA / 'out' / 'digest.png')}"
    notify.send_all([text])
