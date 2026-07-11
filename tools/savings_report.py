# -*- coding: utf-8 -*-
"""Savings/trend report across both contours for the weekly
calibration's savings check: is delegation actually saving money, and
what's the trend.

Computes from cc_usage (the subscription contour) and requests (the
API contour) in gateway/requests.db, using full API list prices (no
batch discount; cache discounts read x0.1 / write x1.25 -- a real API
mechanism):

1. PRE (< --routed-start) and ROUTED (>= --routed-start [.. --until])
   windows: turns, accounted cost, $/day, a main/side breakdown by
   model.
2. Delegation counterfactual: token profiles of routed sidechains,
   re-priced at the top-tier model's rates, against the actual cost --
   gross savings in $ and %.
3. API contour: requests and accounted cost by traffic_kind.

The first run's recorded baseline is your first savings/trend report
-- keep it for comparison against later runs. Method caveats (a
censored baseline, and that the coordination premium isn't separable
from non-delegable top-tier work) belong in that same baseline record
and remain in force.

Prices deliberately do NOT duplicate tools/usage_report.py's
PRICES_PER_TOKEN_USD -- they're imported from there, the single owner
of pricing.
"""
import argparse
import io
import sqlite3
import sys
from pathlib import Path

from usage_report import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, PRICES_PER_TOKEN_USD

FABLE_MODEL = "claude-fable-5"


def _cost(model: str, i: int, o: int, cw: int, cr: int):
    p = PRICES_PER_TOKEN_USD.get(model)
    if p is None:
        return None
    return (i * p[0] + o * p[1]
            + cw * p[0] * CACHE_WRITE_MULTIPLIER
            + cr * p[0] * CACHE_READ_MULTIPLIER)


def fable_counterfactual(i: int, o: int, cw: int, cr: int) -> float:
    """The same token profile priced at the top-tier model's rates --
    "what if the top tier had done this instead"."""
    return _cost(FABLE_MODEL, i or 0, o or 0, cw or 0, cr or 0)


def window_summary(db: sqlite3.Connection, cond: str, params: tuple) -> dict:
    rows = db.execute(
        "select model, is_sidechain, count(*), sum(accounted_cost_usd),"
        " count(distinct session_id) from cc_usage where " + cond +
        " group by model, is_sidechain order by 4 desc", params).fetchall()
    days = db.execute(
        "select count(distinct substr(ts,1,10)) from cc_usage where " + cond,
        params).fetchone()[0]
    total_cost = sum(r[3] or 0 for r in rows)
    total_turns = sum(r[2] for r in rows)
    return {"rows": rows, "days": days, "total_cost": total_cost,
            "total_turns": total_turns,
            "per_day": total_cost / days if days else 0.0}


def counterfactual_summary(db: sqlite3.Connection, cond: str, params: tuple) -> dict:
    rows = db.execute(
        "select agent_type, model, count(*), sum(input_tokens), sum(output_tokens),"
        " sum(cache_creation_tokens), sum(cache_read_tokens), sum(accounted_cost_usd)"
        " from cc_usage where is_sidechain=1 and " + cond +
        " group by agent_type, model order by 8 desc", params).fetchall()
    detail = []
    actual = cf = 0.0
    for at, model, n, ti, to, tcw, tcr, cost in rows:
        f = fable_counterfactual(ti, to, tcw, tcr)
        actual += cost or 0
        cf += f
        detail.append({"agent_type": at, "model": model, "turns": n,
                       "actual": cost or 0, "as_fable": f})
    return {"detail": detail, "actual": actual, "as_fable": cf,
            "gross_savings": cf - actual,
            "savings_pct": (1 - actual / cf) * 100 if cf else 0.0}


def api_contour_summary(db: sqlite3.Connection) -> dict:
    kinds = db.execute(
        "select traffic_kind, count(*), sum(cost_usd) from requests"
        " group by traffic_kind order by 3 desc").fetchall()
    total = db.execute("select count(*), sum(cost_usd) from requests").fetchone()
    return {"kinds": kinds, "total_n": total[0], "total_cost": total[1] or 0.0}


def print_report(db_path: str, routed_start: str, until: str = None) -> None:
    db = sqlite3.connect(db_path)
    until_cond, until_params = ("", ()) if not until else (" and ts < ?", (until,))

    for label, cond, params in [
        ("PRE-ROUTING (< %s)" % routed_start, "ts < ?", (routed_start,)),
        ("ROUTED (>= %s%s)" % (routed_start, f" .. {until}" if until else ""),
         "ts >= ?" + until_cond, (routed_start,) + until_params),
    ]:
        w = window_summary(db, cond, params)
        print(f"\n===== {label} =====")
        for model, sc, n, cost, sess in w["rows"]:
            kind = "side" if sc else "main"
            print(f"  {model:28} {kind:4} turns={n:5} sess={sess:3}"
                  f" cost=${cost or 0:9.2f}  $/turn={((cost or 0) / n):.4f}")
        print(f"  TOTAL: {w['total_turns']} turns, ${w['total_cost']:.2f},"
              f" {w['days']} days, ${w['per_day']:.2f}/day")

    c = counterfactual_summary(db, "ts >= ?" + until_cond,
                               (routed_start,) + until_params)
    print("\n===== COUNTERFACTUAL: routed sidechains at the top-tier model's rates =====")
    for d in c["detail"]:
        print(f"  {str(d['agent_type']):16} {d['model']:28} turns={d['turns']:4}"
              f" actual=${d['actual']:8.2f} as-top-tier=${d['as_fable']:8.2f}")
    print(f"  TOTAL: actual=${c['actual']:.2f}  as-top-tier=${c['as_fable']:.2f}"
          f"  gross savings=${c['gross_savings']:.2f} ({c['savings_pct']:.0f}%)")

    a = api_contour_summary(db)
    print("\n===== API CONTOUR (requests.db, full history) =====")
    for kind, n, cost in a["kinds"]:
        print(f"  {kind:10} n={n:4} cost=${cost or 0:.4f}")
    print(f"  TOTAL: {a['total_n']} requests, ${a['total_cost']:.4f} accounted")

    print("\nFor the calibrated event's notes: $/day ROUTED, gross"
          " savings in $ and %, actual sidechain cost, API total;"
          " compare against your first run's recorded baseline.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Savings/trend report (calibration check 18)")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent
                                        / "gateway" / "requests.db"))
    ap.add_argument("--routed-start", default="2026-07-08",
                    help="the PRE/ROUTED boundary (when routing was deployed on this repo)")
    ap.add_argument("--until", default=None,
                    help="upper bound of the ROUTED window (for reproducible slices)")
    args = ap.parse_args(argv)
    print_report(args.db, args.routed_start, args.until)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.exit(main())
