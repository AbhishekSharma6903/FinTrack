# FinTrack — Beginner's Concept Guide

A plain-English walkthrough of the three things you asked about:

1. Docker port mapping (`5432:5432`, `5433:5432`, etc.)
2. SQLAlchemy — what it is and how it's used in this project
3. The architecture: what are MCP servers and the AI gateway, and how do they work together?

---

## 1) Docker port mapping — `HOST_PORT : CONTAINER_PORT`

### The mental model: two worlds

When you run something in Docker, there are **two separate worlds**:

- **Your Mac** (the "host") — this is where you type commands, open a browser, use `psql`, etc.
- **The container** — a tiny isolated Linux computer running inside Docker.

These two worlds have **completely separate networking**. If Postgres listens on port 5432 *inside* the container, your Mac cannot reach it — unless you build a bridge. That bridge is called a **port mapping**.

### The syntax

```yaml
ports:
  - "HOST_PORT : CONTAINER_PORT"
```

- **Left side (HOST_PORT)** = the port on **your Mac**
- **Right side (CONTAINER_PORT)** = the port **inside the container**

Docker sets up a rule: "Anyone who knocks on `localhost:HOST_PORT` on the Mac, forward them to `CONTAINER_PORT` inside the container."

### Example — before and after our fix

**Before (had conflict):**
```yaml
ports:
  - "5432:5432"
```
This means: "Take incoming traffic on Mac's port 5432 → forward to Postgres inside the container on 5432."

But there was already a **Homebrew Postgres** installed on your Mac occupying port 5432. Two things cannot own the same port at the same time, so Docker crashed with `port is already allocated`.

**After (works):**
```yaml
ports:
  - "5433:5432"
```
Now: "Traffic on Mac's port 5433 → forward to Postgres inside the container on 5432."

Nothing was competing for 5433, so it works.

### Visual

```
┌──────────── YOUR MAC ────────────┐
│                                  │
│  Homebrew Postgres  ⇢  :5432     │  ← already using 5432
│                                  │
│  psql -h localhost -p 5433 ─┐    │  ← you connect here
│                             │    │
│                             ▼    │
│                        ┌────────────────┐
│                        │  DOCKER        │
│                        │  ┌──────────┐  │
│                        │  │postgres  │  │
│                        │  │  :5432   │  │  ← runs on 5432 inside
│                        │  └──────────┘  │
│                        └────────────────┘
└──────────────────────────────────┘
```

### Key insight — inside the Docker network, ports are UNCHANGED

Notice something important: `budget-server`, `expense-server`, and `ai-gateway` all talk to Postgres using this connection string:

```
postgresql://user:pass@postgres:5432/fintrack
                       ^^^^^^^^
```

They use `postgres:5432`, **not** `5433`. Why?

Because **Docker Compose creates its own private network** where each container gets a hostname equal to its service name (`postgres`, `redis`, `expense-server`, etc.). Inside this private network, containers talk to each other on the container's **actual** port (5432), not the mapped host port.

So:
- `HOST_PORT` (left side) → only affects **your Mac reaching in from outside**
- `CONTAINER_PORT` (right side) → what other containers use to reach it, and what the app inside listens on

That's why changing `5432:5432` → `5433:5432` fixed *your Mac's* conflict but broke absolutely nothing about how the containers talk to each other.

### More examples to lock this in

| YAML                | Meaning |
|---------------------|---------|
| `"3000:3000"`       | Mac's `:3000` → container's `:3000` (identical, most common) |
| `"8080:80"`         | Mac's `:8080` → container's `:80` (an nginx container listens on 80 internally) |
| `"5433:5432"`       | Mac's `:5433` → container's `:5432` (what we did) |
| `"6379:6379"`       | Mac's `:6379` → Redis container's `:6379` |

**Rule of thumb:** If you get "port is already allocated", change **only the left number** to any unused port. The app inside doesn't know or care.

### Check what's on a port (Mac)
```bash
lsof -i :5432        # who's using port 5432?
```

---

## 2) SQLAlchemy — what it is and how this project uses it

### The problem it solves

Databases speak **SQL** (a text language):
```sql
INSERT INTO transactions (user_id, amount, category) VALUES ('u1', 250, 'food');
SELECT * FROM transactions WHERE user_id = 'u1' ORDER BY created_at DESC LIMIT 10;
```

