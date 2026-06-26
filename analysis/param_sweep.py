"""
Parameter robustness sweep for the reaction strategy.

Sweeps displacement_threshold and price_band over a range of values.
A genuine edge survives perturbation.  A knife-edge effect (profit only
at exactly one threshold) signals overfitting — reject that parameter.

Usage:
  uv run python analysis/param_sweep.py
  uv run python analysis/param_sweep.py --after 2026-06-17
  uv run python analysis/param_sweep.py --max-elapsed 60 --after 2026-06-17
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np

from fees import net_pnl_per_stake  # noqa: E402

DB = "data/kalbot.db"

THRESHOLDS = [0.05, 0.075, 0.10, 0.125, 0.15, 0.20, 0.25, 0.30]

BAND_CONFIGS = [
    (0.50, 0.80, "wide"),
    (0.55, 0.72, "default"),
    (0.57, 0.70, "tight"),
    (0.60, 0.70, "core (OOS winner)"),
    (0.55, 0.65, "low"),
    (0.65, 0.75, "high"),
]


def load_all(
    conn: sqlite3.Connection,
    max_elapsed: int,
    after: str | None,
) -> list[dict]:
    query = """
        SELECT COALESCE(realistic_fill_price, trade_fill_price, trade_entry_price),
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

    rows = conn.execute(query, params).fetchall()
    out = []
    for entry, side, outcome, elapsed, spot_disp in rows:
        if not entry or not (0.0 < entry < 1.0):
            continue
        out.append({
            "entry": float(entry),
            "side": side,
            "outcome": outcome,
            "elapsed": elapsed,
            "spot_disp": float(spot_disp) if spot_disp else 0.0,
        })
    return out


def eval_combo(
    trades: list[dict],
    threshold: float,
    band_low: float,
    band_high: float,
) -> tuple[int, float, float]:
    active = [
        t for t in trades
        if t["spot_disp"] >= threshold and band_low <= t["entry"] <= band_high
    ]
    if not active:
        return 0, 0.0, 0.0
    pnls = [net_pnl_per_stake(t["entry"], t["side"] == t["outcome"]) for t in active]
    arr = np.array(pnls)
    return len(arr), float(arr.sum()), float((arr > 0).mean())


def run(max_elapsed: int, after: str | None) -> None:
    conn = sqlite3.connect(DB)
    trades = load_all(conn, max_elapsed, after)
    conn.close()

    date_note = f"after={after}" if after else "all dates"
    print(f"\nPARAMETER ROBUSTNESS SWEEP")
    print(f"  elapsed <= {max_elapsed}s  |  {date_note}  |  n_base={len(trades)}")
    print(f"  Fee: 0.072 * entry * (1-entry) per $1 stake\n")

    if len(trades) == 0:
        print("  No trades found — check --after date or --max-elapsed value.")
        return

    print(
        f"  {'Threshold':>10}  {'Band':>22}  {'N':>5}  "
        f"{'NetPnL':>8}  {'WinRate':>8}  {'Net/T':>8}"
    )
    print("  " + "-" * 72)

    for threshold in THRESHOLDS:
        first_band = True
        for bl, bh, label in BAND_CONFIGS:
            n, net, wr = eval_combo(trades, threshold, bl, bh)
            marker = " ◄" if net > 0 and n >= 10 else "  "
            thresh_str = f"{threshold:.3f}%" if first_band else ""
            first_band = False
            print(
                f"  {thresh_str:>10}  {label:>22}  {n:>5}  "
                f"{net:>+8.3f}  {wr:>7.1%}  {net/n if n else 0:>+8.4f}{marker}"
            )
        print()

    print("  ◄ = positive net PnL with n ≥ 10")
    print("  Real edge: ◄ appears across ≥ 3 consecutive threshold rows.")
    print("  Knife-edge: ◄ at exactly one threshold row → overfit, reject.")


def main() -> None:
    p = argparse.ArgumentParser(description="Reaction strategy parameter robustness sweep")
    p.add_argument("--max-elapsed", type=int, default=90,
                   help="Only include trades with elapsed_seconds <= this (default 90)")
    p.add_argument("--after", type=str, default=None,
                   help="Only include trades after this date (YYYY-MM-DD). "
                        "Pass the reaction-bot deploy date to exclude forecaster trades.")
    args = p.parse_args()
    run(args.max_elapsed, args.after)


if __name__ == "__main__":
    main()
