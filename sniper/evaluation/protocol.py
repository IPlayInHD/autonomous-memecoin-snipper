"""The go-live gate. Run: python -m sniper.evaluate

Live mode is justified ONLY if every criterion holds:
 1. sample size: >= min_trades closed paper trades
 2. duration: the sample spans >= min_days (and even then: one regime)
 3. pre-registration: the score threshold file is unchanged since data
    collection started (hash recorded in the DB on first run)
 4. expectancy: bootstrapped 95% CI lower bound on net per-trade P&L > 0
 5. stability: expectancy positive in a majority of walk-forward time folds,
    including the FINAL (most recent) fold
 6. calibration: shadow trades confirm paper's fill rate and slippage within
    stated tolerances

Optimize for EXPECTANCY (mean net P&L per trade), never win rate: a 70% win
rate still bleeds out through -100% rug tails.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import constants as C
from ..models import RunMode
from ..storage.db import Database
from .bootstrap import BootstrapResult, bootstrap_ci

DEFAULT_MIN_TRADES = 300
DEFAULT_MIN_DAYS = 14.0
REGIME_WARN_DAYS = 28.0
FILL_RATE_TOLERANCE = 0.15        # |paper fill rate - shadow fill rate|
SLIPPAGE_TOLERANCE_BPS = 150.0    # mean |predicted - actual| slippage
MIN_SHADOW_TRADES = 20
WALK_FORWARD_FOLDS = 4


@dataclass
class Criterion:
    name: str
    passed: bool
    detail: str


@dataclass
class EvaluationReport:
    criteria: list[Criterion] = field(default_factory=list)
    bootstrap: Optional[BootstrapResult] = None
    fold_means: list[float] = field(default_factory=list)
    win_rate: Optional[float] = None
    outcome_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def go(self) -> bool:
        return bool(self.criteria) and all(c.passed for c in self.criteria)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.criteria.append(Criterion(name, passed, detail))


def _closed_trade_pnls(db: Database, mode: RunMode) -> list[tuple[float, float]]:
    """[(closed_at, net_pnl_sol)] for closed positions, oldest first.
    No-fill entries are excluded from per-trade expectancy but their fee burn
    is already inside positions that did fill? No — unfilled entries never open
    a position; their fees are reported separately as a warning."""
    rows = db.query(
        "SELECT closed_at, sol_received - sol_spent AS pnl FROM positions"
        " WHERE state='closed' AND mode=? ORDER BY closed_at", (mode.value,))
    return [(r["closed_at"], r["pnl"] / C.LAMPORTS_PER_SOL) for r in rows]


def evaluate_go_live(db: Database, mode: RunMode = RunMode.PAPER,
                     min_trades: int = DEFAULT_MIN_TRADES,
                     min_days: float = DEFAULT_MIN_DAYS) -> EvaluationReport:
    report = EvaluationReport()
    trades = _closed_trade_pnls(db, mode)
    pnls = [p for _, p in trades]

    # 1. sample size ---------------------------------------------------------
    report.add("sample_size", len(pnls) >= min_trades,
               f"{len(pnls)} closed {mode.value} trades (need {min_trades})")

    # 2. duration ------------------------------------------------------------
    span_days = 0.0
    if len(trades) >= 2:
        span_days = (trades[-1][0] - trades[0][0]) / 86400.0
    report.add("duration", span_days >= min_days,
               f"sample spans {span_days:.1f} days (need {min_days:.0f})")
    if span_days < REGIME_WARN_DAYS:
        report.warnings.append(
            f"REGIME WARNING: {span_days:.0f} days likely samples ONE market "
            "regime; results may not generalize to the next one.")

    # 3. pre-registration integrity -------------------------------------------
    recorded = db.get_meta("prereg_sha256")
    current = db.get_meta("prereg_current_sha256")  # updated at each startup
    if recorded is None:
        report.add("preregistration", False,
                   "no pre-registered threshold recorded — freeze "
                   "config/preregistration.json and rerun the bot first")
    else:
        unchanged = current is None or current == recorded
        report.add("preregistration", unchanged,
                   "threshold file unchanged since data collection started"
                   if unchanged else
                   "THRESHOLD FILE CHANGED MID-SAMPLE — results are tuned on "
                   "their own evaluation data and therefore void")

    # 4. expectancy with bootstrap CI ------------------------------------------
    if len(pnls) >= 2:
        bs = bootstrap_ci(pnls)
        report.bootstrap = bs
        report.win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        report.add("expectancy_ci",
                   bs.significantly_positive,
                   f"net expectancy {bs.mean:+.6f} SOL/trade, "
                   f"95% CI [{bs.ci_low:+.6f}, {bs.ci_high:+.6f}] — "
                   + ("lower bound > 0" if bs.significantly_positive
                      else "CI includes zero or worse; NO detectable edge"))
    else:
        report.add("expectancy_ci", False, "not enough trades to bootstrap")

    # 5. walk-forward stability --------------------------------------------------
    if len(pnls) >= WALK_FORWARD_FOLDS * 10:
        fold_size = len(pnls) // WALK_FORWARD_FOLDS
        folds = [pnls[i * fold_size:(i + 1) * fold_size]
                 for i in range(WALK_FORWARD_FOLDS)]
        report.fold_means = [sum(f) / len(f) for f in folds if f]
        positive = sum(1 for m in report.fold_means if m > 0)
        final_positive = report.fold_means[-1] > 0 if report.fold_means else False
        ok = positive > len(report.fold_means) / 2 and final_positive
        report.add("walk_forward", ok,
                   f"{positive}/{len(report.fold_means)} time folds positive; "
                   f"final fold {'positive' if final_positive else 'NEGATIVE'} "
                   f"(means: {[f'{m:+.5f}' for m in report.fold_means]})")
    else:
        report.add("walk_forward", False,
                   f"need >= {WALK_FORWARD_FOLDS * 10} trades for "
                   f"{WALK_FORWARD_FOLDS} folds")

    # 6. shadow calibration ---------------------------------------------------------
    cal = db.query("SELECT * FROM calibration")
    if len(cal) >= MIN_SHADOW_TRADES:
        fill_pred = sum(r["predicted_fill_prob"] for r in cal) / len(cal)
        fill_actual = sum(r["actually_filled"] for r in cal) / len(cal)
        slip_errs = [abs((r["actual_slippage_bps"] or 0) - (r["predicted_slippage_bps"] or 0))
                     for r in cal if r["actual_slippage_bps"] is not None]
        slip_err = sum(slip_errs) / len(slip_errs) if slip_errs else float("inf")
        ok = (abs(fill_pred - fill_actual) <= FILL_RATE_TOLERANCE
              and slip_err <= SLIPPAGE_TOLERANCE_BPS)
        report.add("shadow_calibration", ok,
                   f"fill rate paper {fill_pred:.2f} vs shadow {fill_actual:.2f} "
                   f"(tol {FILL_RATE_TOLERANCE}); mean |slippage error| "
                   f"{slip_err:.0f} bps (tol {SLIPPAGE_TOLERANCE_BPS:.0f})")
    else:
        report.add("shadow_calibration", False,
                   f"{len(cal)} shadow calibration trades (need {MIN_SHADOW_TRADES}) "
                   "— run shadow mode before any live decision")

    # context: rejection stats + fee bleed + rug tail --------------------------------
    for row in db.query(
            "SELECT outcome, COUNT(*) c FROM positions WHERE state='closed'"
            " AND mode=? GROUP BY outcome", (mode.value,)):
        report.outcome_counts[row["outcome"] or "unknown"] = row["c"]
    could_not_sell = report.outcome_counts.get("could_not_sell", 0)
    if could_not_sell:
        report.warnings.append(
            f"{could_not_sell} positions ended COULD_NOT_SELL (rug/no-route) — "
            "these are -100% tails, not normal losses.")
    unfilled_fees = db.query(
        "SELECT COALESCE(SUM(fee_base+fee_priority),0) f FROM trades"
        " WHERE filled=0 AND mode=?", (mode.value,))[0]["f"]
    if unfilled_fees:
        report.warnings.append(
            f"fee burn on unfilled entries: {unfilled_fees / 1e9:.4f} SOL — "
            "this cost is real even when nothing fills.")
    return report


def format_report(report: EvaluationReport) -> str:
    lines = ["", "=" * 72, "GO-LIVE EVALUATION", "=" * 72]
    for c in report.criteria:
        lines.append(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    if report.bootstrap:
        bs = report.bootstrap
        lines += ["", f"  trades={bs.n}  win_rate={report.win_rate:.1%}  "
                      f"mean={bs.mean:+.6f} SOL  median={bs.median:+.6f} SOL",
                  f"  left tail: p5={bs.p5:+.6f}  worst={bs.worst:+.6f} SOL"]
    if report.outcome_counts:
        lines.append(f"  outcomes: {report.outcome_counts}")
    for w in report.warnings:
        lines.append(f"  !! {w}")
    verdict = ("GO (criteria met — proceed to shadow-verified live at MINIMUM "
               "size, discounted fractional Kelly)" if report.go
               else "NO-GO — stay in paper/shadow.")
    lines += ["-" * 72, f"  VERDICT: {verdict}", "=" * 72, ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sniper.evaluate")
    parser.add_argument("--db", default="data/sniper.db")
    parser.add_argument("--mode", default="paper", choices=["paper", "shadow", "live"])
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--min-days", type=float, default=DEFAULT_MIN_DAYS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    db = Database(args.db)
    report = evaluate_go_live(db, RunMode(args.mode), args.min_trades, args.min_days)
    if args.json:
        print(json.dumps({
            "go": report.go,
            "criteria": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                         for c in report.criteria],
            "warnings": report.warnings,
            "generated_at": time.time(),
        }, indent=2))
    else:
        print(format_report(report))
    return 0 if report.go else 1


if __name__ == "__main__":
    raise SystemExit(main())
