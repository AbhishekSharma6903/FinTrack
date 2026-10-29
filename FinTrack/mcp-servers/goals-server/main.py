from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
from database import init_db, get_db
from tools import (
    create_goal,
    get_goals,
    add_goal_contribution,
    get_goal_detail,
    update_goal,
    complete_goal,
    get_habit_status,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("✅ goals-server ready — tables created")
    yield


app = FastAPI(
    title="FinTrack Goals MCP Server",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/mcp/tools")
async def list_tools():
    """
    Tool discovery. Notice the descriptions are written from Claude's perspective:
    "Use this when the user says..." — Claude reads these to decide which tool to call.
    The richer and more specific the description, the better Claude's tool selection.
    """
    return {
        "tools": [
            {
                "name": "create_goal",
                "description": (
                    "Create a new financial goal for the user. Two types: "
                    "(1) SAVINGS: user wants to accumulate a target amount by a deadline — "
                    "'I want to save ₹50,000 for a MacBook by December', 'build an emergency fund of 3 lakhs'. "
                    "(2) HABIT: user wants to limit spending in a category every month — "
                    "'keep food under ₹4000/month', 'I want to spend less than ₹2000 on entertainment'. "
                    "Always call this when the user expresses a financial intention or target."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":        {"type": "string"},
                        "title":          {"type": "string", "description": "Short goal name e.g. 'MacBook Fund'"},
                        "goal_type":      {"type": "string", "enum": ["savings", "habit"]},
                        "target_amount":  {"type": "number",  "description": "Target amount in INR (savings goals only)"},
                        "deadline":       {"type": "string",  "description": "ISO date string e.g. '2025-12-31' (savings goals)"},
                        "habit_category": {"type": "string",
                                          "enum": ["food", "transport", "shopping", "entertainment",
                                                   "health", "utilities", "emi", "other"],
                                          "description": "Category to track (habit goals only)"},
                        "habit_limit":    {"type": "number", "description": "Monthly spend limit in INR (habit goals only)"},
                        "notes":          {"type": "string"},
                    },
                    "required": ["user_id", "title", "goal_type"]
                }
            },
            {
                "name": "get_goals",
                "description": (
                    "Get all active goals with current progress. "
                    "Use when user asks 'show my goals', 'how am I doing on my goals', "
                    "'goals overview', or 'what am I saving for'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"]
                }
            },
            {
                "name": "add_goal_contribution",
                "description": (
                    "Log a savings deposit toward a specific goal and recalculate progress. "
                    "Use when user says 'I put 5000 toward my MacBook fund', "
                    "'added 2000 to my emergency fund', 'saved 3000 this month for my trip'. "
                    "Returns updated progress including % complete and required monthly savings."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "goal_id": {"type": "string", "description": "UUID of the goal"},
                        "amount":  {"type": "number",  "description": "Amount deposited in INR"},
                        "note":    {"type": "string",  "description": "Optional note about this deposit"},
                    },
                    "required": ["user_id", "goal_id", "amount"]
                }
            },
            {
                "name": "get_goal_detail",
                "description": (
                    "Get full detail for a specific goal including contribution history. "
                    "Use when user asks about a specific goal by name, "
                    "'show me my MacBook goal details', or after calling get_goals and needing more info."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "goal_id": {"type": "string"},
                    },
                    "required": ["user_id", "goal_id"]
                }
            },
            {
                "name": "update_goal",
                "description": (
                    "Update a goal's title, target amount, deadline, or habit limit. "
                    "Use when user says 'change my MacBook goal to 60,000', "
                    "'extend my deadline to March', 'rename my goal'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":       {"type": "string"},
                        "goal_id":       {"type": "string"},
                        "title":         {"type": "string"},
                        "target_amount": {"type": "number"},
                        "deadline":      {"type": "string", "description": "ISO date string"},
                        "habit_limit":   {"type": "number"},
                        "notes":         {"type": "string"},
                    },
                    "required": ["user_id", "goal_id"]
                }
            },
            {
                "name": "complete_goal",
                "description": (
                    "Mark a goal as completed. Use when user says 'I bought the MacBook', "
                    "'goal achieved', 'mark my emergency fund as done', "
                    "or when add_goal_contribution returns auto_completed=true."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "goal_id": {"type": "string"},
                    },
                    "required": ["user_id", "goal_id"]
                }
            },
            {
                "name": "get_habit_status",
                "description": (
                    "Check progress on all habit goals this month — how much has been spent "
                    "vs the user's self-imposed limit for each category. "
                    "Use when user asks 'how are my habit goals going', "
                    "'am I on track with my food limit', or 'check my spending habits'. "
                    "Also call this proactively after log_expense if the user has habit goals."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"]
                }
            },
        ]
    }


@app.post("/mcp/execute")
async def execute_tool(payload: dict, db: AsyncSession = Depends(get_db)):
    tool_name = payload.get("tool")
    args      = payload.get("args", {})

    if tool_name == "create_goal":
        return await create_goal(
            user_id        = args["user_id"],
            title          = args["title"],
            goal_type      = args["goal_type"],
            db             = db,
            target_amount  = args.get("target_amount"),
            deadline       = args.get("deadline"),
            habit_category = args.get("habit_category"),
            habit_limit    = args.get("habit_limit"),
            notes          = args.get("notes"),
        )

    elif tool_name == "get_goals":
        return await get_goals(user_id=args["user_id"], db=db)

    elif tool_name == "add_goal_contribution":
        return await add_goal_contribution(
            user_id = args["user_id"],
            goal_id = args["goal_id"],
            amount  = args["amount"],
            db      = db,
            note    = args.get("note"),
        )

    elif tool_name == "get_goal_detail":
        return await get_goal_detail(
            user_id = args["user_id"],
            goal_id = args["goal_id"],
            db      = db,
        )

    elif tool_name == "update_goal":
        return await update_goal(
            user_id       = args["user_id"],
            goal_id       = args["goal_id"],
            db            = db,
            title         = args.get("title"),
            target_amount = args.get("target_amount"),
            deadline      = args.get("deadline"),
            habit_limit   = args.get("habit_limit"),
            notes         = args.get("notes"),
        )

    elif tool_name == "complete_goal":
        return await complete_goal(
            user_id = args["user_id"],
            goal_id = args["goal_id"],
            db      = db,
        )

    elif tool_name == "get_habit_status":
        return await get_habit_status(user_id=args["user_id"], db=db)

    else:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")


@app.get("/health")
async def health():
    return {"status": "ok", "server": "goals-server"}
