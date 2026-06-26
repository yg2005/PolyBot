"""
Phase 1 Kill Test — fee-aware post-mortem on all primary settled windows.

Fee formula (Polymarket CLOB taker, crypto category):
  fee = contracts × 0.07 × p_yes × (1 − p_yes)
  where contracts = stake / entry_price
  Simplifies to: fee = stake × 0.07 × (1 − entry_price) [for any side]

PnL per $1 stake:
  WIN  → gross = (1 − entry) / entry
  LOSE → gross = −1
  net  = gross − 0.07 × (1 − entry)
"""

import sqlite3
import numpy as np
from collections import defaultdict

DB = "data/kalbot.db"
TAKER_FEE = 0.07   # 7% taker rate (user's formula uses 0.072; we use 0.07 from docs,
                    # note at end compares both)

def load_primary_signals(conn):
    """All settled primary windows with YES or NO rule_signal."""
    cur = conn.execute("""
        SELECT
            window_id,
            rule_signal,
            yes_ask,
            no_ask,
            mid_price,
            trade_entry_price,
            realistic_fill_price,
            trade_fill_price,
            trade_side,
            traded,
            settlement_outcome,
            elapsed_seconds,
            snapshot_time,
            window_open_time,
            model_prob,
            spot_displacement_pct,
            spot_trend_1m
        FROM windows
        WHERE is_primary = 1
          AND settlement_outcome IS NOT NULL
          AND rule_signal IN ('YES', 'NO')
    """)
    return cur.fetchall()


def compute_trade(rule_signal, yes_ask, no_ask, settlement_outcome,
                  actual_entry=None, actual_side=None):
    """
    Returns (entry_price, won, gross_pnl, fee, net_pnl) per $1 stake.
    Uses actual fill if available, else signal ask.
    """
    if actual_entry and actual_side:
        side = actual_side
        entry = actual_entry
    else:
        side = rule_signal
        entry = yes_ask if side == "YES" else no_ask

    if entry is None or entry <= 0 or entry >= 1:
        return None

    won = (settlement_outcome == side)
    gross = (1 - entry) / entry if won else -1.0
    fee = TAKER_FEE * (1 - entry)          # per $1 stake
    net = gross - fee
    return entry, won, gross, fee, net


def bucket_label(entry):
    if entry < 0.30:
        return "<0.30"
    elif entry < 0.40:
        return "0.30-0.40"
    elif entry < 0.50:
        return "0.40-0.50"
    elif entry < 0.60:
        return "0.50-0.60"
    elif entry < 0.70:
        return "0.60-0.70"
    elif entry < 0.80:
        return "0.70-0.80"
    else:
        return "0.80+"


def analyze(rows, label):
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0.0,
                                    "fees": 0.0, "net": 0.0, "entries": []})
    all_nets = []

    for row in rows:
        (wid, rule_signal, yes_ask, no_ask, mid_price,
         trade_entry, realistic_fill, trade_fill,
         trade_side, traded, settlement_outcome,
         elapsed, snap_time, open_time, model_prob,
         spot_disp, spot_trend) = row

        # prefer realistic fill > trade fill > signal ask
        actual_entry = realistic_fill or trade_fill or trade_entry if traded else None
        actual_side = trade_side if traded else None

        result = compute_trade(rule_signal, yes_ask, no_ask,
                               settlement_outcome, actual_entry, actual_side)
        if result is None:
            continue

        entry, won, gross, fee, net = result
        bl = bucket_label(entry)
        b = buckets[bl]
        b["n"] += 1
        b["wins"] += int(won)
        b["gross"] += gross
        b["fees"] += fee
        b["net"] += net
        b["entries"].append(entry)
        all_nets.append(net)

    print(f"\n{'='*70}")
    print(f"  {label}  (n={len(all_nets)})")
    print(f"{'='*70}")
    header = f"{'Bucket':<12} {'N':>6} {'WinRate':>8} {'GrossPnL':>10} {'TotalFee':>10} {'NetPnL':>10} {'Net/Trade':>10}"
    print(header)
    print("-" * 70)

    order = ["<0.30", "0.30-0.40", "0.40-0.50", "0.50-0.60",
             "0.60-0.70", "0.70-0.80", "0.80+"]

    total_n = total_wins = 0
    total_gross = total_fees = total_net = 0.0

    for bl in order:
        b = buckets[bl]
        n = b["n"]
        if n == 0:
            continue
        wr = b["wins"] / n
        g = b["gross"]
        f = b["fees"]
        nt = b["net"]
        npt = nt / n
        marker = " ◄ POSITIVE" if nt > 0 else ""
        print(f"{bl:<12} {n:>6,} {wr:>7.1%} {g:>10.2f} {f:>10.2f} {nt:>10.2f} {npt:>10.4f}{marker}")
        total_n += n
        total_wins += b["wins"]
        total_gross += g
        total_fees += f
        total_net += nt

    print("-" * 70)
    print(f"{'TOTAL':<12} {total_n:>6,} {total_wins/total_n if total_n else 0:>7.1%} "
          f"{total_gross:>10.2f} {total_fees:>10.2f} {total_net:>10.2f} "
          f"{total_net/total_n if total_n else 0:>10.4f}")

    if all_nets:
        arr = np.array(all_nets)
        print(f"\n  Sharpe (per-trade): {arr.mean()/arr.std():.4f}")
        print(f"  Mean net/trade: {arr.mean():.4f}")
        print(f"  Std: {arr.std():.4f}")

    return buckets, all_nets


