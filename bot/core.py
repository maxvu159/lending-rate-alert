"""Shared plumbing: config, JSON-RPC, eth_call by signature, Morpho GraphQL, reference yields.

No web3 dependency: calls are built by hand, selectors come from keccak (pycryptodome).
All rates are percent per year.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from Crypto.Hash import keccak

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("RATE_BOT_DATA", ROOT / "data"))      # state.json, history.jsonl, out/
MORPHO_API = "https://blue-api.morpho.org/graphql"


@lru_cache(maxsize=1)
def config():
    p = Path(os.environ.get("RATE_BOT_CONFIG", ROOT / "config.json"))
    if not p.exists():
        raise SystemExit(f"config not found: {p} (copy config.example.json to config.json)")
    return json.loads(p.read_text())


def tz():
    return timezone(timedelta(hours=config().get("timezone_offset_hours", 0)))


def tz_label():
    h = config().get("timezone_offset_hours", 0)
    return "UTC" if not h else f"UTC{h:+g}"


def decimals():
    return 10 ** config()["asset"].get("decimals", 18)


def http(url, body=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def rpc(method, params):
    for i in range(3):
        try:
            d = http(config()["chain"]["rpc"], {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" in d:
                raise RuntimeError(d["error"])
            return d["result"]
        except Exception:
            if i == 2:
                raise
            time.sleep(2 * (i + 1))


@lru_cache(maxsize=None)
def selector(sig):
    return keccak.new(digest_bits=256, data=sig.encode()).hexdigest()[:8]


def word(x):
    """ABI-encode one static argument: int or address."""
    if isinstance(x, str):
        return x.lower().removeprefix("0x").zfill(64)
    return hex(int(x))[2:].zfill(64)


def call_raw(to, sig, *args, block="latest"):
    blk = block if isinstance(block, str) else hex(block)
    out = rpc("eth_call", [{"to": to, "data": "0x" + selector(sig) + "".join(word(a) for a in args)}, blk])
    return out.removeprefix("0x")


def call(to, sig, *args, block="latest"):
    """eth_call returning the first word as int (0 for empty)."""
    out = call_raw(to, sig, *args, block=block)
    return int(out[:64], 16) if out else 0


def words(hexdata):
    return [hexdata[i:i + 64] for i in range(0, len(hexdata), 64)]


def to_address(w):
    """Last 20 bytes of an ABI word, keeping leading zero bytes."""
    if isinstance(w, int):
        if not 0 <= w < 2 ** 160:
            raise ValueError(f"address value out of range: {w}")
        w = f"{w:040x}"
    return "0x" + w[-40:]


def block_number():
    return int(rpc("eth_blockNumber", []), 16)


def block_time(b):
    return int(rpc("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)


def week_window():
    """(head, old, seconds between them): the block about 7 days ago and the real elapsed time."""
    head = block_number()
    old = max(head - 7 * config()["chain"].get("blocks_per_day", 86400), 1)
    return head, old, block_time(head) - block_time(old)


def annualise(now, then, seconds):
    return (now / then - 1) * 365 * 86400 / seconds * 100 if then and seconds else 0.0


def gql(q):
    d = http(MORPHO_API, {"query": q})
    if "errors" in d:
        raise RuntimeError(d["errors"][0]["message"])
    return d["data"]


def yields(window=None):
    """Reference yields from config['yields'], 7-day change annualised. Types:
    exchange_rate (one view call), balance_ratio (two balances of one wallet), erc4626 (convertToAssets), fixed."""
    head, old, secs = window or week_window()
    out = {}
    for name, y in config().get("yields", {}).items():
        if name.startswith("_"):
            continue
        t = y["type"]
        if t == "fixed":
            out[name] = float(y["value"])
            continue
        if t == "exchange_rate":
            f = lambda b, y=y: call(y["contract"], y["signature"], *([int(y["arg"])] if "arg" in y else []), block=b)
        elif t == "erc4626":
            f = lambda b, y=y: call(y["vault"], "convertToAssets(uint256)", 10 ** 18, block=b)
        elif t == "balance_ratio":
            f = lambda b, y=y: (call(y["numerator"], "balanceOf(address)", y["wallet"], block=b) * 10 ** 18
                                // max(call(y["denominator"], "balanceOf(address)", y["wallet"], block=b), 1))
        else:
            raise ValueError(f"unknown yield type {t} for {name}")
        out[name] = annualise(f(head), f(old), secs)
    return out
