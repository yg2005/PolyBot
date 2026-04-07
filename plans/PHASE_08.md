# Phase 8 — ML Pipeline

Read CONTEXT.md first. Phase 5 must be complete + enough data collected.

## Build

### 1. Feature Engineering (src/kalbot/ml/features.py)
Feature groups:
- Trajectory: displacement_pct, abs_displacement_pct, direction_consistency, cross_count, time_above_pct, velocity, acceleration, max/min_displacement_pct, distance_from_low
- Market: mid_price, spread, depth_imbalance, market_move_speed
- Confirmation: spot_confirms, spot_displacement_pct, spot_trend_1m, chainlink_spot_divergence
- Timing: elapsed_seconds, remaining_seconds, time_of_day_hour, day_of_week
- Derived: displacement_per_cross, momentum_score (velocity * consistency), market_disagreement

Target: settlement_outcome → 1 if YES, 0 if NO
Method: get_feature_matrix(min_date, max_date) → X, y, feature_names

### 2. Training (src/kalbot/ml/train.py)
- Temporal split (NOT random): train on earliest 75%, validate on latest 25%
- XGBoost: max_depth=4, n_estimators=200, lr=0.05, subsample=0.8, colsample_bytree=0.8, min_child_weight=5
- Walk-forward: rolling windows, train on N, validate on N+1, average AUC
- If AUC >= 0.75: calibrate and save

### 3. Calibration (src/kalbot/ml/calibrate.py)
- Isotonic if n_val > 200, else Platt scaling
- ECE must be < 0.05
- Verify with calibration curve plot

### 4. Evaluation (src/kalbot/ml/evaluate.py)
- AUC-ROC, AUC-PR, calibration curve, feature importance
- Edge distribution vs market price
- Profitability simulation on validation set

### 5. Backtest (src/kalbot/ml/backtest.py)
- Out-of-sample with fill simulation
- Apply fees (VERIFY actual fee schedule — Gamma shows 1000 bps)
- Compute: total P&L, Sharpe, max drawdown, win rate
- KILL CRITERIA: if ROI < 5% on paper bankroll → DO NOT go live

### 6. ML Scorer (src/kalbot/engine/ml_scorer.py)
- Implements BaseScorer. Drop-in replacement.
- Load model + calibrator. predict_proba() → calibrate → compute edge vs market
- Switch via config: engine.scorer = "rules" or "ml"

## Done when
- Training pipeline runs on collected data
- Model evaluates with AUC, calibration, and profitability metrics
- ML scorer slots into main loop via config change
