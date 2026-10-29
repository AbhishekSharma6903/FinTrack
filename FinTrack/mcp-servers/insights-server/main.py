from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
from database import init_db, get_db
from tools import (
    get_spending_trends,
    detect_anomalies,
    get_month_forecast,
    get_net_worth,
    get_financial_health_score,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()  # no-op — this server owns no tables
    print("✅ insights-server ready")
    yield


app = FastAPI(
    title="FinTrack Insights MCP Server",
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
    return {
        "tools": [
            {
                "name": "get_spending_trends",
                "description": (
                    "Get month-over-month spending trends per category for the last 3 months. "
                    "Use when user asks 'how has my spending changed', 'am I spending more than last month', "
                    "or 'show me my spending trends'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"]
                }
            },
            {
                "name": "detect_anomalies",
                "description": (
                    "Detect spending anomalies — categories where this month's spend is unusually high "
                    "compared to the 3-month average. Use when user asks 'anything unusual in my spending', "
                    "'why is my spending high', or proactively after viewing monthly summary."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"]
                }
            },
            {
                "name": "get_month_forecast",
                "description": (
                    "Project end-of-month spend and savings based on current burn rate. "
                    "Use when user asks 'will I save money this month', 'how much will I spend by month end', "
                    "or 'am I on track this month'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"]
                }
            },
            {
                "name": "get_net_worth",
                "description": (
                    "Calculate the user's net worth — total assets (portfolio + savings) minus liabilities (EMI). "
                    "Use when user asks 'what is my net worth', 'how much am I worth', or 'assets vs liabilities'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"]
                }
            },
            {
                "name": "get_financial_health_score",
                "description": (
                    "Calculate a 0-100 financial health score based on savings rate, budget adherence, "
                    "and portfolio diversification. Use when user asks 'how am I doing financially', "
                    "'give me a health check', 'rate my finances', or 'financial score'."
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

    if tool_name == "get_spending_trends":
        return await get_spending_trends(user_id=args["user_id"], db=db)

    elif tool_name == "detect_anomalies":
        return await detect_anomalies(user_id=args["user_id"], db=db)

    elif tool_name == "get_month_forecast":
        return await get_month_forecast(user_id=args["user_id"], db=db)

    elif tool_name == "get_net_worth":
        return await get_net_worth(user_id=args["user_id"], db=db)

    elif tool_name == "get_financial_health_score":
        return await get_financial_health_score(user_id=args["user_id"], db=db)

    else:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")


@app.get("/health")
async def health():
    return {"status": "ok", "server": "insights-server"}
