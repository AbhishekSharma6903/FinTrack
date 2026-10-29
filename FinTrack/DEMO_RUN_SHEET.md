# FinTrack — Demo Run Sheet

> Everything you need to test the whole project end-to-end and run a clean live demo.
> Bruno collection lives in `Bruno/FinTrack/`; curl equivalents are below each step.
> **Verified live** against the running stack (all 8 containers up) on the date this was written.

---

## 0. Pre-flight (once, before the demo)

```bash
cd "/Users/i749349/Desktop/MCP PROJECT/FinTrack"

# Are all 8 containers up? (gateway + 5 servers + postgres + redis)
docker compose ps

# If not up (or after an env change), bring them up / recreate:
docker compose up -d
#   NOTE: env var change (e.g. AI_MODEL)? use `up -d` (recreates), NOT `restart`.

# Optional: watch the gateway print tool calls live during the demo
docker compose logs -f ai-gateway
```

**Ports:** gateway `8000` · expense `8001` · budget `8002` · invest `8003` · insights `8004` · goals `8005` · Postgres `5433→5432` · Redis `6379`.

**Request body shape for `/chat` (current code — no `api_key`):**
```json
{ "user_id": "u_demo", "message": "…", "history": [] }
```

> ⚠️ Your OLD `.bru` files (`Log Expense`, `Create Goal`, `Test 1`, etc.) still send
> `"api_key": "{{GEMINI_KEY}}"`. That field was removed from the gateway — it's ignored now,
> so they still work, but the **new numbered files** (`00 –` … `57 –`) are the current, correct set.
> Use the new ones for the demo.

---

## 1. Bruno run order (click top-to-bottom)

The files are numbered so they sort in demo order in Bruno's sidebar:

| Phase | Files | Purpose |
|---|---|---|
| **A — Init / Sanity** | `00`–`10` | Prove the platform is alive: gateway health (tool count), each server's tool catalog, each server's health |
| **B — Agentic /chat flow** | `20`–`34` | The story: log income → expense chain → budget → over-budget → goals → investments → insights → multi-turn |
| **C — Direct MCP (no LLM)** | `50`–`57` | Deterministic fallback: call tools directly if the model is flaky live |

---

## 2. Phase A — Init / Sanity (VERIFIED)

### 00 — Gateway Health
```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```
**Expect (verified):** `status: "ok"`, **`tools_count: 29`**, `model: "openai/gpt-oss-20b"`, and a `tools` array of all 29 names. *This is your opening "the platform is up and discovered all 5 servers" slide.*

### 01–05 — Tool catalogs per server
```bash
curl -s http://localhost:8001/mcp/tools | python3 -m json.tool   # expense  → 4 tools
curl -s http://localhost:8002/mcp/tools | python3 -m json.tool   # budget   → 7 tools
curl -s http://localhost:8003/mcp/tools | python3 -m json.tool   # invest   → 6 tools
curl -s http://localhost:8004/mcp/tools | python3 -m json.tool   # insights → 5 tools
curl -s http://localhost:8005/mcp/tools | python3 -m json.tool   # goals    → 7 tools
```
**Expect (verified counts):** 4 + 7 + 6 + 5 + 7 = **29**. Shows the MCP discovery contract per server.

### 06–10 — Per-server health
```bash
for p in 8001 8002 8003 8004 8005; do curl -s http://localhost:$p/health; echo; done
```
**Expect:** `{"status":"ok","server":"<name>-server"}` for each — proves each microservice is independently healthy.


---

## 3. Phase B — The Agentic /chat Demo Flow (user_id: `u_demo`)

Run these in order — they build up data so later insights/net-worth are meaningful.
Each response returns `{reply, tools_used, raw_results}`. **Watch `tools_used`** — that's the proof of orchestration.

> Tip: `tool_choice="auto"` means the model *chooses* tools; on a small model the exact
> chain can vary slightly run-to-run. The **flagship (21)** reliably shows the 3-tool chain.

| # | Say / send | Expect `tools_used` to include |
|---|---|---|
| 20 | "got my salary of 60000 today" | `log_income` |
| **21** | **"spent 250 on zomato lunch"** | **`log_expense`, `check_budget_on_expense`, `get_habit_status`** (VERIFIED) |
| 22 | "set my food budget to 5000 a month" | `set_budget` |
| 23 | "spent another 4800 on food ordering dinner for a party" | `log_expense` + `check_budget_on_expense` → alert warning/exceeded |
| 24 | "I want to save 50000 for a MacBook by December 2025" | `create_goal` (savings) |
| 25 | "keep my food spending under 4000 every month" | `create_goal` (habit) |
| 26 | "I bought 10 shares of Reliance at 2900" | `add_holding` |
| 27 | "my monthly SIP of 5000 in Nifty BEES got processed today" | `log_sip` |
| 28 | "how is my investment portfolio doing right now?" | `get_portfolio` (live Yahoo prices) |
| 29 | "how am I doing this month?" | `get_monthly_summary` |
| 30 | "am I spending more than usual? anything unusual this month?" | `get_spending_trends` / `detect_anomalies` |
| 31 | "what is my net worth right now?" | `get_net_worth` |
| 32 | "give me a financial health check, rate my finances out of 100" | `get_financial_health_score` |
| 33 | "show me all my goals and how I'm doing on them" | `get_goals` |
| 34 | multi-turn follow-up with `history` | `get_expenses` / `get_monthly_summary` |

