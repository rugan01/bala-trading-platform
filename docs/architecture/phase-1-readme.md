# Phase 1 Blueprint

This folder defines the initial build for a broker-agnostic trading platform focused on the Indian market.

Phase 1 scope:
- Instruments: NSE cash equities and index futures
- Trading style: passive swing/position trading
- Primary timeframe: daily
- Broker at launch: Upstox
- Broker architecture: adapter-based, configuration-driven
- Deployment path: research -> backtest -> paper trading -> controlled live trading

Design goals:
- Keep the core system independent from any broker API
- Store all critical trading state internally instead of relying on broker history
- Make journaling and post-trade review part of the core platform
- Optimize for robustness, explainability, and low operational overhead

Documents:
- `architecture.md`: system architecture, boundaries, and configuration model
- `data-model.md`: database tables and storage responsibilities
- `implementation-plan.md`: exact build order and delivery milestones

Phase 1 non-goals:
- Options strategy research or live deployment
- Intraday scalping
- Multi-broker smart routing
- Auto-optimization of strategy parameters in production
- Full portfolio margin optimization

Recommended first strategy family:
- Daily trend-following or regime-filtered swing trading on liquid equities
- Optional extension: index futures after the cash-equity workflow is stable