def latency_analysis(conn):
    """
    Estimate entry freshness: how many seconds after window_open do we fire?
    And: do we see spot move BEFORE the Polymarket price moves?
    """
    print(f"\n{'='*70}")
    print("  LATENCY / ENTRY TIMING ANALYSIS")
    print(f"{'='*70}")

    # elapsed_seconds at snapshot for all primary windows
    cur = conn.execute("""
        SELECT elapsed_seconds, traded
        FROM windows
        WHERE is_primary=1 AND settlement_outcome IS NOT NULL
          AND elapsed_seconds IS NOT NULL
    """)
    rows = cur.fetchall()
    elapsed_all = [r[0] for r in rows]
    elapsed_traded = [r[0] for r in rows if r[1] == 1]

    print(f"\n  All primary windows — elapsed_seconds at snapshot:")
    arr = np.array(elapsed_all)
    print(f"    n={len(arr)}, mean={arr.mean():.0f}s, median={np.median(arr):.0f}s, "
          f"p10={np.percentile(arr,10):.0f}s, p90={np.percentile(arr,90):.0f}s")

    if elapsed_traded:
        arr2 = np.array(elapsed_traded)
        print(f"\n  Traded windows — elapsed_seconds at entry:")
        print(f"    n={len(arr2)}, mean={arr2.mean():.0f}s, median={np.median(arr2):.0f}s, "
              f"p10={np.percentile(arr2,10):.0f}s, p90={np.percentile(arr2,90):.0f}s")

    # Distribution of entry timing vs 30-90s lag window
    buckets_lag = {"<30s": 0, "30-90s (target)": 0, "90-180s": 0,
                   "180-270s": 0, ">270s": 0}
    for e in elapsed_traded:
        if e < 30:
            buckets_lag["<30s"] += 1
        elif e <= 90:
            buckets_lag["30-90s (target)"] += 1
        elif e <= 180:
            buckets_lag["90-180s"] += 1
        elif e <= 270:
            buckets_lag["180-270s"] += 1
        else:
            buckets_lag[">270s"] += 1

    print("\n  Entry timing vs reaction-window buckets (traded only):")
    for k, v in buckets_lag.items():
        pct = v / len(elapsed_traded) * 100 if elapsed_traded else 0
        print(f"    {k:<22}: {v:>5,} ({pct:.1f}%)")

    # spot_displacement vs win rate
    print("\n  Win rate by |spot_displacement_pct| for traded windows:")
    cur2 = conn.execute("""
        SELECT spot_displacement_pct, trade_side, settlement_outcome, elapsed_seconds
        FROM windows
        WHERE is_primary=1 AND traded=1 AND settlement_outcome IS NOT NULL
          AND spot_displacement_pct IS NOT NULL
    """)
    spot_rows = cur2.fetchall()

    disp_buckets = defaultdict(lambda: {"n": 0, "wins": 0})
    for (spot_disp, trade_side, outcome, elapsed) in spot_rows:
        abs_d = abs(spot_disp) if spot_disp else 0
        if abs_d < 0.1:
            bk = "<0.1%"
        elif abs_d < 0.3:
            bk = "0.1-0.3%"
        elif abs_d < 0.5:
            bk = "0.3-0.5%"
        elif abs_d < 1.0:
            bk = "0.5-1.0%"
        else:
            bk = ">1.0%"
        won = (outcome == trade_side)
        disp_buckets[bk]["n"] += 1
        disp_buckets[bk]["wins"] += int(won)

    order_disp = ["<0.1%", "0.1-0.3%", "0.3-0.5%", "0.5-1.0%", ">1.0%"]
    for bk in order_disp:
        d = disp_buckets[bk]
        if d["n"] > 0:
            wr = d["wins"] / d["n"]
            print(f"    |spot_disp| {bk:<10}: n={d['n']:>5,}, win_rate={wr:.1%}")


