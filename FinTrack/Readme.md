# FinTrack

> **An AI-first personal finance assistant.** Talk to it in plain English — it logs expenses, tracks budgets, manages goals, monitors investments, and gives insights. Built on the Model Context Protocol (MCP) with a microservices backend.

```
You:  "I spent 450 on zomato for dinner"
AI:   "Logged ₹450 for food. You've used 72% of your ₹5,000 food budget
       with 12 days left — pacing is okay but keep an eye on it."
```

Behind that one reply, 3 tools across 2 microservices fired automatically.

---

## 📑 Table of contents

- [What makes this interesting](#-what-makes-this-interesting)
- [Architecture](#-architecture)
- [Tool catalog (30 tools across 5 servers)](#-tool-catalog)
- [Tech stack](#-tech-stack)
- [Quick start](#-quick-start)
- [Testing](#-testing)
- [Documentation](#-documentation)
- [Roadmap](#-roadmap)

---

## ✨ What makes this interesting

- **🤖 Agentic by design.** The LLM reads your intent, picks tools, chains them across services, feeds results back on itself, and composes a natural reply. No scripted flows.
- **🔌 MCP-native.** Each capability (expenses, budgets, goals, investments, insights) is an independent MCP server exposing its tools via `GET /mcp/tools` and `POST /mcp/execute`. New capabilities = new server, no gateway changes.
- **💰 Zero vendor lock-in on the LLM.** OpenAI-compatible gateway — swap between **Groq** (default, free tier), **Ollama** (local, offline), or **Gemini** by changing three env vars.
- **🧠 Cross-service intelligence.** `insights-server` is read-only and aggregates data from expenses + budgets + holdings to compute metrics like financial-health scores — showing how microservices can collaborate via a shared data substrate.
- **🌐 Reaches outside the system.** `get_live_price` pulls real-time stock prices from Yahoo Finance.
- **🧹 Honest state management.** No pretend idempotency — duplicates are logged, the AI gently flags them, and the user has a `delete_transaction` tool to clean up explicitly.

---

## 🏛 Architecture

```
                       ┌────────────────────────┐
   You (chat client) ─► │   ai-gateway  :8000    │ ─► Groq LLM (openai/gpt-oss-20b)
                       │   agentic tool loop    │     [or Ollama, Gemini, ...]
                       └──┬───┬───┬───┬───┬─────┘
                          │   │   │   │   │
                          ▼   ▼   ▼   ▼   ▼
         ┌─────────────────┐  │   │   │  ┌─────────────────┐
         │ expense  :8001  │  │   │   │  │ goals    :8005  │
         │  5 tools        │  │   │   │  │  7 tools        │
         └────────┬────────┘  │   │   │  └────────┬────────┘
                  │           │   │   │           │
         ┌────────┴────────┐  │   │   │  ┌────────┴────────┐
         │ budget   :8002  │  │   │   │  │ invest   :8003  │
         │  7 tools        │  │   │   │  │  6 tools        │
         └────────┬────────┘  │   │   │  └────────┬────────┘
                  │           │   │   │           │
                  │   ┌───────┴───┴───┴───────┐   │
                  │   │ insights :8004        │   │
                  │   │ 5 tools (read-only    │   │
                  │   │ cross-service)        │   │
                  │   └──────────┬────────────┘   │
                  │              │                │
                  └──────────────┼────────────────┘
                                 ▼
                          ┌─────────────┐  ┌──────────┐
                          │ Postgres 16 │  │ Redis 7  │
                          │  5433→5432  │  │  6379    │
                          └─────────────┘  └──────────┘
```

Everything runs locally via Docker Compose — 8 containers total.

---

## 🧰 Tool catalog

**30 tools across 5 MCP servers.** The AI picks what to call based on your message.

### 🧾 expense-server (`:8001`) — 5 tools
| Tool | What it does |
|---|---|
| `log_expense` | Record a spend; returns rich monthly context + `recent_similar` for duplicate awareness |
| `log_income` | Record income (salary, freelance, etc.) |
| `get_expenses` | List recent transactions with optional category filter |
| `get_monthly_summary` | Income, spend, savings rate, category breakdown for current month |
| `delete_transaction` | Remove a transaction by ID — clean up mistakes |

### 💰 budget-server (`:8002`) — 7 tools
| Tool | What it does |
|---|---|
| `set_budget` | Set/update monthly limit for a category |
| `get_budget_status` | Current progress for one category |
| `get_all_budgets` | All budgets at a glance |
| `check_budget_on_expense` | Called automatically after every `log_expense` — returns `alert_level: ok/warning/danger/exceeded` to drive the AI's reply tone |
| `update_budget` | Change limit or threshold |
| `delete_budget` | Remove a budget |
| `get_budget_recommendations` | Suggest reasonable budgets based on past spending |

### 📈 invest-server (`:8003`) — 6 tools
| Tool | What it does |
|---|---|
| `add_holding` | Record a stock / MF holding |
| `get_portfolio` | View all holdings with current value |
| `get_live_price` | **Reaches Yahoo Finance** for real-time price + 52w range |
| `log_sip` | Record a SIP contribution |
| `get_sip_summary` | SIP totals by fund |
| `get_asset_allocation` | % breakdown across stocks / MFs / etc. |

### 🧠 insights-server (`:8004`) — 5 tools (read-only aggregator)
| Tool | What it does |
|---|---|
| `get_spending_trends` | Month-over-month trends by category |
| `detect_anomalies` | Flag unusual spends (e.g. 3× the usual food amount) |
| `get_month_forecast` | Projects month-end spend based on current burn rate |
| `get_net_worth` | Reads transactions + holdings → total net worth |
| `get_financial_health_score` | 0–100 score + grade — cross-service composite metric |

### 🎯 goals-server (`:8005`) — 7 tools
| Tool | What it does |
|---|---|
| `create_goal` | Savings goal (target + deadline) or habit goal (e.g. "food < ₹5000/mo") |
| `get_goals` | List all active goals with progress |
| `add_goal_contribution` | Log money set aside toward a savings goal |
| `get_goal_detail` | Deep-dive on one goal (milestones, projection) |
| `update_goal` | Change target, deadline, or status |
| `complete_goal` | Mark a goal done |
| `get_habit_status` | Called after `log_expense` to flag habit-goal breaches |

---

## 🏗 Tech stack

- **Backend:** Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · asyncpg
- **Database:** PostgreSQL 16 · Redis 7 (cache; health-only today)
- **LLM:** Groq (default `openai/gpt-oss-20b`) · OpenAI-compatible — supports Ollama / Gemini / any `/v1/chat/completions` endpoint
- **Protocol:** [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) — tool discovery + execution
- **External:** Yahoo Finance (`yfinance`) for live prices
- **Infra:** Docker Compose · hot-reload mounted volumes
- **Testing:** Bruno (24 numbered requests, demo-ordered)


---

## 🚀 Quick start

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Colima)
- A free API key from [Groq](https://console.groq.com/) — no credit card needed

### 60-second setup

```bash
git clone https://github.com/AbhishekSharma6903/FinTrack.git
cd FinTrack/FinTrack
cp .env.example .env
# ↳ open .env, paste your Groq API key into AI_API_KEY
docker compose up --build -d
```

Wait ~15 seconds, then verify:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
# Expect: tools_count: 30, model: "openai/gpt-oss-20b"
```

### First real request

```bash
curl -s -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "me",
    "message": "I spent 350 on uber coming home",
    "history": []
  }' | python3 -m json.tool
```

You should get back a natural reply + `tools_used: ["log_expense", "check_budget_on_expense", "get_habit_status"]` — three tools fired across two services from one sentence.

### Switching LLM providers

In `.env`, swap these three variables:

```bash
# Groq (default — free, fast)
AI_BASE_URL=https://api.groq.com/openai/v1
AI_API_KEY=gsk_...
AI_MODEL=openai/gpt-oss-20b

# Ollama (fully local, offline, free)
AI_BASE_URL=http://host.docker.internal:11434/v1
AI_API_KEY=ollama
AI_MODEL=llama3.2

# Gemini (via OpenAI-compat shim)
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
AI_API_KEY=AIza...
AI_MODEL=gemini-2.0-flash
```

Then `docker compose up -d --force-recreate ai-gateway`. Zero code changes.

---

## 🧪 Testing

### Bruno (recommended)
Open the `Bruno/FinTrack/` collection in [Bruno](https://www.usebruno.com/). Requests are numbered for demo order:

- **00–10** — Platform sanity (gateway + per-server health, tool catalogs)
- **20–34** — Agentic `/chat` flow (log, budget, goals, portfolio, insights, multi-turn)
- **50–57** — Direct MCP tool calls (bypass the LLM — for deterministic testing)

### Direct curl
See [`DEMO_RUN_SHEET.md`](./DEMO_RUN_SHEET.md) for curl equivalents of every Bruno request.

### Database peek

```bash
docker compose exec postgres psql -U fintrack_user -d fintrack
# \dt                                      -- list tables
# SELECT * FROM transactions LIMIT 5;      -- peek data
```

---

## 📚 Documentation

| File | For |
|---|---|
| [`LEARN.md`](./LEARN.md) | **Start here if new to this stack.** Beginner-friendly walkthrough of Docker port mapping, SQLAlchemy ORM, MCP architecture. |
| [`PROJECT_DEEP_DIVE.md`](./PROJECT_DEEP_DIVE.md) | 441-line engineering deep-dive — every component explained with code snippets. |
| [`DEMO_RUN_SHEET.md`](./DEMO_RUN_SHEET.md) | Verified step-by-step demo script — Bruno numbers + curl equivalents + expected outputs. |

---

## 🗺 Roadmap

**Completed**
- ✅ 5-server MCP platform (expense, budget, goals, invest, insights)
- ✅ 30 agentic tools with cross-service orchestration
- ✅ OpenAI-compatible gateway (Groq / Ollama / Gemini swappable)
- ✅ Delete-transaction tool for clean user-driven corrections
- ✅ Comprehensive Bruno test collection

**Next up**
- 🚧 **Conversational memory** — persist chat history per user; replay in subsequent turns; enable follow-ups ("and the uber too") and open-ended questions ("what should I cut?")
- 🚧 **Telegram bot** / web UI — log expenses from anywhere, see history, swipe-to-delete
- 🚧 **Alembic migrations** — proper schema versioning
- 🚧 **Scheduled jobs** — monthly summaries, SIP reminders
- 🚧 **RAG over conversation history** — semantic "ask my past self" (when conversation history is long enough to warrant it)

**Future**
- 📦 Deploy to a VPS (Hetzner / Railway / Fly.io)
- 🔐 Multi-user auth
- 📊 Dashboard with charts (net worth timeline, category trends)

---

## 📝 License

MIT — build on top of this, use it personally, hack it apart. Credit appreciated but not required.

---

*Built as a learning project to explore agentic AI, MCP, microservices, and personal-finance automation. If you found this useful or interesting, a ⭐ on the repo would make my day.*

