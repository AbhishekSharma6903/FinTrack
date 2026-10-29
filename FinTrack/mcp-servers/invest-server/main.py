from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager
from database import init_db, get_db
from tools import add_holding, get_portfolio, get_live_price, log_sip, get_sip_summary, get_asset_allocation


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("✅ invest-server ready — tables created")
    yield


app = FastAPI(
    title="FinTrack Invest MCP Server",
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
    Tool discovery endpoint. The ai-gateway calls this on startup.
    Note the descriptions — they tell Claude WHEN to call each tool.
    The get_live_price tool description is especially important: it tells
    Claude this tool calls Yahoo Finance (an external API), so Claude knows
    it returns real-time data, not just stored values.
    """
    return {
        "tools": [
            {
                "name": "add_holding",
                "description": (
                    "Add a stock, mutual fund, crypto, gold, or FD holding to the user's portfolio. "
                    "Use when the user says 'I bought X shares of Y at Z', 'I invested in Nifty ETF', "
                    "'I have 10g of gold', or 'I started a SIP in HDFC Flexi Cap'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id":       {"type": "string"},
                        "symbol":        {"type": "string", "description": "Yahoo Finance symbol. NSE stocks: append .NS (e.g. RELIANCE.NS). MF ETFs: NIFTYBEES.NS. Crypto: BTC-INR. Gold: GC=F. FD: use a custom label like FD_HDFC."},
                        "name":          {"type": "string", "description": "Human-readable name e.g. 'Reliance Industries'"},
                        "asset_type":    {"type": "string", "enum": ["stock", "mutual_fund", "crypto", "gold", "fd"]},
                        "quantity":      {"type": "number", "description": "Number of shares or units"},
                        "avg_buy_price": {"type": "number", "description": "Price per unit at time of purchase in INR"},
                        "notes":         {"type": "string", "description": "Optional note"},
                    },
                    "required": ["user_id", "symbol", "name", "asset_type", "quantity", "avg_buy_price"]
                }
            },
            {
                "name": "get_portfolio",
                "description": (
                    "Get the user's full investment portfolio with LIVE prices from Yahoo Finance "
                    "and real-time P&L for each holding. Use when user asks 'show my portfolio', "
                    "'how are my investments doing', 'what's my profit/loss', or 'portfolio summary'."
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
                "name": "get_live_price",
                "description": (
                    "Fetch the current live market price for any stock, ETF, or crypto from Yahoo Finance. "
                    "Use when user asks 'what is the price of HDFC Bank today', 'check Nifty 50', "
                    "'how much is Bitcoin in rupees'. No user_id needed — this is a pure market data query."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Yahoo Finance symbol e.g. HDFCBANK.NS, ^NSEI, BTC-INR"}
                    },
                    "required": ["symbol"]
                }
            },
            {
                "name": "log_sip",
                "description": (
                    "Record a monthly SIP (Systematic Investment Plan) instalment. "
                    "Use when user says 'my SIP of 5000 in Nifty processed', "
                    "'logged my monthly mutual fund SIP', or 'SIP deducted today'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "symbol":  {"type": "string", "description": "Fund symbol e.g. NIFTYBEES.NS"},
                        "amount":  {"type": "number", "description": "SIP instalment amount in INR"},
                    },
                    "required": ["user_id", "symbol", "amount"]
                }
            },
            {
                "name": "get_sip_summary",
                "description": (
                    "Get a summary of all SIP investments — total instalments per fund and overall SIP corpus. "
                    "Use when user asks 'how much have I invested via SIPs', 'SIP summary', 'my SIP history'."
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
                "name": "get_asset_allocation",
                "description": (
                    "Get portfolio breakdown by asset type — what % is in stocks vs mutual funds vs crypto vs gold. "
                    "Use when user asks 'how diversified am I', 'asset allocation', 'am I too heavy in stocks'."
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
    tool_name = payload.get("tool")
    args      = payload.get("args", {})

    if tool_name == "add_holding":
        return await add_holding(
            user_id       = args["user_id"],
            symbol        = args["symbol"],
            name          = args["name"],
            asset_type    = args["asset_type"],
            quantity      = args["quantity"],
            avg_buy_price = args["avg_buy_price"],
            db            = db,
            notes         = args.get("notes"),
        )

    elif tool_name == "get_portfolio":
        return await get_portfolio(user_id=args["user_id"], db=db)

    elif tool_name == "get_live_price":
        # No DB or user_id needed — pure external API call
        return await get_live_price(symbol=args["symbol"])

    elif tool_name == "log_sip":
        return await log_sip(
            user_id = args["user_id"],
            symbol  = args["symbol"],
            amount  = args["amount"],
            db      = db,
        )

    elif tool_name == "get_sip_summary":
        return await get_sip_summary(user_id=args["user_id"], db=db)

    elif tool_name == "get_asset_allocation":
        return await get_asset_allocation(user_id=args["user_id"], db=db)

    else:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")


@app.get("/health")
async def health():
    return {"status": "ok", "server": "invest-server"}
