"""
Single source of truth for the Polymarket taker fee used in analysis scripts.

Formula mirrors main.py:468:
    taker_fee = 0.072 * fill_price * (1.0 - fill_price) * size_usd

NOTE on other constants in the codebase:
  src/kalbot/engine/decision.py:79   — uses same 0.072 * p * (1-p) formula (as %)
  src/kalbot/main.py:468             — uses same formula for paper settlement
  src/kalbot/ml/backtest.py:31       — uses 0.07 * (1-p) formula (DIFFERENT; old backtest)
  src/kalbot/execution/paper.py:25   — 0.005 fixed slippage (different purpose)

Analysis scripts import from here so the constant drifts in exactly one place.
"""

TAKER_FEE_RATE: float = 0.072


def taker_fee_per_stake(entry_price: float) -> float:
    """Fee per dollar of stake at a given entry price.

    From main.py: fee = 0.072 * entry * (1 - entry) * stake
    Normalised to per-$1: 0.072 * entry * (1 - entry)
    """
    return TAKER_FEE_RATE * entry_price * (1.0 - entry_price)


def net_pnl_per_stake(entry: float, won: bool) -> float:
    """Net PnL per $1 stake after taker fee."""
    gross = (1.0 - entry) / entry if won else -1.0
    return gross - taker_fee_per_stake(entry)


def break_even_win_rate(entry: float) -> float:
    """Minimum win rate to produce zero net PnL at this entry price."""
    fee = taker_fee_per_stake(entry)
    # gross_win = (1-e)/e, gross_lose = -1
    # W*(1-e)/e - (1-W) - fee = 0  →  W/e - 1 - fee = 0  →  W = e*(1 + fee)
    return entry * (1.0 + fee)
