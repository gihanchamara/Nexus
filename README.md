# Nexus — AI-First Quantitative Trading Platform

> An event-driven, modular, AI-first quantitative trading platform built on IBKR data feeds,
> with full backtesting, paper/live trading, real-time observability, and a MySQL persistence layer
> managed by Liquibase.

---

## Table of Contents

1. [Design Principles](#design-principles)
2. [System Context](#1-system-context-c4-level-1)
3. [Container Architecture](#2-container-architecture-c4-level-2)
4. [Data Flow](#3-data-flow)
5. [Event Bus & Component Telemetry](#4-event-bus--component-telemetry)
6. [Data Ingestion Layer](#5-data-ingestion-layer)
7. [Strategy & AI Engine](#6-strategy--ai-engine)
8. [Order Management System](#7-order-management-system-oms)
9. [Backtesting Engine](#8-backtesting-engine)
10. [Database Schema Overview](#9-database-schema-overview)
11. [Security Model](#10-security-model)
12. [Technology Stack](#technology-stack)
13. [Known Constraints & Risks](#known-constraints--risks)
14. [Development Phases](#development-phases)

---

## Design Principles

| Principle | Description |
|-----------|-------------|
| **AI-First** | Every module exposes parameters consumable by AI agents; strategy logic is ML-replaceable |
| **Modular** | Each component is independently deployable and observable |
| **Data-Driven** | All component state/config is persisted and driven from data, never hardcoded |
| **Event-Driven** | Components communicate via an in-memory event bus; no direct coupling |
| **Observable** | Every component emits timestamped telemetry events; UI is a passive consumer |
| **Safe by Default** | Paper trading is the default; live trading requires explicit multi-step activation |
| **Schema-Managed** | All DB changes via Liquibase changesets, never manual |

---

## 1. System Context (C4 Level 1)

```mermaid
C4Context
    title Nexus — System Context

    Person(trader, "Quant Trader / AI Agent", "Configures strategies, monitors positions, approves orders")

    System(nexus, "Nexus Platform", "AI-first quant trading platform. Ingests market data, runs strategies, manages orders, backtests algorithms.")

    System_Ext(ibkr, "IBKR / TWS Gateway", "Primary broker. Provides live market data, historical data, order execution via ib_insync.")
    System_Ext(news, "Market News APIs", "Financial news feeds (e.g., Benzinga, Polygon, NewsAPI)")
    System_Ext(altdata, "Alternative Data", "Sentiment, macro, SEC filings, etc.")
    System_Ext(mysql, "MySQL Database", "Relational persistence: orders, positions, strategy config, audit log")

    Rel(trader, nexus, "Configures & monitors", "HTTPS / WebSocket")
    Rel(nexus, ibkr, "Market data & order routing", "ib_insync / TCP")
    Rel(nexus, news, "News ingestion", "REST / WebSocket")
    Rel(nexus, altdata, "Alt data feeds", "REST")
    Rel(nexus, mysql, "Persist state", "SQL over TLS")
```

---

## 2. Container Architecture (C4 Level 2)

```mermaid
C4Container
    title Nexus — Container Architecture

    Person(user, "Trader / AI Agent")

    Container_Boundary(nexus, "Nexus Platform") {

        Container(gateway, "API Gateway", "FastAPI", "REST + WebSocket endpoints. Auth, rate limiting.")
        Container(ui, "Dashboard UI", "React / Streamlit", "Real-time parameter display, telemetry stream, order blotter")
        Container(bus, "Event Bus", "Redis Streams", "In-memory pub/sub backbone. All components publish timestamped events here.")
        Container(ingestion, "Data Ingestion Service", "Python / ib_insync", "Connects to IBKR TWS/Gateway. Streams ticks, bars, news.")
        Container(strategy, "Strategy Engine", "Python", "Hosts and executes trading strategies. Emits signals.")
        Container(ai, "AI / ML Engine", "Python / PyTorch / sklearn", "Model training, inference, feature engineering. Feeds signals to Strategy Engine.")
        Container(oms, "Order Management System", "Python", "Receives signals, applies risk checks, routes to broker or paper engine.")
        Container(paper, "Paper Trading Engine", "Python", "Simulates order fills using live market data. No real money.")
        Container(backtest, "Backtesting Engine", "Python / VectorBT / Zipline-reloaded", "Replays historical data through strategies. Generates performance reports.")
        Container(risk, "Risk Manager", "Python", "Position limits, drawdown guards, VaR, kill switch.")
        Container(telemetry, "Telemetry Aggregator", "Python", "Subscribes to all event bus channels. Aggregates, persists, exposes via WebSocket.")
        Container(scheduler, "Scheduler / Cron", "APScheduler", "Triggers data pulls, model retraining, report generation.")
        Container(dbmgr, "DB Migration Manager", "Liquibase", "Manages MySQL schema via versioned changesets.")
    }

    System_Ext(ibkr, "IBKR TWS / Gateway")
    System_Ext(newsapi, "News APIs")
    System_Ext(mysql, "MySQL DB")
    System_Ext(timeseries, "Time-Series Store (optional)", "TimescaleDB or InfluxDB for tick-level data")

    Rel(user, gateway, "REST / WS", "HTTPS")
    Rel(user, ui, "Browser / CLI", "HTTPS / WS")

    Rel(gateway, bus, "Publish commands")
    Rel(ui, telemetry, "Subscribe to telemetry", "WebSocket")

    Rel(ingestion, ibkr, "ib_insync", "TCP")
    Rel(ingestion, newsapi, "REST / WS")
    Rel(ingestion, bus, "Publish market data events")

    Rel(bus, strategy, "Market data events")
    Rel(strategy, ai, "Feature requests / inference")
    Rel(strategy, bus, "Publish signals")

    Rel(bus, oms, "Signal events")
    Rel(oms, risk, "Risk check")
    Rel(oms, paper, "Paper orders")
    Rel(oms, ibkr, "Live orders (guarded)")
    Rel(oms, bus, "Publish order events")

    Rel(bus, backtest, "Historical data events (replay mode)")
    Rel(backtest, bus, "Publish backtest results")

    Rel(telemetry, bus, "Subscribe all channels")
    Rel(telemetry, mysql, "Persist telemetry")
    Rel(telemetry, ui, "Push telemetry", "WebSocket")

    Rel(dbmgr, mysql, "Schema migrations")
    Rel(ingestion, timeseries, "Write ticks/bars")
    Rel(strategy, timeseries, "Read OHLCV / tick history")
    Rel(strategy, mysql, "Read/write strategy config")
    Rel(oms, mysql, "Persist orders, fills, positions")
    Rel(scheduler, bus, "Trigger events")
```

---

## 3. Data Flow

```mermaid
flowchart TD
    subgraph Sources["Data Sources"]
        IBKR["IBKR TWS/Gateway\n(ib_insync)"]
        NEWS["Market News APIs"]
        ALT["Alternative Data"]
        HIST["Historical Data\n(IBKR / CSV / Polygon)"]
    end

    subgraph Ingestion["Data Ingestion Layer"]
        TICK["Tick/Bar Streamer"]
        NEWSING["News Ingester"]
        HISTPULL["Historical Data Puller\n(rate-limited: ~60 req/min IBKR)"]
    end

    subgraph Bus["Event Bus (Redis Streams)"]
        CH_TICK["channel: market.tick"]
        CH_BAR["channel: market.bar"]
        CH_NEWS["channel: market.news"]
        CH_SIG["channel: strategy.signal"]
        CH_ORD["channel: order.event"]
        CH_TEL["channel: telemetry.*"]
    end

    subgraph Strategy["Strategy & AI Engine"]
        FE["Feature Engineering"]
        ML["ML Model Inference"]
        STRAT["Strategy Logic"]
        SIG["Signal Generator"]
    end

    subgraph Execution["Execution Layer"]
        RISK["Risk Manager"]
        OMS["Order Router"]
        PAPER["Paper Engine"]
        LIVE["Live Broker\n(guarded by safety switch)"]
    end

    subgraph Persistence["Persistence"]
        TS["Time-Series Store\n(ticks, OHLCV bars)"]
        MYSQL["MySQL\n(orders, positions,\nstrategy config, audit)"]
    end

    subgraph Observability["Observability"]
        TEL["Telemetry Aggregator"]
        UI["Dashboard UI"]
    end

    IBKR --> TICK
    IBKR --> HISTPULL
    NEWS --> NEWSING
    ALT --> NEWSING
    HIST --> HISTPULL

    TICK --> CH_TICK
    TICK --> CH_BAR
    TICK --> TS
    NEWSING --> CH_NEWS
    HISTPULL --> TS

    CH_TICK --> FE
    CH_BAR --> FE
    CH_NEWS --> FE
    FE --> ML
    ML --> STRAT
    STRAT --> SIG
    SIG --> CH_SIG

    CH_SIG --> RISK
    RISK --> OMS
    OMS --> PAPER
    OMS --> LIVE

    PAPER --> CH_ORD
    LIVE --> CH_ORD
    CH_ORD --> MYSQL

    CH_TEL --> TEL
    TEL --> UI
    TEL --> MYSQL

    STRAT --> CH_TEL
    OMS --> CH_TEL
    RISK --> CH_TEL
    PAPER --> CH_TEL
    TICK --> CH_TEL
```

---

## 4. Event Bus & Component Telemetry

Every component emits a standardized telemetry event. The UI consumes these passively.

### Telemetry Event Schema

```mermaid
classDiagram
    class TelemetryEvent {
        +String event_id
        +String component_id
        +String component_type
        +DateTime timestamp_utc
        +String level        %%  INFO | WARN | ERROR | DEBUG
        +String channel      %%  Redis stream channel name
        +Dict  payload       %%  Component-specific parameters
        +String session_id   %%  Trading session ID
        +String mode         %%  PAPER | LIVE | BACKTEST
    }

    class ComponentTypes {
        INGESTION
        STRATEGY
        AI_MODEL
        RISK_MANAGER
        ORDER_MANAGER
        PAPER_ENGINE
        BACKTEST_ENGINE
        SCHEDULER
    }

    TelemetryEvent --> ComponentTypes
```

### Redis Streams Channel Map

```mermaid
mindmap
  root((Event Bus\nRedis Streams))
    market
      market.tick
      market.bar.1m
      market.bar.5m
      market.bar.1d
      market.news
      market.halt
    strategy
      strategy.signal
      strategy.param_update
      strategy.state
    order
      order.submitted
      order.filled
      order.cancelled
      order.rejected
    risk
      risk.breach
      risk.killswitch
    telemetry
      telemetry.ingestion
      telemetry.strategy
      telemetry.oms
      telemetry.ai
      telemetry.backtest
    system
      system.heartbeat
      system.error
      system.mode_change
```

---

## 5. Data Ingestion Layer

```mermaid
flowchart TD
    subgraph IBKR_Conn["IBKR Connection Manager"]
        TWS["TWS / IB Gateway\n(port 7497 paper | 7496 live)"]
        IB["ib_insync client\n(asyncio event loop)"]
        WATCHDOG["Connection Watchdog\n(auto-reconnect)"]
    end

    subgraph MarketData["Market Data Streams"]
        LIVE_TICK["Live Tick Streamer\nreqMktData()"]
        LIVE_BAR["Real-Time Bars\nreqRealTimeBars() — 5s bars"]
        HIST_BAR["Historical Bars\nreqHistoricalData()\nrate-limited: 60 req/10s"]
        NEWS_BUL["News Headlines\nreqNewsBulletins()"]
    end

    subgraph Contracts["IBKR Contract Types\n(ib_insync data structures)"]
        STK["Stock\nContract(symbol, secType='STK')"]
        OPT["Option\nContract(secType='OPT')"]
        FUT["Future\nContract(secType='FUT')"]
        FX["Forex\nContract(secType='CASH')"]
    end

    subgraph Output["Output"]
        TS_WRITE["Write to Time-Series Store"]
        BUS_PUB["Publish to Event Bus"]
    end

    TWS <-->|TCP| IB
    WATCHDOG --> IB
    IB --> LIVE_TICK
    IB --> LIVE_BAR
    IB --> HIST_BAR
    IB --> NEWS_BUL

    Contracts --> IB

    LIVE_TICK --> TS_WRITE
    LIVE_TICK --> BUS_PUB
    LIVE_BAR --> TS_WRITE
    LIVE_BAR --> BUS_PUB
    HIST_BAR --> TS_WRITE
    NEWS_BUL --> BUS_PUB
```

**IBKR Data Limits to design around:**
- Paper account: 100 simultaneous market data lines
- Historical data: ~60 requests / 10 minutes
- Real-time bars: 5-second granularity minimum via API

---

## 6. Strategy & AI Engine

```mermaid
flowchart TD
    subgraph StrategyEngine["Strategy Engine"]
        direction TB
        REG["Strategy Registry\n(DB-persisted config)"]
        LOADER["Strategy Loader\n(dynamic import)"]
        RUNNER["Strategy Runner\n(asyncio per-strategy)"]
        PARAM["Parameter Store\n(displayable, hot-reloadable)"]
    end

    subgraph StrategyInterface["Strategy Interface (ABC)"]
        INIT["on_init(params: dict)"]
        BAR["on_bar(bar: BarData)"]
        TICK2["on_tick(tick: TickData)"]
        NEWS2["on_news(news: NewsEvent)"]
        SIG2["emit_signal(signal: Signal)"]
    end

    subgraph AIEngine["AI / ML Engine"]
        FE2["Feature Engineering\n(technical indicators, NLP embeddings)"]
        MODEL["Model Registry\n(versioned, DB-backed)"]
        INFER["Inference Service\n(async, non-blocking)"]
        TRAIN["Training Pipeline\n(offline / scheduled)"]
        EVAL["Model Evaluator\n(Sharpe, accuracy, etc.)"]
    end

    subgraph Signal["Signal Schema"]
        SIGOBJ["Signal {
  symbol: str
  direction: BUY | SELL | HOLD
  confidence: float 0..1
  strategy_id: str
  model_version: str
  timestamp_utc: datetime
  metadata: dict
}"]
    end

    REG --> LOADER
    LOADER --> RUNNER
    PARAM --> RUNNER
    RUNNER --> StrategyInterface
    StrategyInterface --> FE2
    FE2 --> INFER
    MODEL --> INFER
    INFER --> SIG2
    SIG2 --> Signal
    TRAIN --> MODEL
    EVAL --> MODEL
```

---

## 7. Order Management System (OMS)

```mermaid
flowchart TD
    subgraph Input["Inputs"]
        SIG_IN["Signal from Strategy Engine"]
        MANUAL["Manual Order (API / UI)"]
    end

    subgraph Safety["Safety Layer (CRITICAL)"]
        MODE{"Trading Mode?"}
        KILL{"Kill Switch\nActive?"}
        AUTH{"Order Authorized?"}
    end

    subgraph RiskChecks["Risk Manager"]
        POS_LIM["Position Limit Check"]
        DRAWDOWN["Max Drawdown Check"]
        CONC["Concentration Limit"]
        BUYING_PWR["Buying Power Check"]
        SIZE["Position Sizer\n(Kelly / Fixed / ATR-based)"]
    end

    subgraph OrderTypes["Order Construction"]
        MKT["Market Order"]
        LMT["Limit Order"]
        STP["Stop Order"]
        BRACKET["Bracket Order\n(entry + TP + SL)"]
    end

    subgraph Execution["Execution"]
        PAPER_EX["Paper Engine\n(simulated fills)"]
        LIVE_EX["IBKR Live\n(ib_insync placeOrder)"]
    end

    subgraph Persistence["Persist"]
        DB_ORD["orders table"]
        DB_FILL["fills table"]
        DB_POS["positions table"]
    end

    SIG_IN --> MODE
    MANUAL --> AUTH
    AUTH --> MODE

    MODE -->|PAPER| PAPER_EX
    MODE -->|LIVE - requires explicit activation| KILL
    KILL -->|Active| BLOCKED["ORDER BLOCKED\nEmit risk.killswitch event"]
    KILL -->|Inactive| RiskChecks

    RiskChecks --> POS_LIM
    POS_LIM --> DRAWDOWN
    DRAWDOWN --> CONC
    CONC --> BUYING_PWR
    BUYING_PWR --> SIZE
    SIZE --> OrderTypes

    OrderTypes --> PAPER_EX
    OrderTypes --> LIVE_EX

    PAPER_EX --> DB_ORD
    LIVE_EX --> DB_ORD
    DB_ORD --> DB_FILL
    DB_FILL --> DB_POS
```

---

## 8. Backtesting Engine

```mermaid
flowchart TD
    subgraph Config["Backtest Configuration"]
        BCONF["BacktestConfig {
  strategy_id: str
  symbol_list: List[str]
  start_date: date
  end_date: date
  initial_capital: float
  commission_model: str
  slippage_model: str
  data_frequency: '1m' | '1d'
  mode: VECTORIZED | EVENT_DRIVEN
}"]
    end

    subgraph DataReplay["Historical Data Replay"]
        LOAD["Load from Time-Series Store\nor CSV / Parquet"]
        NORM["Normalize to BarData schema\n(IBKR-compatible structs)"]
        REPLAY["Event Replay Engine\n(simulates real-time feed)"]
    end

    subgraph Execution2["Simulated Execution"]
        STRAT_BT["Strategy Instance\n(same code as live!)"]
        FILL_SIM["Fill Simulator\n(VWAP / next-bar / mid-price)"]
        COMM["Commission Calculator"]
        SLIP["Slippage Model"]
    end

    subgraph Results["Performance Analytics"]
        EQUITY["Equity Curve"]
        METRICS["Metrics {
  total_return, CAGR
  Sharpe, Sortino, Calmar
  Max Drawdown, Recovery Time
  Win Rate, Profit Factor
  Avg Trade Duration
}"]
        TRADES["Trade Log"]
        REPORT["HTML / JSON Report"]
    end

    Config --> DataReplay
    LOAD --> NORM
    NORM --> REPLAY
    REPLAY --> STRAT_BT
    STRAT_BT --> FILL_SIM
    FILL_SIM --> COMM
    COMM --> SLIP
    SLIP --> Results
    Results --> EQUITY
    Results --> METRICS
    Results --> TRADES
    Results --> REPORT
```

---

## 9. Database Schema Overview

> All schema changes managed via **Liquibase** changesets in `db/changelogs/`.

```mermaid
erDiagram
    STRATEGY {
        int id PK
        varchar name
        varchar class_path
        json parameters
        enum status
        varchar version
        datetime created_at
        datetime updated_at
    }

    MODEL_REGISTRY {
        int id PK
        int strategy_id FK
        varchar model_name
        varchar version
        varchar artifact_path
        json hyperparameters
        json eval_metrics
        enum status
        datetime trained_at
    }

    SYMBOL {
        varchar symbol PK
        varchar name
        enum sec_type
        varchar exchange
        varchar currency
        int conid
    }

    TRADING_SESSION {
        int id PK
        enum mode
        datetime started_at
        datetime ended_at
        float initial_capital
        json config_snapshot
    }

    ORDER_EVENT {
        int id PK
        int session_id FK
        int strategy_id FK
        varchar symbol FK
        int ib_order_id
        enum side
        enum order_type
        float quantity
        float limit_price
        float stop_price
        enum status
        json ib_order_state
        datetime submitted_at
        datetime updated_at
    }

    FILL {
        int id PK
        int order_id FK
        float fill_qty
        float fill_price
        float commission
        datetime filled_at
        enum execution_source
    }

    POSITION {
        int id PK
        int session_id FK
        varchar symbol FK
        float quantity
        float avg_cost
        float unrealized_pnl
        float realized_pnl
        datetime updated_at
    }

    BACKTEST_RUN {
        int id PK
        int strategy_id FK
        date start_date
        date end_date
        float initial_capital
        json config
        json metrics
        datetime run_at
    }

    TELEMETRY_EVENT {
        int id PK
        varchar event_id
        varchar component_id
        varchar component_type
        varchar channel
        enum level
        varchar mode
        json payload
        datetime timestamp_utc
    }

    CREDENTIAL {
        int id PK
        varchar service_name
        varchar username
        text encrypted_secret
        varchar vault_key_id
        datetime created_at
        datetime rotated_at
    }

    STRATEGY ||--o{ ORDER_EVENT : "generates"
    STRATEGY ||--o{ MODEL_REGISTRY : "has"
    STRATEGY ||--o{ BACKTEST_RUN : "tested_by"
    ORDER_EVENT ||--o{ FILL : "results_in"
    SYMBOL ||--o{ ORDER_EVENT : "traded_as"
    SYMBOL ||--o{ POSITION : "held_as"
    TRADING_SESSION ||--o{ ORDER_EVENT : "contains"
    TRADING_SESSION ||--o{ POSITION : "tracks"
```

---

## 10. Security Model

```mermaid
flowchart TD
    subgraph Secrets["Secret Management"]
        VAULT["HashiCorp Vault\nor AWS Secrets Manager\n(NEVER store plaintext credentials)"]
        ENV["Environment Variables\n(runtime injection only)"]
        CRED_DB["credentials table\n(encrypted_secret only,\nkey in Vault)"]
    end

    subgraph Auth["Authentication & Authorization"]
        JWT["JWT Tokens\n(short-lived, 15min access)"]
        REFRESH["Refresh Tokens\n(httpOnly cookie)"]
        RBAC["Role-Based Access\nVIEWER | OPERATOR | ADMIN | AI_AGENT"]
    end

    subgraph TradingGuards["Trading Safety Guards"]
        MODE_LOCK["Mode Lock\n(PAPER is default;\nLIVE requires:\n1. Admin role\n2. Explicit activation\n3. Confirmation token)"]
        KILL_SW["Hardware Kill Switch\n(env var: NEXUS_KILL=1\nstops all live orders)"]
        ORDER_LIMIT["Order Size Limits\n(config-driven, per-strategy)"]
    end

    subgraph Network["Network Security"]
        TLS["TLS everywhere\n(API, DB, broker connections)"]
        IBKR_IP["IBKR IP Whitelist\n(TWS/Gateway config)"]
        AUDIT["Audit Log\n(all order events, config changes,\nauth events — immutable append)"]
    end

    Secrets --> Auth
    Auth --> TradingGuards
    TradingGuards --> Network
```

---

## Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Broker Integration | `ib_insync` (Python) | Best async Python client for IBKR; uses asyncio natively |
| API Backend | FastAPI (Python) | Async, auto-OpenAPI docs, WebSocket support |
| Event Bus | Redis Streams | **Confirmed.** Kafka-compatible patterns without broker ops overhead; upgrade path to Kafka if scale demands |
| Relational DB | MySQL 8.x | Orders, positions, strategy config, audit |
| Time-Series DB | TimescaleDB (Postgres extension) | High-performance OHLCV/tick storage; SQL-compatible. **Confirmed primary time-series store.** |
| Schema Management | Liquibase | Versioned, rollback-capable DB migrations |
| Strategy Framework | Custom ABC + VectorBT / Zipline-reloaded | Backtesting; custom ABC for live |
| ML/AI | PyTorch, scikit-learn, LangChain | Model training and LLM-based signal generation |
| Scheduler | APScheduler | Cron-like scheduling within Python |
| Dashboard | React + Recharts / Streamlit (prototype) | Real-time charts, order blotter, telemetry feed |
| Container | Docker + Docker Compose | Local dev; K8s-ready for production |
| Secrets | HashiCorp Vault (or AWS Secrets Manager) | Never store credentials in config files |

---

## Known Constraints & Risks

| # | Constraint | Mitigation |
|---|-----------|-----------|
| 1 | IBKR historical data rate limit (~60 req/10min) | Queue + throttle requests; pre-cache in time-series DB |
| 2 | IBKR paper account: 100 market data line limit | Symbol watchlist management; prioritize active symbols |
| 3 | MySQL is not ideal for tick-level time-series | Use TimescaleDB for OHLCV/tick data; MySQL for relational state |
| 4 | Single point of failure: Redis | Use Redis Sentinel or Cluster for production; acceptably simple for dev |
| 5 | Live trading safety | PAPER mode default; LIVE requires admin + explicit token activation |
| 6 | Model overfitting in backtest | Walk-forward validation, out-of-sample test sets mandatory |
| 7 | ib_insync requires TWS/Gateway running | Monitor TWS uptime; implement reconnect watchdog |

---

## Development Phases

```mermaid
gantt
    title Nexus Development Phases
    dateFormat  YYYY-MM
    section Phase 1 - Foundation
    DB Schema + Liquibase setup        :p1a, 2026-03, 2w
    IBKR connection + data ingestion   :p1b, after p1a, 3w
    Redis Streams event bus            :p1c, after p1a, 2w
    Telemetry aggregator + basic UI    :p1d, after p1c, 2w

    section Phase 2 - Strategy Core
    Strategy engine + ABC interface    :p2a, after p1d, 3w
    OMS + paper trading engine         :p2b, after p2a, 3w
    Risk manager                       :p2c, after p2b, 2w

    section Phase 3 - Backtesting
    Backtesting engine                 :p3a, after p2c, 3w
    Performance analytics + reporting  :p3b, after p3a, 2w

    section Phase 4 - AI/ML
    Feature engineering pipeline       :p4a, after p3b, 3w
    ML model training pipeline         :p4b, after p4a, 3w
    LLM-based signal generation        :p4c, after p4b, 4w

    section Phase 5 - Production
    Security hardening + Vault         :p5a, after p4c, 2w
    Live trading activation (guarded)  :p5b, after p5a, 3w
    Production deployment (Docker/K8s) :p5c, after p5b, 2w
```

---

## Directory Structure (Planned)

```
nexus/
├── db/
│   ├── changelogs/           # Liquibase changesets
│   │   ├── 001-initial.xml
│   │   └── ...
│   └── liquibase.properties
├── nexus/
│   ├── ingestion/            # Data ingestion services
│   │   ├── ibkr_connector.py
│   │   ├── news_ingester.py
│   │   └── historical_puller.py
│   ├── strategy/             # Strategy engine + ABC
│   │   ├── base.py           # Strategy ABC
│   │   ├── registry.py
│   │   └── strategies/       # Concrete strategy implementations
│   ├── ai/                   # ML/AI engine
│   │   ├── features.py
│   │   ├── model_registry.py
│   │   └── models/
│   ├── oms/                  # Order management
│   │   ├── order_manager.py
│   │   ├── paper_engine.py
│   │   └── risk_manager.py
│   ├── backtest/             # Backtesting engine
│   │   ├── engine.py
│   │   └── analytics.py
│   ├── bus/                  # Event bus
│   │   ├── publisher.py
│   │   └── consumer.py
│   ├── telemetry/            # Telemetry aggregation
│   │   └── aggregator.py
│   ├── api/                  # FastAPI routes
│   │   ├── main.py
│   │   ├── auth.py
│   │   └── routes/
│   └── core/                 # Shared schemas, config, utils
│       ├── schemas.py        # Pydantic models (BarData, Signal, TelemetryEvent...)
│       ├── config.py
│       └── security.py
├── ui/                       # React dashboard
├── tests/
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

---

> **Next Step:** Begin with Phase 1 — Liquibase schema setup + IBKR connection manager.
> Run `docker-compose up` to start MySQL, Redis, and TimescaleDB locally.