**Flagship curl (step 21) — VERIFIED output:**
```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u_demo","message":"spent 250 on zomato lunch"}' | python3 -m json.tool
```
```
tools_used: ["log_expense", "check_budget_on_expense", "get_habit_status"]
reply: "Got it! Your ₹250 lunch on Zomato has been recorded. …"
```

**Multi-turn curl (step 34):**
```bash
curl -s -X POST http://localhost:8000/chat -H "Content-Type: application/json" -d '{
  "user_id":"u_demo",
  "message":"and how much did I spend on food specifically?",
  "history":[
    {"role":"user","content":"how am I doing this month?"},
    {"role":"assistant","content":"This month you earned 60000 and your biggest category is food."}
  ]
}' | python3 -m json.tool

---

## 4. Phase C — Direct MCP Calls (no LLM — deterministic fallback, user_id: `u_direct`)

If the live LLM is slow/flaky, demo the tools **directly** — same servers, same DB, zero model variance.
Payload shape is the MCP contract: `{"tool": "<name>", "args": {…}}`.

**50 — log_expense (VERIFIED):**
```bash
curl -s -X POST http://localhost:8001/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"log_expense","args":{"user_id":"u_direct","amount":250,"category":"food","description":"zomato lunch","merchant":"Zomato","payment_method":"upi"}}'
# → success:true, transaction_id, monthly_context.category_spend_this_month:250.0
```

**51 → 52 — set_budget then check (VERIFIED):**
```bash
curl -s -X POST http://localhost:8002/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"set_budget","args":{"user_id":"u_direct","category":"food","monthly_limit":5000,"alert_threshold_pct":80}}'
curl -s -X POST http://localhost:8002/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"check_budget_on_expense","args":{"user_id":"u_direct","category":"food"}}'
# → {has_budget:true, alert_level:"ok", monthly_limit:5000, remaining:5000, daily_allowance:312.5, ...}
```

**53 — add_holding (VERIFIED):**
```bash
curl -s -X POST http://localhost:8003/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"add_holding","args":{"user_id":"u_direct","symbol":"RELIANCE.NS","name":"Reliance Industries","asset_type":"stock","quantity":10,"avg_buy_price":2900}}'
# → success:true, action:"created"
```

**54 — get_live_price (external API, VERIFIED):**
```bash
curl -s -X POST http://localhost:8003/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"get_live_price","args":{"symbol":"RELIANCE.NS"}}'
# → {success:true, symbol:"RELIANCE.NS", price:1257.5, 52w_high:1611.8, 52w_low:1249.8}
#   (a REAL live price from Yahoo Finance — great "reaches outside the system" moment)
```

**55 — create_goal (goals):**
```bash
curl -s -X POST http://localhost:8005/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"create_goal","args":{"user_id":"u_direct","title":"MacBook Fund","goal_type":"savings","target_amount":50000,"deadline":"2025-12-31"}}'
```

**56 — financial health score (insights, VERIFIED):**
```bash
curl -s -X POST http://localhost:8004/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"get_financial_health_score","args":{"user_id":"u_direct"}}'
# → {score:45, max_score:100, breakdown:{savings_score, budget_score, portfolio_score}, grade:"Fair"}
```

**57 — net worth (insights, cross-server read):**
```bash
curl -s -X POST http://localhost:8004/mcp/execute -H "Content-Type: application/json" \
  -d '{"tool":"get_net_worth","args":{"user_id":"u_direct"}}'
```

---

## 5. Suggested 4-minute demo narrative

1. **"The platform is alive."** Run `00` → point at `tools_count: 29` and 5 servers. Run `01`/`05` to show two servers' tool catalogs.
2. **"One sentence, multiple tools, multiple services."** Run `21` (zomato) → highlight `tools_used: [log_expense, check_budget_on_expense, get_habit_status]` and the natural reply. *This is the whole thesis.*
3. **"It reasons with context."** Run `22` (set budget) then `23` (go over) → the reply now warns you're over budget. Same category, different tone — driven by `alert_level`.
4. **"It reaches the outside world."** Run `28` (or direct `54`) → live Reliance price from Yahoo Finance.
5. **"It aggregates across services."** Run `32` health score / `31` net worth → insights-server reading transactions + budgets + holdings.
6. **Deterministic fallback:** if the model stalls, switch to Phase C (`50`–`57`) — same logic, no LLM.

---

## 6. Troubleshooting (fast)

| Symptom | Cause | Fix |
|---|---|---|
| `tools_count: 0` on `/health` | gateway started before servers | `curl /health` again (it re-discovers), or `docker compose up -d ai-gateway` |
| `404 … model … does not exist` | Groq model deprecated/Enterprise-only | `AI_MODEL=openai/gpt-oss-20b` in `.env`, then `docker compose up -d` (NOT `restart`) |
| `503 No MCP servers reachable` | servers down | `docker compose up -d`, check `docker compose ps` |
| `get_live_price` returns `price: null` | Yahoo unreachable / rate-limited | expected external-dependency behavior; retry, or use a stored-value tool for the demo |
| Chat reply but empty/odd chain | small model varied its tool choice | re-run; flagship (21) is most reliable; or use Phase C direct calls |
| 401/403 from provider | bad `AI_API_KEY` | fix key in `.env`, `docker compose up -d` |

---

**Files created:** `Bruno/FinTrack/00 …` through `57 …` (24 requests) + this `DEMO_RUN_SHEET.md`.

```