Python speaks **objects**:
```python
t = Transaction(user_id="u1", amount=250, category="food")
```

Every time you'd normally write SQL in a Python app, you'd have to:
1. Build the SQL string (risky — SQL injection),
2. Send it to the DB,
3. Parse the raw results back into Python.

**SQLAlchemy is a bridge** — an ORM (Object–Relational Mapper) — that lets you work with **Python classes and objects** and it translates to/from SQL automatically. It also handles connections, transactions, pooling, migrations, etc.

### Two layers of SQLAlchemy you'll see

1. **Core** — a Pythonic way of writing SQL (`select(...)`, `insert(...)`).
2. **ORM** — Python classes that map to database tables. Row = object.

This project uses **the ORM**.

### The 3 files that matter — walk through your code

#### (a) `models.py` — Python classes ≈ DB tables

Snippet from `expense-server/models.py`:

```python
from sqlalchemy import Column, String, Numeric, DateTime, Text, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.postgresql import UUID

class Base(DeclarativeBase):
    pass                       # marker class — all models inherit from Base

class Transaction(Base):
    __tablename__ = "transactions"   # → maps to Postgres table "transactions"

    id       = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id  = Column(String(100), nullable=False, index=True)
    amount   = Column(Numeric(12, 2), nullable=False)
    category = Column(SAEnum(Category, native_enum=False, length=20), nullable=False)
    ...
```

Reading this: "Create a table called `transactions`. It has an `id` column that's a UUID primary key, a `user_id` VARCHAR(100), an `amount` NUMERIC(12,2)…". SQLAlchemy will generate the equivalent `CREATE TABLE` SQL for you when you tell it to.

#### (b) `database.py` — the connection

```python
DATABASE_URL = os.getenv("DATABASE_URL", "").replace(
    "postgresql://", "postgresql+asyncpg://"
)

engine = create_async_engine(DATABASE_URL, echo=False)
```

- **Engine** = the "connection factory". It knows *how* to reach Postgres.
- `+asyncpg` = the async driver, so we can `await` DB calls (fast, non-blocking).

```python
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

This one line is doing something big: **`Base.metadata.create_all`** looks at every class that inherits from `Base`, and issues `CREATE TABLE IF NOT EXISTS ...` for each. That's why simply starting the container creates the `transactions` and `budgets` tables in Postgres — no manual SQL migration.

*(By the way — remember the bug we fixed? SQLAlchemy was also creating a Postgres `ENUM` type named `category` here. Setting `native_enum=False` told it "don't create a Postgres ENUM type; just store the enum values as short strings with a CHECK constraint instead." That's why the collision between the two services went away.)*

```python
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
```

A **Session** is a short-lived scratchpad where you stage inserts, queries, and updates. When you're done, you `commit()` and it flushes them all to the DB. FastAPI's `Depends(get_db)` provides a fresh session per HTTP request and auto-closes it.

#### (c) `tools.py` — actually using it

You didn't ask to see it, but here's the pattern that lives in there:

```python
# INSERT — build an object, add to session, commit
new_txn = Transaction(user_id="u1", amount=250, category="food")
db.add(new_txn)
await db.commit()

# SELECT — build a query object, execute, iterate
from sqlalchemy import select
result = await db.execute(
    select(Transaction).where(Transaction.user_id == "u1").limit(10)
)
transactions = result.scalars().all()

# UPDATE — mutate the Python object, commit
txn.amount = 300
await db.commit()

# DELETE
await db.delete(txn)
await db.commit()
```

Notice — **no raw SQL strings.** SQLAlchemy builds the correct SQL for whichever database you use (Postgres, MySQL, SQLite…) and handles escaping.

### One more thing — Pydantic vs SQLAlchemy

You'll see both in `models.py`. They look similar but have different jobs:

| Library     | Role in this project                                          |
|-------------|---------------------------------------------------------------|
| SQLAlchemy  | **How data is stored** — defines the DB table (`Transaction`) |
| Pydantic    | **How data enters/leaves the API** — validates JSON (`TransactionCreate`, `TransactionResponse`) |

Flow of an incoming request:

```
HTTP JSON body
      │
      ▼
Pydantic (TransactionCreate)   ← validates: is amount a number? is category in enum?
      │
      ▼
SQLAlchemy (Transaction)       ← saves to Postgres
      │
      ▼
