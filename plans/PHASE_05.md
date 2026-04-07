# Phase 5 — Paper Trading Integration

Read CONTEXT.md first. Phases 1-4 must be complete.

## Build

### 1. Paper Executor (src/kalbot/execution/paper.py)
- Implements OrderManager interface
- Maker order fill: simulate — did market mid cross our price before expiry? Add 50-200ms random latency. ~70% fill rate.
- Taker order: fill immediately at current ask/bid
- Track paper positions, P&L, order history → orders table
- Must check: price moved THROUGH limit price, not just touched it

### 2. Wire main loop (src/kalbot/main.py)
```python
async def run():
    feeds = await start_feeds(config)
    tracker = WindowTracker()
    lifecycle = WindowLifecycleManager(tracker, logger)
    scorer = RuleBasedScorer(config)
    decision = DecisionEngine(config)  # stub for now
    risk = RiskManager(config)         # stub for now
    executor = PaperExecutor(config)
    logger = WindowLogger(db)

    async for feed_state in feeds.updates():
        await lifecycle.on_market_update(feed_state)
        tracker.update(feed_state.chainlink_price, feed_state.timestamp)
        await logger.log_tick(feed_state)

        elapsed = lifecycle.elapsed_seconds
        if elapsed in config.data.snapshot_at_seconds:
            snapshot = build_snapshot(tracker, feed_state)
            await logger.log_snapshot(snapshot)

        if 60 <= elapsed <= 270 and not already_traded(lifecycle.current_market):
            snapshot = build_snapshot(tracker, feed_state)
            score = await scorer.score(snapshot)
            if score.signal != "PASS":
                # stub decision: trade if edge > 0
                await executor.place_order(...)
                snapshot.traded = True
            await logger.log_snapshot(snapshot)
```

### 3. Settlement handler
```python
async def settle_window(window, logger, executor):
    settlement_price = feeds.chainlink.latest_price
    outcome = "YES" if settlement_price > window.strike else "NO"
    await logger.settle(window.id, outcome, settlement_price)
    await executor.settle_positions(window.id, outcome)
```

### 4. End-to-end verification
Print every window: `Window 12:05 | BTC +0.12% | Consistency: 0.78 | Signal: YES | TRADE @ $0.58 | Outcome: YES | P&L: +$0.42`

## Done when
- Bot runs continuously, connects all feeds, tracks windows
- Logs ALL windows to DB
- Places paper orders when rules trigger
- Settles and computes P&L correctly
- Window tracker resets on every market transition (elapsed never >300)