def sensitivity_check(rows):
    """
    Sensitivity: at fee_rate = 0.072 (user's value) vs 0.07 (docs).
    Also check what minimum win-rate threshold would be needed per bucket.
    """
    print(f"\n{'='*70}")
    print("  SENSITIVITY / BREAK-EVEN ANALYSIS")
    print(f"{'='*70}")

    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "entries": []})
    for row in rows:
        (wid, rule_signal, yes_ask, no_ask, mid_price,
         trade_entry, realistic_fill, trade_fill,
         trade_side, traded, settlement_outcome,
         elapsed, snap_time, open_time, model_prob,
         spot_disp, spot_trend) = row

        actual_entry = realistic_fill or trade_fill or trade_entry if traded else None
        actual_side = trade_side if traded else None
        result = compute_trade(rule_signal, yes_ask, no_ask,
                               settlement_outcome, actual_entry, actual_side)
        if result is None:
            continue
        entry, won, gross, fee, net = result
        bl = bucket_label(entry)
        buckets[bl]["n"] += 1
        buckets[bl]["wins"] += int(won)
        buckets[bl]["entries"].append(entry)

    print(f"\n  {'Bucket':<12} {'N':>6} {'ActualWR':>9} {'BreakEven7%':>12} {'BreakEven7.2%':>14} {'Edge(7%)':>9} {'Edge(7.2%)':>11}")
    print("  " + "-" * 70)
    order = ["<0.30", "0.30-0.40", "0.40-0.50", "0.50-0.60",
             "0.60-0.70", "0.70-0.80", "0.80+"]

    for bl in order:
        b = buckets[bl]
        n = b["n"]
        if n == 0:
            continue
        actual_wr = b["wins"] / n
        avg_e = np.mean(b["entries"])
        # break-even WR: W × (1-e)/e - (1-W) - fee_rate×(1-e) = 0
        # W × [1/e] - 1 - fee×(1-e) = 0  → W = e × (1 + fee×(1-e))
        be_7  = avg_e * (1 + 0.07  * (1 - avg_e))
        be_72 = avg_e * (1 + 0.072 * (1 - avg_e))
        edge_7  = actual_wr - be_7
        edge_72 = actual_wr - be_72
        marker = " ◄" if edge_7 > 0 else ""
        print(f"  {bl:<12} {n:>6,} {actual_wr:>8.2%} {be_7:>11.2%} {be_72:>13.2%} "
              f"{edge_7:>+8.2%} {edge_72:>+10.2%}{marker}")


def main():
    conn = sqlite3.connect(DB)

    print("\nPHASE 1 KILL TEST — Fee-Aware Post-Mortem")
    print("Fee model: 0.07 × (1 − entry_price) per $1 stake (taker)")
    print("PnL unit: dollars per $1 stake\n")

    rows = load_primary_signals(conn)
    print(f"Total primary settled signal windows: {len(rows):,}")

    # Split: actual trades vs simulated (untraded primary with signal)
    traded_rows = [r for r in rows if r[9] == 1]    # traded=1
    sim_rows    = [r for r in rows if r[9] == 0]    # traded=0

    print(f"  Actual trades: {len(traded_rows):,}")
    print(f"  Simulated (untraded primary signals): {len(sim_rows):,}")

    # --- Analysis 1: actual trades only ---
    analyze(traded_rows, "ACTUAL TRADES (filled orders)")

    # --- Analysis 2: all primary signals including untraded ---
    analyze(rows, "ALL PRIMARY SIGNALS (traded + simulated)")

    # --- Analysis 3: simulated only (untraded signal windows) ---
    analyze(sim_rows, "SIMULATED ONLY (untraded primary signals)")

    # --- Latency ---
    latency_analysis(conn)

    # --- Sensitivity ---
    sensitivity_check(rows)

    conn.close()


if __name__ == "__main__":
    main()