Pydantic (TransactionResponse) ← shapes what goes back as JSON
```

---

## 3) The architecture: MCP servers + AI gateway

### The big picture

FinTrack is designed as a mini **microservices** system with an AI brain on top.

```
                       ┌───────────────────────┐
   You / a chat app ─► │   ai-gateway :8000    │ ─► Anthropic Claude API (LLM)
                       │  (the "brain wiring") │
                       └──────┬────────┬───────┘
                              │        │
                    HTTP JSON │        │ HTTP JSON
                              ▼        ▼
              ┌─────────────────┐    ┌─────────────────┐
              │ expense-server  │    │ budget-server   │
              │      :8001      │    │      :8002      │
              └────────┬────────┘    └────────┬────────┘
                       │                      │
                       └────────┬─────────────┘
                                ▼
                          ┌───────────┐
                          │ Postgres  │  (shared DB)
                          │  :5432    │
                          └───────────┘
```

Plus Redis (not shown) for caching.

### What is MCP?

**MCP = Model Context Protocol.** It's an open protocol from Anthropic that standardises how an LLM (like Claude) discovers and invokes external tools.

Without MCP, if you wanted Claude to "log an expense", you'd have to invent your own JSON contract, ship it in your prompt every time, and hand-code the wiring. With MCP:

- Each tool provider (an **MCP server**) exposes a standard set of endpoints:
  - `GET  /mcp/tools`    → "here's the list of tools I offer and their JSON schemas"
  - `POST /mcp/execute`  → "run this tool with these arguments"
- Any MCP-aware LLM host can plug into any MCP server.

So in this project, `expense-server` and `budget-server` are **MCP servers** — self-contained services that publish a menu of tools and know how to execute them against a database.

### The three components in this project

#### A) `expense-server` (port 8001) — MCP server #1

Look at `mcp-servers/expense-server/main.py`. It's a FastAPI app that exposes:

- `GET /mcp/tools` → returns 4 tool definitions:
  - `log_expense`
  - `log_income`
  - `get_expenses`
  - `get_monthly_summary`
- `POST /mcp/execute` → receives `{"tool": "log_expense", "args": {...}}` and dispatches to the right handler in `tools.py`, which uses SQLAlchemy to write/read the `transactions` table.

Each tool definition includes a **JSON Schema** describing the arguments. Example from your code:

```python
{
  "name": "log_expense",
  "description": "Log a user's expense transaction. Use this when the user mentions spending money...",
  "input_schema": {
    "type": "object",
    "properties": {
      "user_id":  {"type": "string"},
      "amount":   {"type": "number"},
      "category": {"type": "string", "enum": ["food", "transport", "..."]},
    },
    "required": ["user_id", "amount", "category"]
  }
}
```

The `description` field is important — that's what Claude reads to decide *when* to call this tool. Good descriptions = smart tool use.

#### B) `budget-server` (port 8002) — MCP server #2

Same pattern, different concern. It exposes tools like `set_budget`, `get_budgets`, `check_budget_on_expense`. It writes/reads the `budgets` table.

**Why split them?** Separation of concerns:
- One team could own budgets, another could own expenses.
- If budget logic gets complex, it doesn't bloat the expense codebase.
- You can scale, deploy, or restart each independently.
- Later you could add `invest-server`, `goals-server`, etc. without touching the existing ones.

Both services happen to share the same Postgres database in this project (for simplicity), but architecturally they don't have to — each could have its own DB if needed.

#### C) `ai-gateway` (port 8000) — the orchestrator

This is the app that **users actually talk to**. It has one main endpoint: `POST /chat`.

Look at `ai-gateway/mcp_client.py`. When it starts:

```python
self.servers = {
    "expense-server": "http://expense-server:8001",
    "budget-server":  "http://budget-server:8002",
}
```

It calls `GET /mcp/tools` on each server (in `discover_tools()`), aggregates all tool definitions into one big list, and remembers *which server owns which tool* in `self._tool_registry`.

Then in `ai-gateway/main.py`, the `/chat` endpoint runs an **agentic loop**:

```python
while True:
    response = client.messages.create(
        model    = "claude-sonnet-4-6",
        system   = SYSTEM_PROMPT,
        tools    = tools,       # ← all tool defs from all MCP servers
        messages = messages,
    )

    if response.stop_reason == "end_turn":
        return final_text          # Claude is done — send reply to user

    if response.stop_reason == "tool_use":
        # Claude wants to call one or more tools
        for block in response.content:
            if block.type == "tool_use":
                result = await mcp.execute_tool(block.name, block.input)
                # ...append result to messages so Claude sees it next iteration
