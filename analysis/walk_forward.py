"""
Walk-forward validation on reaction-window trades from data/kalbot.db.

Protocol:
  1. Pull settled primary windows where elapsed_seconds <= max_elapsed.
  2. Optionally filter to snapshot_time >= --after DATE (use the reaction-bot
     deploy date so historical forecaster trades are excluded).
  3. Sort chronologically.  Split into N sequential folds (default 4).
  4. For each fold k=1..N-1:
       in-sample  = all folds before k
       out-of-sample = fold k
     Sweep displacement_threshold on IS data, pick best.
     Apply that threshold UNCHANGED to OS data — report IS and OS net PnL.
  5. Walk-forward efficiency = sum(OS) / sum(IS).
     Target > 70%.  Below 50% = overfit, kill the strategy.

Usage:
  uv run python analysis/walk_forward.py
  uv run python analysis/walk_forward.py --folds 3 --after 2026-06-17
  uv run python analysis/walk_forward.py --max-elapsed 60
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np

from fees import net_pnl_per_stake, taker_fee_per_stake  # noqa: E402

DB = "data/kalbot.db"
THRESHOLDS = [0.05, 0.075, 0.10, 0.125, 0.15, 0.20, 0.25, 0.30]


def load_trades(
    conn: sqlite3.Connection,
    max_elapsed: int,
    after: str | None,
) -> list[dict]:
    query = """
        SELECT snapshot_time,
               COALESCE(realistic_fill_price, trade_fill_price, trade_entry_price),
               trade_side,
               settlement_outcome,
               elapsed_seconds,
               ABS(spot_displacement_pct)
        FROM windows
        WHERE is_primary = 1
          AND traded = 1
          AND settlement_outcome IS NOT NULL
          AND elapsed_seconds <= ?
          AND spot_displacement_pct IS NOT NULL
    """
    params: list = [max_elapsed]

    if after:
        query += " AND snapshot_time >= ?"
        params.append(after)

    query += " ORDER BY snapshot_time"

    rows = conn.execute(query, params).fetchall()
    out = []
    for ts, entry, side, outcome, elapsed, spot_disp in rows:
        if not entry or not (0.0 < entry < 1.0):
            continue
        out.append({
            "ts": ts,
            "entry": float(entry),
            "side": side,
            "outcome": outcome,
            "elapsed": elapsed,
            "spot_disp": float(spot_disp) if spot_disp else 0.0,
        })
    return out


def eval_threshold(trades: list[dict], threshold: float) -> dict:
    active = [t for t in trades if t["spot_disp"] >= threshold]
    if not active:
        return {"n": 0, "net_pnl": 0.0, "win_rate": 0.0}
    pnls = [net_pnl_per_stake(t["entry"], t["side"] == t["outcome"]) for t in active]
    return {
        "n": len(pnls),
        "net_pnl": sum(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
    }


def run(n_folds: int, max_elapsed: int, after: str | None) -> None:
    conn = sqlite3.connect(DB)
    trades = load_trades(conn, max_elapsed, after)
    conn.close()

    date_note = f" | after={after}" if after else " | all dates"
    print(f"\nWALK-FORWARD VALIDATION")
    print(f"  elapsed <= {max_elapsed}s{date_note}  |  n={len(trades)}  |  folds={n_folds}")
    print(f"  Fee: 0.072 * entry * (1-entry) per $1 stake\n")

    if len(trades) < n_folds * 10:
        print(
            f"  WARNING: only {len(trades)} trades — need ≥ {n_folds * 10} for {n_folds} folds.\n"
            f"  Run the bot longer before interpreting these results."
        )
        if len(trades) < n_folds * 3:
            return

    fold_size = len(trades) // n_folds
    os_pnls: list[float] = []
    is_pnls: list[float] = []

    print(f"{'Fold':>5}  {'IS_thresh':>10}  {'IS_n':>6}  {'IS_net':>8}  "
          f"{'OS_n':>6}  {'OS_net':>8}  {'OS_WR':>7}")
    print("  " + "-" * 60)

    for fold in range(1, n_folds):
        is_trades = trades[: fold * fold_size]
        os_trades = trades[fold * fold_size: (fold + 1) * fold_size]

        best_thresh = THRESHOLDS[0]
        best_is_pnl = float("-inf")
        for t in THRESHOLDS:
            r = eval_threshold(is_trades, t)
            if r["n"] >= 5 and r["net_pnl"] > best_is_pnl:
                best_is_pnl = r["net_pnl"]
                best_thresh = t

        is_r = eval_threshold(is_trades, best_thresh)
        os_r = eval_threshold(os_trades, best_thresh)

        os_pnls.append(os_r["net_pnl"])
        is_pnls.append(is_r["net_pnl"])

        print(
            f"  {fold:>3}  {best_thresh:>9.3f}%  {is_r['n']:>6}  {is_r['net_pnl']:>+8.3f}  "
            f"{os_r['n']:>6}  {os_r['net_pnl']:>+8.3f}  {os_r['win_rate']:>6.1%}"
        )

    total_is = sum(is_pnls)
    total_os = sum(os_pnls)
    efficiency = total_os / total_is if total_is != 0 else 0.0

    print()
    print(f"  Total IS net PnL : {total_is:>+8.3f}")
    print(f"  Total OS net PnL : {total_os:>+8.3f}")
    print(f"  Walk-forward eff : {efficiency:>7.1%}  (target >70%, kill <50%)")


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward validation on reaction-window trades")
    p.add_argument("--folds", type=int, default=4, help="Number of folds (default 4)")
    p.add_argument("--max-elapsed", type=int, default=90,
                   help="Only include trades with elapsed_seconds <= this (default 90)")
    p.add_argument("--after", type=str, default=None,
                   help="Only include trades after this date (YYYY-MM-DD). "
                        "Pass the reaction-bot deploy date to exclude forecaster trades.")
    args = p.parse_args()
    run(args.folds, args.max_elapsed, args.after)


if __name__ == "__main__":
    main()
