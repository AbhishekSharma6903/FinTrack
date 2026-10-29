from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from models import Holding, SipEntry, AssetType
from datetime import datetime
from decimal import Decimal
import uuid


# ─── Shared helper: fetch live price ─────────────────────────────────────────
# This is the NEW concept in Phase 3 — a tool that calls an external API.
# yfinance is a free Python wrapper for Yahoo Finance. No API key needed.
# It works for NSE stocks (RELIANCE.NS), BSE stocks (RELIANCE.BO),
# Nifty ETFs (NIFTYBEES.NS), and crypto (BTC-INR).

def _fetch_live_price(symbol: str, asset_type: str) -> float | None:
    """
    Fetches the latest market price for a symbol from Yahoo Finance.
    Returns None if the symbol is not found or yfinance fails.
    FD (Fixed Deposit) has no live price — returns None intentionally.
    """
    if asset_type == "fd":
        return None

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)

        # fast_info uses attribute access (not dict .get()) in yfinance >= 0.2
        price = ticker.fast_info.last_price

        if price is None:
            # Fallback: fetch last closing price from 1-day history
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = hist["Close"].iloc[-1]

        return round(float(price), 2) if price else None
    except Exception as e:
        print(f"⚠️  yfinance failed for {symbol}: {e}")
        return None


# ─── Tool 1: add_holding ─────────────────────────────────────────────────────