```

- The loop asks Claude: "given the user's message and these available tools, what next?"
- Claude replies either with a final text answer OR with `tool_use` blocks requesting one or more tool calls.
- If it's tool calls, the gateway executes them by POSTing to the right MCP server, feeds the results back into the conversation, and loops.
- Eventually Claude says "I'm done" (`end_turn`) and the gateway returns the final reply.

### Concrete end-to-end example

User (via a frontend or Bruno/Postman) POSTs to `http://localhost:8000/chat`:

```json
{
  "user_id": "u_ayush",
  "api_key": "sk-ant-...",
  "message": "spent 250 on zomato lunch"
}
```

Behind the scenes:

1. **ai-gateway** sends to Claude: `SYSTEM_PROMPT` + list of all tools + user's message.
2. Claude reads: "spent 250 on zomato" → looks at tools → picks `log_expense` with `{amount: 250, category: "food", merchant: "zomato"}`.
3. ai-gateway sees `stop_reason=tool_use`, checks its registry: "`log_expense` belongs to `expense-server`" → POSTs to `http://expense-server:8001/mcp/execute`.
4. **expense-server** runs the `log_transaction()` handler in `tools.py`, which uses SQLAlchemy to insert a row into `transactions`.
5. expense-server returns the created row + a mini monthly context: `{id: "...", amount: 250, monthly_total_food: 3200, ...}`.
6. ai-gateway appends that result to the message history and loops back to Claude.
7. The SYSTEM_PROMPT tells Claude: "after every `log_expense`, also call `check_budget_on_expense`." Claude does → gateway routes it to `budget-server:8002/mcp/execute` → returns `{alert_level: "warning", remaining: 1800, ...}`.
8. Claude now has everything it needs. It returns a natural, warm reply: *"Got it — ₹250 on Zomato logged. You've spent ₹3,200 of your ₹5,000 food budget this month, ₹1,800 left with 12 days to go — you're pacing okay."*
9. ai-gateway sends that back as the HTTP response.

### Why this architecture is nice

- **Claude is the reasoning engine** — it decides *what* to do.
- **MCP servers are the hands** — they do *how* to do it (talk to the DB, run business logic).
- **ai-gateway is the nervous system** — it wires everything and manages the multi-step agentic loop.
- Adding a new capability (e.g., "goal tracking") = spin up a `goals-server`, add its URL to `mcp_client.py`, and Claude automatically discovers and uses its tools. No prompt changes.

### BYOK — "Bring Your Own Key"

Note in `ChatRequest`:
```python
api_key: str    # user's own Claude API key
```
The gateway doesn't have an embedded Anthropic key. Each user supplies theirs, so this project can be shared/hosted without you paying for everyone's Claude usage.

---

## Quick reference — where things live

| Concept                 | File(s)                                      |
|-------------------------|----------------------------------------------|
| Docker + ports          | `docker-compose.yml`                         |
| Env vars (DB URL, etc.) | `.env`                                       |
| SQLAlchemy models       | `mcp-servers/*/models.py`                    |
| DB connection / setup   | `mcp-servers/*/database.py`                  |
| MCP tool definitions    | `mcp-servers/*/main.py` (`/mcp/tools`)       |
| MCP tool execution      | `mcp-servers/*/main.py` (`/mcp/execute`) → `tools.py` |
| Talks to Claude         | `ai-gateway/main.py` (`/chat`)               |
| Talks to MCP servers    | `ai-gateway/mcp_client.py`                   |

## Useful docker commands to explore

```bash
# See all services and health
cd FinTrack && docker compose ps

# Live logs of one service
docker compose logs -f ai-gateway

# Hit an MCP server directly and see its tool menu
curl http://localhost:8001/mcp/tools | jq
curl http://localhost:8002/mcp/tools | jq

# See the aggregated view the gateway has
curl http://localhost:8000/health | jq

# Get a psql shell into the Docker Postgres (note the port 5433!)
psql -h localhost -p 5433 -U <user> -d <db>

# Or from *inside* the postgres container (where the port is 5432):
docker compose exec postgres psql -U <user> -d <db>
```
