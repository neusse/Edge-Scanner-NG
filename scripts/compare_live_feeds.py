#!/usr/bin/env python
"""Compare two scanners running side by side on different data providers.

    python scripts/compare_live_feeds.py
    python scripts/compare_live_feeds.py --a 7777 --b 7787 --a-alerts data/alerts --b-alerts data/alerts_schwab

Read-only: it asks each scanner's own API for symbol state and reads the two
alert archives. It never touches a data provider, a stream or a setting.

Two questions, both answered per data tier (most liquid first), because on
Schwab the tiers get their bars differently (real bars / built from streamed
quotes / built from polled quotes):

  STATE   For a sample of symbols in each tier, do the two scanners agree on
          price, session volume, VWAP, relative volume, high and low of day,
          and the premarket levels?
  ALERTS  Which alerts fired on both, on A only, on B only, and for the ones
          on both, how far apart were the time and the price?

Writes a markdown report to data/feed_compare/ and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

_REPO_ROOT = Path(__file__).parent.parent
os.chdir(_REPO_ROOT)

_ET = ZoneInfo("America/New_York")
_TIERS = (("real bars (1-300)", 0, 300), ("streamed quotes (301-3,300)", 300, 3300), ("polled quotes (3,301+)", 3300, 10 ** 9))
_FIELDS = ("price", "pm_vol", "pm_high", "pm_low", "session_open", "vwap", "hod", "lod", "rvol")
_MATCH_WINDOW_MIN = 3        # same symbol + setup + direction within this many minutes = the same alert


def _rank(universe: Path) -> list[str]:
    """Symbols most liquid first, the order the scanner subscribes in."""
    df = pd.read_csv(universe)
    if "avg_dollar_vol_20d" in df.columns:
        df = df.sort_values("avg_dollar_vol_20d", ascending=False)
    return df["symbol"].astype(str).tolist()


def _tier_of(rank: dict[str, int], sym: str) -> str:
    i = rank.get(sym)
    if i is None:
        return "not in universe"
    for name, lo, hi in _TIERS:
        if lo <= i < hi:
            return name
    return "?"


def _state(port: int, sym: str) -> dict | None:
    try:
        r = requests.get(f"http://localhost:{port}/api/v2/state/{sym}", timeout=10)
        j = r.json()
        return j if j.get("found") else None
    except Exception:
        return None


def _pct(a, b) -> float | None:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if a != a or b != b or a == 0:
        return None
    return (b - a) / abs(a) * 100.0


def compare_state(a: int, b: int, ranked: list[str], per_tier: int) -> tuple[list[str], dict]:
    lines, summary = [], {}
    for name, lo, hi in _TIERS:
        pool = ranked[lo:hi]
        if not pool:
            continue
        step = max(1, len(pool) // per_tier)
        sample = pool[::step][:per_tier]
        diffs: dict[str, list[float]] = defaultdict(list)
        have_a = have_b = both = 0
        missing_b: list[str] = []
        for sym in sample:
            sa, sb = _state(a, sym), _state(b, sym)
            if sa is None or sb is None:
                continue
            # "Has a live session" = the scanner has seen at least one bar today.
            la = (sa.get("pm_vol") or 0) > 0 or sa.get("session_open") is not None
            lb = (sb.get("pm_vol") or 0) > 0 or sb.get("session_open") is not None
            have_a += la
            have_b += lb
            if la and not lb:
                missing_b.append(sym)
            if la and lb:
                both += 1
                for f in _FIELDS:
                    d = _pct(sa.get(f), sb.get(f))
                    if d is not None:
                        diffs[f].append(d)
        lines.append(f"### {name}: sampled {len(sample)}")
        lines.append(f"- symbols with bars today: A {have_a}, B {have_b}, both {both}")
        if missing_b:
            lines.append(f"- A has bars, B has none: {', '.join(missing_b[:15])}")
        if diffs:
            lines.append("")
            lines.append("| field | n | median diff % (B vs A) | median abs diff % | worst abs % |")
            lines.append("|---|---|---|---|---|")
            for f in _FIELDS:
                v = diffs.get(f)
                if v:
                    lines.append(f"| {f} | {len(v)} | {statistics.median(v):+.2f} | "
                                 f"{statistics.median([abs(x) for x in v]):.2f} | {max(abs(x) for x in v):.1f} |")
        lines.append("")
        summary[name] = {"sampled": len(sample), "a_live": have_a, "b_live": have_b, "both": both,
                         "median_abs": {f: round(statistics.median([abs(x) for x in v]), 3) for f, v in diffs.items()},
                         "median_signed": {f: round(statistics.median(v), 3) for f, v in diffs.items()}}
    return lines, summary


def _load_alerts(root: Path, day: str) -> list[dict]:
    p = root / "all" / f"{day}.jsonl"
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            a = json.loads(line)
            a["_t"] = pd.Timestamp(a["timestamp"]).tz_convert("UTC")
            out.append(a)
        except Exception:
            continue
    return out


def compare_alerts(a_dir: Path, b_dir: Path, day: str, rank: dict[str, int],
                   since: pd.Timestamp | None = None) -> tuple[list[str], dict]:
    A, B = _load_alerts(a_dir, day), _load_alerts(b_dir, day)
    if since is not None:
        A = [x for x in A if x["_t"] >= since]
        B = [x for x in B if x["_t"] >= since]
    key = lambda x: (x.get("symbol"), x.get("setup") or x.get("trigger"), x.get("direction"))
    by_b: dict[tuple, list[dict]] = defaultdict(list)
    for x in B:
        by_b[key(x)].append(x)
    matched, only_a, used = [], [], set()
    for x in A:
        best = None
        for y in by_b.get(key(x), []):
            if id(y) in used:
                continue
            dt = abs((y["_t"] - x["_t"]).total_seconds()) / 60.0
            if dt <= _MATCH_WINDOW_MIN and (best is None or dt < best[0]):
                best = (dt, y)
        if best:
            used.add(id(best[1]))
            matched.append((x, best[1], best[0]))
        else:
            only_a.append(x)
    only_b = [y for y in B if id(y) not in used]

    lines = [f"A: {len(A)} alerts, B: {len(B)} alerts. Matched {len(matched)}, A only {len(only_a)}, B only {len(only_b)} "
             f"(same symbol, setup and direction within {_MATCH_WINDOW_MIN} min).", ""]
    tiers = [t[0] for t in _TIERS] + ["not in universe"]
    lines += ["| tier | matched | A only | B only | agreement |", "|---|---|---|---|---|"]
    summary = {"a": len(A), "b": len(B), "matched": len(matched), "a_only": len(only_a), "b_only": len(only_b), "tiers": {}}
    for t in tiers:
        m = sum(1 for x, _, _ in matched if _tier_of(rank, x["symbol"]) == t)
        oa = sum(1 for x in only_a if _tier_of(rank, x["symbol"]) == t)
        ob = sum(1 for x in only_b if _tier_of(rank, x["symbol"]) == t)
        if m + oa + ob == 0:
            continue
        agree = m / (m + oa + ob) * 100
        lines.append(f"| {t} | {m} | {oa} | {ob} | {agree:.0f}% |")
        summary["tiers"][t] = {"matched": m, "a_only": oa, "b_only": ob, "agreement_pct": round(agree, 1)}
    if matched:
        dts = [d for _, _, d in matched]
        dps = [abs(_pct(x.get("price"), y.get("price")) or 0.0) for x, y, _ in matched]
        lines += ["", f"Matched alerts: median time gap {statistics.median(dts):.1f} min, "
                      f"median price gap {statistics.median(dps):.3f}%, worst price gap {max(dps):.2f}%."]
    for label, rows in (("A only", only_a), ("B only", only_b)):
        if rows:
            c = Counter((x.get("setup_label") or x.get("setup") or x.get("trigger") or "?") for x in rows)
            lines += ["", f"**{label}, by setup:** " + ", ".join(f"{k} {v}" for k, v in c.most_common(12))]
            lines.append("Examples: " + "; ".join(
                f"{x['symbol']} {x['_t'].tz_convert(_ET).strftime('%H:%M')} {x.get('setup_label') or x.get('setup')} "
                f"rvol={x.get('rvol') if x.get('rvol') is None else round(float(x['rvol']), 2)}" for x in rows[:12]))
    return lines, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a", type=int, default=7777, help="port of scanner A (default 7777)")
    ap.add_argument("--b", type=int, default=7787, help="port of scanner B (default 7787)")
    ap.add_argument("--a-alerts", default="data/alerts")
    ap.add_argument("--b-alerts", default="data/alerts_schwab")
    ap.add_argument("--universe", default="data/universe_all.csv")
    ap.add_argument("--per-tier", type=int, default=40, help="symbols sampled per tier for the state check")
    ap.add_argument("--since", default="", help="only compare alerts from this ET time today, HH:MM "
                                                "(use it after a restart: a scanner that was down has no alerts to match)")
    args = ap.parse_args()

    now = datetime.now(_ET)
    day = now.strftime("%Y-%m-%d")
    ranked = _rank(Path(args.universe))
    rank = {s: i for i, s in enumerate(ranked)}

    out = [f"# Feed comparison {now.strftime('%Y-%m-%d %H:%M ET')}", "",
           f"A = localhost:{args.a} ({args.a_alerts}), B = localhost:{args.b} ({args.b_alerts}). "
           f"Differences are B relative to A.", ""]
    for port in (args.a, args.b):
        try:
            requests.get(f"http://localhost:{port}/api/v2/clock", timeout=5)
        except Exception as exc:
            out.append(f"**Scanner on port {port} is not answering: {exc}**")
    out += ["## State", ""]
    s_lines, s_sum = compare_state(args.a, args.b, ranked, args.per_tier)
    out += s_lines
    out += ["## Alerts", ""]
    since = None
    if args.since:
        since = pd.Timestamp(f"{day} {args.since}", tz=_ET).tz_convert("UTC")
        out.append(f"Alerts from {args.since} ET only.")
        out.append("")
    a_lines, a_sum = compare_alerts(Path(args.a_alerts), Path(args.b_alerts), day, rank, since)
    out += a_lines

    dest = Path("data/feed_compare")
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{now.strftime('%Y-%m-%d_%H%M')}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    (dest / "latest.json").write_text(json.dumps({"at": now.isoformat(), "state": s_sum, "alerts": a_sum}, indent=2), encoding="utf-8")
    print("\n".join(out))
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
