from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, timedelta


# insights-server reads from three tables it doesn't own:
#   transactions  → expense-server
#   budgets       → budget-server
#   holdings      → invest-server
# All queries use ::text casts on enum columns (same fix as budget-server)
# so PostgreSQL doesn't reject plain string comparisons against native enums.


# ─── Tool 1: get_spending_trends ─────────────────────────────────────────────

async def get_spending_trends(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_spending_trends
    Month-over-month spend per category for the last 3 full months + current month.
    Claude uses this to say 'your food spend went up 40% since last month'.
    """
    result = await db.execute(
        text("""
            SELECT
                TO_CHAR(DATE_TRUNC('month', transaction_at), 'Mon YYYY') AS month,
                category::text AS category,
                SUM(amount) AS total
            FROM transactions
            WHERE user_id    = :user_id
              AND type::text = 'expense'
              AND transaction_at >= NOW() - INTERVAL '4 months'
            GROUP BY 1, 2
            ORDER BY DATE_TRUNC('month', transaction_at), category
        """),
        {"user_id": user_id}
    )
    rows = result.all()

    # Pivot into { month: { category: amount } } structure
    trends: dict = {}
    for month, category, total in rows:
        if month not in trends:
            trends[month] = {}
        trends[month][category] = round(float(total), 2)

    return {
        "has_data": bool(trends),
        "trends":   trends,
        "months":   list(trends.keys()),
    }


# ─── Tool 2: detect_anomalies ─────────────────────────────────────────────────

async def detect_anomalies(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: detect_anomalies
    Flags categories where THIS month's spending is >50% above the 3-month average.
    Claude uses this to proactively warn: 'your entertainment spend this month
    is 2.3x your usual — anything special going on?'
    """
    now         = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    three_ago   = month_start - timedelta(days=90)

    # Current month spend per category
    current_result = await db.execute(
        text("""
            SELECT category::text, SUM(amount)
            FROM transactions
            WHERE user_id          = :user_id
              AND type::text       = 'expense'
              AND transaction_at  >= :month_start
            GROUP BY category::text
        """),
        {"user_id": user_id, "month_start": month_start}
    )
    current = {r[0]: float(r[1]) for r in current_result.all()}

    # 3-month average per category
    avg_result = await db.execute(
        text("""
            SELECT category::text, AVG(monthly_total)
            FROM (
                SELECT category::text,
                       DATE_TRUNC('month', transaction_at) AS month,
                       SUM(amount) AS monthly_total
                FROM transactions
                WHERE user_id         = :user_id
                  AND type::text      = 'expense'
                  AND transaction_at >= :three_ago
                  AND transaction_at  < :month_start
                GROUP BY 1, 2
            ) sub
            GROUP BY category::text
        """),
        {"user_id": user_id, "three_ago": three_ago, "month_start": month_start}
    )
    averages = {r[0]: float(r[1]) for r in avg_result.all()}

    anomalies = []
    for category, this_month in current.items():
        avg = averages.get(category)
        if avg and avg > 0:
            ratio = this_month / avg
            if ratio > 1.5:  # 50% above average
                anomalies.append({
                    "category":       category,
                    "this_month":     round(this_month, 2),
                    "three_month_avg": round(avg, 2),
                    "ratio":          round(ratio, 2),  # e.g. 2.3 = 230% of normal
                })

    anomalies.sort(key=lambda x: x["ratio"], reverse=True)

    return {
        "has_anomalies": bool(anomalies),
        "anomalies":     anomalies,
        "checked_month": month_start.strftime("%B %Y"),
    }


# ─── Tool 3: get_month_forecast ───────────────────────────────────────────────

async def get_month_forecast(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_month_forecast
    Projects end-of-month spend based on current daily burn rate.
    Also returns income and projected savings. Claude uses this mid-month
    to tell the user if they're heading for a deficit.
    """
    now         = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_elapsed   = max(1, (now - month_start).days + 1)
    days_in_month  = 30
    days_remaining = max(0, days_in_month - days_elapsed)

    spend_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id         = :user_id
              AND type::text      = 'expense'
              AND transaction_at >= :month_start
        """),
        {"user_id": user_id, "month_start": month_start}
    )
    total_spent = float(spend_result.scalar() or 0)

    income_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id         = :user_id
              AND type::text      = 'income'
              AND transaction_at >= :month_start
        """),
        {"user_id": user_id, "month_start": month_start}
    )
    total_income = float(income_result.scalar() or 0)

    daily_burn       = round(total_spent / days_elapsed, 2)
    projected_spend  = round(daily_burn * days_in_month, 2)
    projected_savings = round(total_income - projected_spend, 2)

    return {
        "month":             now.strftime("%B %Y"),
        "days_elapsed":      days_elapsed,
        "days_remaining":    days_remaining,
        "spent_so_far":      round(total_spent, 2),
        "income_so_far":     round(total_income, 2),
        "daily_burn_rate":   daily_burn,
        "projected_spend":   projected_spend,
        "projected_savings": projected_savings,
        "on_track":          projected_savings >= 0,
    }


# ─── Tool 4: get_net_worth ────────────────────────────────────────────────────

async def get_net_worth(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_net_worth
    Assets = total invested in holdings + current month savings.
    Liabilities = outstanding EMI (tracked as EMI category expenses, annualised).
    Net worth = assets − liabilities.
    This is a simplified snapshot — Phase 4 will add bank balances and loan tracking.
    """
    # Total portfolio value (using invested amount — live price requires yfinance,
    # which lives in invest-server. Here we use stored total_invested as a proxy.)
    portfolio_result = await db.execute(
        text("SELECT COALESCE(SUM(total_invested), 0) FROM holdings WHERE user_id = :user_id"),
        {"user_id": user_id}
    )
    portfolio_value = float(portfolio_result.scalar() or 0)

    # Lifetime savings estimate: total income minus total expenses ever recorded
    income_result = await db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = :user_id AND type::text = 'income'"),
        {"user_id": user_id}
    )
    total_income = float(income_result.scalar() or 0)

    expense_result = await db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = :user_id AND type::text = 'expense'"),
        {"user_id": user_id}
    )
    total_expense = float(expense_result.scalar() or 0)

    cash_savings = max(0, total_income - total_expense)

    # EMI as a proxy for liabilities — last 12 months of EMI category spend
    emi_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id         = :user_id
              AND type::text      = 'expense'
              AND category::text  = 'emi'
              AND transaction_at >= NOW() - INTERVAL '12 months'
        """),
        {"user_id": user_id}
    )
    annual_emi = float(emi_result.scalar() or 0)

    total_assets      = round(portfolio_value + cash_savings, 2)
    total_liabilities = round(annual_emi, 2)   # annualised EMI as outstanding debt proxy
    net_worth         = round(total_assets - total_liabilities, 2)

    return {
        "assets": {
            "portfolio_invested": round(portfolio_value, 2),
            "cash_savings":       round(cash_savings, 2),
            "total":              total_assets,
        },
        "liabilities": {
            "emi_annual_proxy": total_liabilities,
            "total":            total_liabilities,
        },
        "net_worth": net_worth,
        "note": "Portfolio shown at cost price. Use get_portfolio for live-priced net worth.",
    }


# ─── Tool 5: get_financial_health_score ───────────────────────────────────────

async def get_financial_health_score(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_financial_health_score
    Returns a 0-100 score based on three pillars Claude can explain conversationally:
      1. Savings rate (40 pts): income − expenses / income × 100
      2. Budget adherence (30 pts): how many budgets are under their limit
      3. Portfolio health (30 pts): whether any investments are recorded

    This is a simple heuristic score, not financial advice.
    Claude should always frame it as a starting point, not a verdict.
    """
    now         = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Pillar 1: savings rate this month (max 40 pts)
    income_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id = :user_id AND type::text = 'income' AND transaction_at >= :month_start
        """),
        {"user_id": user_id, "month_start": month_start}
    )
    income = float(income_result.scalar() or 0)

    expense_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id = :user_id AND type::text = 'expense' AND transaction_at >= :month_start
        """),
        {"user_id": user_id, "month_start": month_start}
    )
    expenses = float(expense_result.scalar() or 0)

    savings_rate = ((income - expenses) / income * 100) if income > 0 else 0
    # 20%+ savings rate = full 40 pts. Linear scale below that.
    savings_score = min(40, round((savings_rate / 20) * 40, 1))

    # Pillar 2: budget adherence (max 30 pts)
    budget_result = await db.execute(
        text("""
            SELECT b.category::text, b.monthly_limit,
                   COALESCE(SUM(t.amount), 0) AS spent
            FROM budgets b
            LEFT JOIN transactions t
              ON t.user_id        = b.user_id
             AND t.category::text = b.category::text
             AND t.type::text     = 'expense'
             AND t.transaction_at >= :month_start
            WHERE b.user_id = :user_id
            GROUP BY b.category::text, b.monthly_limit
        """),
        {"user_id": user_id, "month_start": month_start}
    )
    budget_rows = budget_result.all()

    if budget_rows:
        under_budget = sum(1 for r in budget_rows if float(r[2]) <= float(r[1]))
        budget_score = round((under_budget / len(budget_rows)) * 30, 1)
        budgets_set  = len(budget_rows)
    else:
        budget_score = 15  # neutral if no budgets set
        budgets_set  = 0

    # Pillar 3: portfolio health (max 30 pts)
    holdings_result = await db.execute(
        text("SELECT COUNT(*) FROM holdings WHERE user_id = :user_id"),
        {"user_id": user_id}
    )
    holdings_count = int(holdings_result.scalar() or 0)

    # 1+ holdings = 15 pts, 3+ = 25 pts, 5+ = full 30 pts
    if holdings_count >= 5:
        portfolio_score = 30
    elif holdings_count >= 3:
        portfolio_score = 25
    elif holdings_count >= 1:
        portfolio_score = 15
    else:
        portfolio_score = 0

    total_score = round(savings_score + budget_score + portfolio_score)

    return {
        "score":      total_score,
        "max_score":  100,
        "breakdown": {
            "savings_rate_pct":  round(savings_rate, 1),
            "savings_score":     savings_score,
            "budgets_set":       budgets_set,
            "budget_score":      budget_score,
            "holdings_count":    holdings_count,
            "portfolio_score":   portfolio_score,
        },
        "grade": (
            "Excellent" if total_score >= 80 else
            "Good"      if total_score >= 60 else
            "Fair"      if total_score >= 40 else
            "Needs work"
        ),
    }