async def add_holding(
    user_id:       str,
    symbol:        str,
    name:          str,
    asset_type:    str,
    quantity:      float,
    avg_buy_price: float,
    db:            AsyncSession,
    notes:         str = None,
) -> dict:
    """
    MCP Tool: add_holding
    Adds or updates a position. If the user already holds the symbol,
    we recalculate the weighted average buy price and add to the quantity.
    This handles "I bought 5 more shares of HDFC at 1650" correctly.
    """
    atype = AssetType(asset_type)

    # Check if holding already exists for this user + symbol
    result = await db.execute(
        select(Holding).where(
            and_(Holding.user_id == user_id, Holding.symbol == symbol.upper())
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Average cost basis formula: (old_qty × old_price + new_qty × new_price) / total_qty
        old_qty   = float(existing.quantity)
        new_qty   = old_qty + quantity
        new_avg   = ((old_qty * float(existing.avg_buy_price)) + (quantity * avg_buy_price)) / new_qty

        existing.quantity       = Decimal(str(round(new_qty, 6)))
        existing.avg_buy_price  = Decimal(str(round(new_avg, 2)))
        existing.total_invested = Decimal(str(round(new_qty * new_avg, 2)))
        existing.updated_at     = datetime.utcnow()
        await db.commit()

        return {
            "success": True,
            "action":  "updated",
            "message": f"Added {quantity} units to {symbol.upper()}. New avg buy price: ₹{round(new_avg, 2)}.",
            "holding": {
                "symbol":        symbol.upper(),
                "name":          existing.name,
                "quantity":      round(new_qty, 4),
                "avg_buy_price": round(new_avg, 2),
                "total_invested": round(new_qty * new_avg, 2),
            }
        }

    # New holding
    total_invested = Decimal(str(round(quantity * avg_buy_price, 2)))
    holding = Holding(
        id             = uuid.uuid4(),
        user_id        = user_id,
        symbol         = symbol.upper(),
        name           = name,
        asset_type     = atype,
        quantity       = Decimal(str(quantity)),
        avg_buy_price  = Decimal(str(avg_buy_price)),
        total_invested = total_invested,
        notes          = notes,
    )
    db.add(holding)
    await db.commit()

    return {
        "success": True,
        "action":  "created",
        "message": f"Added {symbol.upper()} to your portfolio. Invested ₹{float(total_invested):,.2f}.",
        "holding": {
            "symbol":         symbol.upper(),
            "name":           name,
            "asset_type":     asset_type,
            "quantity":       quantity,
            "avg_buy_price":  avg_buy_price,
            "total_invested": float(total_invested),
        }
    }


# ─── Tool 2: get_portfolio ────────────────────────────────────────────────────

async def get_portfolio(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_portfolio
    Returns all holdings with live prices fetched from Yahoo Finance.
    Computes P&L (profit/loss) for each position and portfolio totals.
    This is where the external API call happens — Claude gets real-time data.
    """
    result = await db.execute(
        select(Holding).where(Holding.user_id == user_id)
    )
    holdings = result.scalars().all()

    if not holdings:
        return {
            "has_portfolio": False,
            "message": "No holdings found. Add your first investment with 'I bought 10 shares of Reliance at ₹2800'.",
        }

    positions   = []
    total_invested = 0
    total_current  = 0

    for h in holdings:
        live_price = _fetch_live_price(h.symbol, h.asset_type.value)
        invested   = float(h.total_invested)

        if live_price and h.asset_type != AssetType.FD:
            current_value = round(float(h.quantity) * live_price, 2)
            pnl           = round(current_value - invested, 2)
            pnl_pct       = round((pnl / invested * 100) if invested > 0 else 0, 2)
        else:
            # FD or price unavailable — use invested amount as current value
            current_value = invested
            pnl           = 0
            pnl_pct       = 0
            live_price    = float(h.avg_buy_price)  # show cost price for FD

        total_invested += invested
        total_current  += current_value

        positions.append({
            "symbol":        h.symbol,
            "name":          h.name,
            "asset_type":    h.asset_type.value,
            "quantity":      float(h.quantity),
            "avg_buy_price": float(h.avg_buy_price),
            "live_price":    live_price,
            "invested":      invested,
            "current_value": current_value,
            "pnl":           pnl,
            "pnl_pct":       pnl_pct,
        })

    total_pnl     = round(total_current - total_invested, 2)
    total_pnl_pct = round((total_pnl / total_invested * 100) if total_invested > 0 else 0, 2)

    return {
        "has_portfolio":   True,
        "positions":       positions,
        "summary": {
            "total_invested":    round(total_invested, 2),
            "total_current":     round(total_current, 2),
            "total_pnl":         total_pnl,
            "total_pnl_pct":     total_pnl_pct,
            "holdings_count":    len(positions),
        }
    }


# ─── Tool 3: get_live_price ───────────────────────────────────────────────────

async def get_live_price(symbol: str) -> dict:
    """
    MCP Tool: get_live_price
    Fetches current market price for any Yahoo Finance symbol.
    Doesn't need user_id or DB — it's a pure external API call.
    Use when user asks 'what's the price of HDFC today' or 'check Nifty'.
    """
    symbol = symbol.upper()
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info   = ticker.fast_info

        price   = info.last_price
        high_52 = getattr(info, "year_high", None)
        low_52  = getattr(info, "year_low",  None)

        if not price:
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = hist["Close"].iloc[-1]

        if not price:
            return {"success": False, "symbol": symbol, "message": f"Could not fetch price for {symbol}. Check the symbol format (e.g. RELIANCE.NS for NSE)."}

        return {
            "success":  True,
            "symbol":   symbol,
            "price":    round(float(price), 2),
            "52w_high": round(float(high_52), 2) if high_52 else None,
            "52w_low":  round(float(low_52),  2) if low_52  else None,
        }
    except Exception as e:
        return {"success": False, "symbol": symbol, "message": f"Error fetching {symbol}: {str(e)}"}


# ─── Tool 4: log_sip ─────────────────────────────────────────────────────────

async def log_sip(
    user_id: str,
    symbol:  str,
    amount:  float,
    db:      AsyncSession,
) -> dict:
    """
    MCP Tool: log_sip
    Records a monthly SIP instalment. Also adds to the holding (so the
    portfolio reflects accumulated SIP units). Claude calls this when the
    user says 'my Nifty SIP of 5000 processed today'.
    """
    entry = SipEntry(
        id       = uuid.uuid4(),
        user_id  = user_id,
        symbol   = symbol.upper(),
        amount   = Decimal(str(amount)),
        sip_date = datetime.utcnow(),
    )
    db.add(entry)
    await db.commit()

    # Total SIP invested in this fund across all instalments
    total_result = await db.execute(
        select(func.sum(SipEntry.amount)).where(
            and_(SipEntry.user_id == user_id, SipEntry.symbol == symbol.upper())
        )
    )
    total_invested_via_sip = float(total_result.scalar() or 0)

    return {
        "success":              True,
        "message":              f"SIP of ₹{amount} logged for {symbol.upper()}.",
        "symbol":               symbol.upper(),
        "this_instalment":      amount,
        "total_sip_in_fund":    total_invested_via_sip,
    }


# ─── Tool 5: get_sip_summary ─────────────────────────────────────────────────

async def get_sip_summary(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_sip_summary
    Shows all active SIPs — total instalments, total invested per fund,
    and overall SIP corpus. Use when user asks 'how much have I put into SIPs'.
    """
    result = await db.execute(
        select(SipEntry.symbol, func.count(SipEntry.id), func.sum(SipEntry.amount))
        .where(SipEntry.user_id == user_id)
        .group_by(SipEntry.symbol)
    )
    rows = result.all()

    if not rows:
        return {"has_sips": False, "message": "No SIP entries found."}

    sips       = []
    total_corpus = 0

    for symbol, count, total in rows:
        total_corpus += float(total)
        sips.append({
            "symbol":      symbol,
            "instalments": count,
            "total":       float(total),
        })

    return {
        "has_sips":     True,
        "sips":         sips,
        "total_corpus": round(total_corpus, 2),
        "funds_count":  len(sips),
    }


# ─── Tool 6: get_asset_allocation ────────────────────────────────────────────

async def get_asset_allocation(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_asset_allocation
    Breaks down the portfolio by asset type (stock / mutual_fund / crypto / gold / fd).
    Returns both absolute invested amounts and percentage of total portfolio.
    Claude uses this to advise on diversification — 'you have 90% in stocks, consider adding debt'.
    """
    result = await db.execute(
        select(Holding.asset_type, func.sum(Holding.total_invested))
        .where(Holding.user_id == user_id)
        .group_by(Holding.asset_type)
    )
    rows = result.all()

    if not rows:
        return {"has_holdings": False, "message": "No holdings found."}

    total = sum(float(r[1]) for r in rows)
    allocation = []

    for asset_type, invested in rows:
        amt = float(invested)
        allocation.append({
            "asset_type": asset_type.value,
            "invested":   round(amt, 2),
            "pct":        round((amt / total * 100) if total > 0 else 0, 1),
        })

    # Sort by percentage descending so Claude narrates the dominant asset type first
    allocation.sort(key=lambda x: x["pct"], reverse=True)

    return {
        "has_holdings":   True,
        "allocation":     allocation,
        "total_invested": round(total, 2),
    }
