from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
from database import init_db, get_db
from tools import (
    set_budget,
    get_budget_status,
    get_all_budgets,
    check_budget_on_expense,
    update_budget,
    delete_budget,
    get_budget_recommendations,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the 'budgets' table on startup (if it doesn't exist yet)
    await init_db()
    print("✅ budget-server ready — tables created")
    yield


app = FastAPI(
    title="FinTrack Budget MCP Server",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# MCP Protocol Endpoints
# These two endpoints are the entire MCP contract. Every MCP server in FinTrack
# must implement exactly these two routes — that's what makes them interchangeable
# and discoverable by the ai-gateway without any hardcoded tool knowledge.
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/mcp/tools")
async def list_tools():
    """
    Tool discovery endpoint. The ai-gateway calls this on startup (and per request)
    to learn what capabilities this server exposes. Claude receives these definitions
    and uses the name + description to decide when to call each tool.

    The 'description' field is critical — it's what Claude reads to decide
    WHEN to invoke a tool. Write it from Claude's perspective: "use this when..."
    """
    return {
        "tools": [
            {
                "name": "set_budget",
                "description": (
                    "Set or update a monthly spending limit for a category. "
                    "Use when the user says things like 'set my food budget to 5000', "
                    "'I want to spend max 3000 on transport', or 'limit my shopping to 8000 a month'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":             {"type": "string",  "description": "User's unique ID"},
                        "category":            {
                            "type": "string",
                            "enum": ["food", "transport", "shopping", "entertainment",
                                     "health", "utilities", "emi", "investment", "other"],
                            "description": "The category to set a budget for"
                        },
                        "monthly_limit":       {"type": "number",  "description": "Monthly limit in INR"},
                        "alert_threshold_pct": {"type": "integer", "description": "Warn at this % used (default 80)"},
                    },
                    "required": ["user_id", "category", "monthly_limit"]
                }
            },
            {
                "name": "get_budget_status",
                "description": (
                    "Get the current status of a specific category budget — how much is left, "
                    "daily allowance, whether the user is on track. "
                    "Use when user asks 'how much food budget do I have left' or 'am I over on transport'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":  {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": ["food", "transport", "shopping", "entertainment",
                                     "health", "utilities", "emi", "investment", "other"]
                        },
                    },
                    "required": ["user_id", "category"]
                }
            },
            {
                "name": "get_all_budgets",
                "description": (
                    "Get status of all budgets the user has set. Use when the user wants "
                    "a budget overview — 'show me all my budgets', 'how am I doing on budgets', "
                    "'budget summary'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"}
                    },
                    "required": ["user_id"]
                }
            },
            {
                "name": "check_budget_on_expense",
                "description": (
                    "ALWAYS call this immediately after logging an expense with log_expense. "
                    "It checks if the user is near or over their budget for that category "
                    "and returns an alert level (ok / warning / danger / exceeded). "
                    "Use the alert level to decide how urgently to warn the user in your reply. "
                    "If alert_level is 'ok', no need to mention the budget. "
                    "If 'warning' or higher, include budget context in your reply."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":  {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": ["food", "transport", "shopping", "entertainment",
                                     "health", "utilities", "emi", "investment", "other"]
                        },
                    },
                    "required": ["user_id", "category"]
                }
            },
            {
                "name": "update_budget",
                "description": (
                    "Update the monthly limit on an existing budget. "
                    "Use when user says 'increase my food budget to 6000' or 'change transport limit'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":       {"type": "string"},
                        "category":      {
                            "type": "string",
                            "enum": ["food", "transport", "shopping", "entertainment",
                                     "health", "utilities", "emi", "investment", "other"]
                        },
                        "monthly_limit": {"type": "number", "description": "New monthly limit in INR"},
                    },
                    "required": ["user_id", "category", "monthly_limit"]
                }
            },
            {
                "name": "delete_budget",
                "description": (
                    "Remove a budget for a category. "
                    "Use when user says 'remove my food budget' or 'I don't want to track shopping anymore'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":  {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": ["food", "transport", "shopping", "entertainment",
                                     "health", "utilities", "emi", "investment", "other"]
                        },
                    },
                    "required": ["user_id", "category"]
                }
            },
            {
                "name": "get_budget_recommendations",
                "description": (
                    "Analyse spending patterns and suggest budget limits. "
                    "Use when user asks 'suggest budgets for me', 'what budgets should I set', "
                    "or 'help me set up budgets based on my spending'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"}
                    },
                    "required": ["user_id"]
                }
            },
        ]
    }


@app.post("/mcp/execute")
async def execute_tool(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Tool execution endpoint. The ai-gateway sends:
      { "tool": "set_budget", "args": { "user_id": "...", "category": "food", ... } }
    We route to the right function in tools.py and return the result.
    Claude receives the return value as a tool_result and continues reasoning.
    """
    tool_name = payload.get("tool")
    args      = payload.get("args", {})

    if tool_name == "set_budget":
        return await set_budget(
            user_id             = args["user_id"],
            category            = args["category"],
            monthly_limit       = args["monthly_limit"],
            db                  = db,
            alert_threshold_pct = args.get("alert_threshold_pct", 80),
        )

    elif tool_name == "get_budget_status":
        return await get_budget_status(
            user_id  = args["user_id"],
            category = args["category"],
            db       = db,
        )

    elif tool_name == "get_all_budgets":
        return await get_all_budgets(user_id=args["user_id"], db=db)

    elif tool_name == "check_budget_on_expense":
        return await check_budget_on_expense(
            user_id  = args["user_id"],
            category = args["category"],
            db       = db,
        )

    elif tool_name == "update_budget":
        return await update_budget(
            user_id       = args["user_id"],
            category      = args["category"],
            monthly_limit = args["monthly_limit"],
            db            = db,
        )

    elif tool_name == "delete_budget":
        return await delete_budget(
            user_id  = args["user_id"],
            category = args["category"],
            db       = db,
        )

    elif tool_name == "get_budget_recommendations":
        return await get_budget_recommendations(user_id=args["user_id"], db=db)

    else:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")


@app.get("/health")
async def health():
    return {"status": "ok", "server": "budget-server"}