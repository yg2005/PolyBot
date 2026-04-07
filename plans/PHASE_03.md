# Phase 3 — Window Tracker & Signal Engine

Read CONTEXT.md first. Phases 1-2 must be complete.

## Build

### 1. WindowLifecycleManager (src/kalbot/engine/window_tracker.py)

THIS PREVENTS THE #1 BUG. The old bot defined reset() but NEVER CALLED IT. 82% of training data was corrupted.

```python
class WindowLifecycleManager:
    """ONLY component that calls tracker.reset(). Triggered by market discovery."""
    def on_market_discovered(self, market):
        if market.condition_id != self.current_market_id:
            # settle old window, reset tracker with new open price
            self.tracker.reset(chainlink_price, datetime.utcnow())
    def check_expiry(self):
        if now > self.current_end_time:
            # settle and clear — no active window until next discovery
```

RUNTIME ASSERTION: if elapsed_seconds > 330 at any point → log CRITICAL, block trading, continue logging.

### 2. WindowTracker (same file)

State (reset every new window):
- open_price, price_history: deque(maxlen=300), cross_events, max_price, min_price, open_detected_at

get_features() → WindowFeatures | None (None if insufficient data):
- btc_move_since_open: current - open (raw $)
- btc_move_pct: (current - open) / open * 100
- displacement_pct: same as btc_move_pct
- abs_displacement_pct: abs value
- direction: 1 above, -1 below, 0 at open
- direction_consistency: from LAST 20 SAMPLES — count moves in dominant direction / total moves. NOT time-weighted.
- cross_count: times price crossed open_price
- time_above_pct: SAMPLE-BASED — sum(p > open for p in prices) / len(prices)
- max_displacement_pct, min_displacement_pct
- distance_from_low: (current - min) / (max - min), 0.5 if range is 0
- velocity: displacement_pct / elapsed_seconds
- acceleration: (velocity_now - velocity_30s_ago) / 30
- elapsed_seconds: time since open_detected_at
- momentum_slope_1min: linear regression slope of last 60 prices

### 3. Rule-Based Scorer (src/kalbot/engine/scorer.py)

Implements BaseScorer. 17-step waterfall — ALL must pass:
1. Market is BTC5M target (series_ticker matches)
2. Chainlink feed fresh (<10s), else spot fallback
3. remaining_seconds >= 30
4. elapsed_seconds <= 270
5. Window tracker has data (not None)
6. elapsed_seconds >= 60
7. cross_count <= 6
8. abs(btc_move_pct) >= 0.02
9. direction_consistency >= 0.60
10. yes_bid >= 0.20 AND yes_bid <= 0.80
11. If YES: time_above_pct >= 0.55
12. If NO: time_above_pct <= 0.45
13. If YES: spot_trend_1m >= -0.08%
14. If NO: spot_trend_1m <= +0.08%

Direction: btc_move_since_open > 0 → YES, < 0 → NO

Edge: `move_strength = min(1.0, abs(btc_move_pct) / 0.3)`, `consistency_bonus = max(0.0, direction_consistency - 0.60)`, `edge = 0.003 + move_strength * 0.015 + consistency_bonus * 0.008`

Confidence: `strength = min(1.0, move_strength * 0.6 + direction_consistency * 0.4)`, `conf = quality_confidence(strength)` → ~0.55–0.75. TTC boost: <1.5min: *=1.15, <3.0: *=1.08. Cap 0.95.

### 4. Tests
- test_window_tracker.py: uptrend, downtrend, choppy, flat, at-open edge case
- test_scorer.py: all 17 threshold gates with fixtures

## Done when
- WindowTracker computes correct features from synthetic price series
- Scorer produces YES/NO/PASS signals matching the threshold logic
- WindowLifecycleManager resets tracker on market transition
- elapsed_seconds never exceeds 300 in normal operation
