"""Market adapters. Each kind returns the same snapshot and a rate curve, so alerts and loop maths are generic.

  aave   : Aave v3 and forks. Rate read on-chain (getReserveData), curve from the reserve's rate strategy
           (getInterestRateDataBps, v3.2+), or from "curve" in the market config for older strategies.
  morpho : Morpho Blue market via the Morpho API. Curve: adaptive-curve IRM shape around the 90% target.
  euler  : Euler v2 vault with a kink IRM, all on-chain.

snapshot(m) -> dict(rate %, util %, supplied, borrowed, kink %, kink_label, curve(u) -> rate %, key?)
"""
import math
import time
from datetime import datetime
from functools import lru_cache

import core

SY = 365 * 86400
RAY = 1e27


def asset():
    return core.config()["asset"]["address"]


def chain_id():
    return core.config()["chain"]["morpho_chain_id"]


# ── aave ──
@lru_cache(maxsize=None)
def aave_pool(atoken):
    return core.to_address(core.call_raw(atoken, "POOL()"))


def aave_reserve(m):
    w = core.words(core.call_raw(aave_pool(m["atoken"]), "getReserveData(address)", asset()))
    # v3 layout: 2 liquidity rate, 4 variable borrow rate (ray, APR), 8 aToken, 10 variable debt, 11 strategy
    return dict(supply_rate=int(w[2], 16) / RAY * 100, borrow_rate=int(w[4], 16) / RAY * 100,
                debt=core.to_address(w[10]), strategy=core.to_address(w[11]))


def aave_curve(m, strategy):
    """Piecewise-linear Aave curve as (optimal, base, slope1, slope2), fractions and percent."""
    try:
        w = core.words(core.call_raw(strategy, "getInterestRateDataBps(address)", asset()))
        opt, base, s1, s2 = (int(x, 16) for x in w[:4])
        return opt / 1e4, base / 100, s1 / 100, s2 / 100
    except Exception:
        c = m.get("curve")
        if not c:
            raise RuntimeError(f"{m['key']}: rate strategy has no getInterestRateDataBps; set 'curve' "
                               "{optimal, base, slope1, slope2} in the market config")
        return c["optimal"], c["base"], c["slope1"], c["slope2"]


def aave_rate_at(u, opt, base, s1, s2):
    return base + s1 * u / opt if u <= opt else base + s1 + s2 * (u - opt) / (1 - opt)


# ── morpho ──
@lru_cache(maxsize=None)
def morpho_key(prefix):
    if len(prefix) == 66:
        return prefix
    items = core.gql('{ markets(first:100, where:{chainId_in:[%d], loanAssetAddress_in:["%s"]}){ items{ marketId } } }'
                     % (chain_id(), asset()))["markets"]["items"]
    hit = [x["marketId"] for x in items if x["marketId"].startswith(prefix.lower())]
    if len(hit) != 1:
        raise RuntimeError(f"Morpho market prefix {prefix}: {len(hit)} matches")
    return hit[0]


def morpho_curve(u):
    """Adaptive-curve IRM shape: rate = rate_at_target * curve(u), target utilization 90%."""
    return 1 - 0.75 * (0.9 - u) / 0.9 if u <= 0.9 else 1 + 3 * (u - 0.9) / 0.1


# ── euler ──
def euler_irm(vault):
    irm = core.to_address(core.call(vault, "interestRateModel()"))
    return tuple(core.call(irm, n + "()") for n in ("baseRate", "slope1", "slope2", "kink"))


def euler_rate_at(u, base, s1, s2, kink):
    ui = u * 2 ** 32
    spy = base + s1 * min(ui, kink) + (s2 * (ui - kink) if ui > kink else 0)
    return (math.exp(spy / RAY * SY) - 1) * 100


# ── snapshot ──
def snapshot(m):
    d = core.decimals()
    if m["kind"] == "aave":
        r = aave_reserve(m)
        S = core.call(m["atoken"], "totalSupply()") / d
        B = core.call(r["debt"], "totalSupply()") / d
        opt, base, s1, s2 = aave_curve(m, r["strategy"])
        return dict(rate=r["borrow_rate"], supply_rate=r["supply_rate"], util=B / S * 100 if S else 0,
                    supplied=S, borrowed=B, kink=opt * 100, kink_label="kink",
                    curve=lambda u: aave_rate_at(u, opt, base, s1, s2))
    if m["kind"] == "morpho":
        key = morpho_key(m["market"])
        st = core.gql('{ markets(first:1, where:{chainId_in:[%d], uniqueKey_in:["%s"]}){ items{ state{ utilization '
                      'borrowApy supplyAssets borrowAssets } } } }' % (chain_id(), key))["markets"]["items"][0]["state"]
        u0, rate = st["utilization"], st["borrowApy"] * 100
        at_target = rate / morpho_curve(u0)
        return dict(rate=rate, util=u0 * 100, supplied=int(st["supplyAssets"]) / d,
                    borrowed=int(st["borrowAssets"]) / d, kink=90.0, kink_label="target", key=key,
                    curve=lambda u: at_target * morpho_curve(u))
    if m["kind"] == "euler":
        v = m["vault"]
        S, B = core.call(v, "totalAssets()") / d, core.call(v, "totalBorrows()") / d
        base, s1, s2, kink = euler_irm(v)
        rate = (math.exp(core.call(v, "interestRate()") / RAY * SY) - 1) * 100
        return dict(rate=rate, util=B / S * 100 if S else 0, supplied=S, borrowed=B, kink=kink / 2 ** 32 * 100,
                    kink_label="kink", curve=lambda u: euler_rate_at(u, base, s1, s2, kink))
    raise ValueError(f"unknown market kind {m['kind']}")


# ── 7-day history for charts ──
def history(m, snap, own):
    """-> list[(naive local datetime, rate %)]. Uses an API when one exists, otherwise the bot's own samples."""
    now = time.time()
    start = now - 7 * 86400
    if m.get("history") == "hyperlend_api":
        h = core.http("https://api.hyperlend.finance/data/interestRateHistory?chain=hyperEvm&token=" + asset())
        a = asset()
        pts = [(x["timestamp"] / 1000, int(x[a]["currentVariableBorrowRate"]) / 1e25) for x in h
               if x["timestamp"] / 1000 >= start and a in x]
    elif m["kind"] == "morpho":
        q = ('{ markets(first:1, where:{chainId_in:[%d], uniqueKey_in:["%s"]}){ items{ historicalState{ '
             'borrowApy(options:{startTimestamp:%d, endTimestamp:%d, interval:HOUR}){ x y } } } } }'
             % (chain_id(), snap["key"], start, now))
        pts = sorted((p["x"], p["y"] * 100) for p in
                     core.gql(q)["markets"]["items"][0]["historicalState"]["borrowApy"] if p["y"] is not None)
    else:
        pts = [(t, r) for t, r in own if t >= start]
    pts.append((now, snap["rate"]))
    return [(datetime.fromtimestamp(t, core.tz()).replace(tzinfo=None), r) for t, r in pts]
