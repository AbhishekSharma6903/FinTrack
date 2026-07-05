from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
from database import init_db, get_db
from models import TransactionCreate
from tools import log_transaction, get_expenses, get_monthly_summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("✅ expense-server ready — tables created")
    yield


app = FastAPI(
    title="FinTrack Expense MCP Server",
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
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/mcp/tools")
async def list_tools():
    """
    MCP tool discovery endpoint.
    The ai-gateway calls this to know what tools this server exposes.
    Claude receives these definitions and decides when to call each one.
    """
    return {
        "tools": [
            {
                "name": "log_expense",
                "description": (
                    "Log a user's expense transaction. Use this when the user mentions "
                    "spending money, buying something, or paying for anything. "
                    "Automatically provides monthly context so you can give smart feedback."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":        {"type": "string",  "description": "User's unique ID"},
                        "amount":         {"type": "number",  "description": "Amount spent in INR"},
                        "category":       {
                            "type": "string",
                            "enum": ["food", "transport", "shopping", "entertainment",
                                     "health", "utilities", "emi", "investment", "other"],
                            "description": "Expense category"
                        },
                        "description":    {"type": "string",  "description": "What was bought"},
                        "merchant":       {"type": "string",  "description": "Shop/app name if mentioned"},
                        "payment_method": {
                            "type": "string",
                            "enum": ["upi", "cash", "credit_card", "debit_card", "net_banking", "other"]
                        },
                    },
                    "required": ["user_id", "amount", "category"]
                }
            },
            {
                "name": "log_income",
                "description": (
                    "Log income received by the user — salary, freelance payment, "
                    "bonus, gift money, or any other inflow."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":     {"type": "string", "description": "User's unique ID"},
                        "amount":      {"type": "number", "description": "Amount received in INR"},
                        "category":    {
                            "type": "string",
                            "enum": ["salary", "freelance", "other"]
                        },
                        "description": {"type": "string", "description": "Source of income"},
                    },
                    "required": ["user_id", "amount", "category"]
                }
            },
            {
                "name": "get_expenses",
                "description": (
                    "Retrieve recent transactions for a user. Use when the user asks "
                    "about their spending history, wants to review expenses, or asks "
                    "'how much did I spend on X'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":  {"type": "string"},
                        "category": {"type": "string", "description": "Filter by category (optional)"},
                        "days":     {"type": "integer", "description": "How many days back (default 30)"},
                        "limit":    {"type": "integer", "description": "Max transactions to return"},
                    },
                    "required": ["user_id"]
                }
            },
            {
                "name": "get_monthly_summary",
                "description": (
                    "Get a full financial summary for the current month — income, "
                    "total spend, savings rate, breakdown by category. Use when user "
                    "asks for an overview or 'how am I doing this month'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"}
                    },
                    "required": ["user_id"]
                }
            }
        ]
    }


@app.post("/mcp/execute")
async def execute_tool(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    MCP tool execution endpoint.
    The ai-gateway calls this when Claude decides to use a tool.
    """
    tool_name = payload.get("tool")
    args      = payload.get("args", {})

    if tool_name == "log_expense":
        data = TransactionCreate(
            user_id        = args["user_id"],
            type           = "expense",
            amount         = args["amount"],
            category       = args["category"],
            description    = args.get("description"),
            merchant       = args.get("merchant"),
            payment_method = args.get("payment_method", "upi"),
        )
        return await log_transaction(data, db)

    elif tool_name == "log_income":
        data = TransactionCreate(
            user_id     = args["user_id"],
            type        = "income",
            amount      = args["amount"],
            category    = args["category"],
            description = args.get("description"),
        )
        return await log_transaction(data, db)

    elif tool_name == "get_expenses":
        return await get_expenses(
            user_id  = args["user_id"],
            db       = db,
            category = args.get("category"),
            days     = args.get("days", 30),
            limit    = args.get("limit", 20),
        )

    elif tool_name == "get_monthly_summary":
        return await get_monthly_summary(args["user_id"], db)

    else:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")


@app.get("/health")
async def health():
    return {"status": "ok", "server": "expense-server"}