from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from models import Budget, BudgetCreate, Category
from datetime import datetime
from decimal import Decimal


# ─── Shared helper ────────────────────────────────────────────────────────────
# Both get_budget_status and check_budget_on_expense need to know how much
# the user spent in a given category this month. Instead of calling expense-server
# over HTTP, we query the shared PostgreSQL database directly.
# Both servers share the same DATABASE_URL, so budget-server can read the
# "transactions" table that expense-server owns. This is the simplest approach
# for Phase 2. In Phase 3+ you'd evaluate whether a proper service-to-service
# HTTP call makes more sense.

async def _get_category_spend_this_month(user_id: str, category: str, db: AsyncSession) -> float:
    """
    Reads expense-server's 'transactions' table directly.
    Returns total spend for this user + category in the current calendar month.
    """
    from sqlalchemy import text

    now         = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Raw SQL is fine here — we're reading a table we don't own, so no ORM model for it.
    # ::text casts are required because 'category' and 'type' are PostgreSQL native enums.
    # Comparing an enum column against a plain string bind parameter silently returns no
    # rows without the cast — the types don't match at the DB level.
    result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id          = :user_id
              AND category::text   = :category
              AND type::text       = 'expense'
              AND transaction_at  >= :month_start
        """),
        {"user_id": user_id, "category": category, "month_start": month_start}
    )
    return float(result.scalar() or 0)


# ─── Tool 1: set_budget ───────────────────────────────────────────────────────

async def set_budget(
    user_id:             str,
    category:            str,
    monthly_limit:       float,
    db:                  AsyncSession,
    alert_threshold_pct: int = 80,
) -> dict:
    """
    MCP Tool: set_budget
    Creates or updates a monthly spending limit for a category.
    Uses PostgreSQL's "upsert" — INSERT ... ON CONFLICT UPDATE — so calling
    set_budget twice for the same category updates the limit, not creates a duplicate.
    """
    cat = Category(category)  # validates the string against the enum

    # PostgreSQL-native upsert. The conflict target is the unique constraint
    # (user_id, category) we defined in models.py.
    stmt = pg_insert(Budget).values(
        user_id             = user_id,
        category            = cat,
        monthly_limit       = Decimal(str(monthly_limit)),
        alert_threshold_pct = alert_threshold_pct,
        created_at          = datetime.utcnow(),
        updated_at          = datetime.utcnow(),
    ).on_conflict_do_update(
        constraint = "uq_user_category",
        set_       = {
            "monthly_limit":       Decimal(str(monthly_limit)),
            "alert_threshold_pct": alert_threshold_pct,
            "updated_at":          datetime.utcnow(),
        }
    ).returning(Budget)

    result = await db.execute(stmt)
    await db.commit()
    budget = result.fetchone()

    return {
        "success":   True,
        "message":   f"Budget set: ₹{monthly_limit}/month for {category}. You'll be warned at {alert_threshold_pct}% usage.",
        "budget": {
            "category":            category,
            "monthly_limit":       monthly_limit,
            "alert_threshold_pct": alert_threshold_pct,
        }
    }


# ─── Tool 2: get_budget_status ────────────────────────────────────────────────

async def get_budget_status(user_id: str, category: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_budget_status
    Returns the full picture for one category budget: limit, spent, remaining,
    percentage used, daily allowance left, and whether the user is on track.
    """
    cat = Category(category)

    # Fetch the budget row
    result = await db.execute(
        select(Budget).where(
            and_(Budget.user_id == user_id, Budget.category == cat)
        )
    )
    budget = result.scalar_one_or_none()

    if not budget:
        return {
            "has_budget": False,
            "category":   category,
            "message":    f"No budget set for {category}. Use set_budget to create one.",
        }

    # How much was actually spent this month
    spent = await _get_category_spend_this_month(user_id, category, db)
    limit = float(budget.monthly_limit)

    remaining    = max(0, limit - spent)
    pct_used     = round((spent / limit * 100) if limit > 0 else 0, 1)

    now              = datetime.utcnow()
    days_in_month    = 30
    days_elapsed     = (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).days + 1
    days_remaining   = max(0, days_in_month - days_elapsed)

    # Daily allowance = what's left divided by days left in the month
    daily_allowance  = round(remaining / days_remaining, 2) if days_remaining > 0 else 0

    # On-track check: if current spend pace projects to exceed the limit
    daily_burn       = round(spent / days_elapsed, 2) if days_elapsed > 0 else 0
    projected_spend  = round(daily_burn * days_in_month, 2)
    on_track         = projected_spend <= limit

    return {
        "has_budget":        True,
        "category":          category,
        "monthly_limit":     limit,
        "spent":             spent,
        "remaining":         remaining,
        "pct_used":          pct_used,
        "days_remaining":    days_remaining,
        "daily_allowance":   daily_allowance,
        "daily_burn_rate":   daily_burn,
        "projected_spend":   projected_spend,
        "on_track":          on_track,
        "alert_threshold":   budget.alert_threshold_pct,
    }


# ─── Tool 3: get_all_budgets ──────────────────────────────────────────────────

async def get_all_budgets(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_all_budgets
    Returns status for every budget the user has set — good for a budget overview screen
    or when the user asks "how am I doing against all my budgets?"
    """
    result = await db.execute(
        select(Budget).where(Budget.user_id == user_id)
    )
    budgets = result.scalars().all()

    if not budgets:
        return {
            "has_budgets": False,
            "message":     "No budgets set yet. Try 'set my food budget to ₹5000'.",
        }

    summaries = []
    total_limit = 0
    total_spent = 0

    for b in budgets:
        spent = await _get_category_spend_this_month(user_id, b.category.value, db)
        limit = float(b.monthly_limit)
        pct   = round((spent / limit * 100) if limit > 0 else 0, 1)

        total_limit += limit
        total_spent += spent

        summaries.append({
            "category":      b.category.value,
            "monthly_limit": limit,
            "spent":         spent,
            "remaining":     round(max(0, limit - spent), 2),
            "pct_used":      pct,
            # Status label for Claude to reason about
            "status":        "exceeded" if pct > 100 else
                             "danger"   if pct >= 95  else
                             "warning"  if pct >= b.alert_threshold_pct else
                             "ok",
        })

    return {
        "has_budgets":   True,
        "budgets":       summaries,
        "total_limit":   total_limit,
        "total_spent":   total_spent,
        "total_remaining": round(max(0, total_limit - total_spent), 2),
    }


# ─── Tool 4: check_budget_on_expense ─────────────────────────────────────────
# This is THE key tool for Phase 2 cross-server chaining.
# After expense-server logs a transaction, Claude calls this tool to check
# if the user is now near or over their budget. Claude then weaves both results
# into a single smart reply — "logged ₹800 on food... and heads up, you're at 92%".

async def check_budget_on_expense(
    user_id:  str,
    category: str,
    db:       AsyncSession,
) -> dict:
    """
    MCP Tool: check_budget_on_expense
    Called right after logging an expense. Checks if the user is near/over their
    budget for that category and returns an alert level Claude can act on.

    Alert levels (Claude should tailor its reply tone to this):
      ok       → spending is fine, no action needed
      warning  → at or past alert threshold (default 80%), gentle nudge
      danger   → at 95%+, stronger warning
      exceeded → over 100%, user has blown the budget
    """
    cat = Category(category)

    result = await db.execute(
        select(Budget).where(
            and_(Budget.user_id == user_id, Budget.category == cat)
        )
    )
    budget = result.scalar_one_or_none()

    # No budget set for this category — that's fine, just tell Claude
    if not budget:
        return {
            "has_budget":   False,
            "category":     category,
            "alert_level":  "ok",
            "message":      f"No budget set for {category}.",
        }

    spent = await _get_category_spend_this_month(user_id, category, db)
    limit = float(budget.monthly_limit)
    pct   = round((spent / limit * 100) if limit > 0 else 0, 1)

    now            = datetime.utcnow()
    days_in_month  = 30
    days_elapsed   = (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).days + 1
    days_remaining = max(0, days_in_month - days_elapsed)
    remaining      = round(max(0, limit - spent), 2)
    daily_left     = round(remaining / days_remaining, 2) if days_remaining > 0 else 0

    # Determine alert level based on percentage used
    if pct > 100:
        alert_level = "exceeded"
    elif pct >= 95:
        alert_level = "danger"
    elif pct >= budget.alert_threshold_pct:
        alert_level = "warning"
    else:
        alert_level = "ok"

    return {
        "has_budget":    True,
        "category":      category,
        "alert_level":   alert_level,   # Claude: use this to set reply tone
        "monthly_limit": limit,
        "spent":         spent,
        "remaining":     remaining,
        "pct_used":      pct,
        "days_remaining": days_remaining,
        "daily_allowance": daily_left,  # how much they can spend per day and still stay in budget
    }


# ─── Tool 5: update_budget ────────────────────────────────────────────────────

async def update_budget(
    user_id:       str,
    category:      str,
    monthly_limit: float,
    db:            AsyncSession,
) -> dict:
    """
    MCP Tool: update_budget
    Changes just the monthly limit on an existing budget.
    Returns error if no budget exists (use set_budget for creation).
    """
    cat = Category(category)

    result = await db.execute(
        select(Budget).where(
            and_(Budget.user_id == user_id, Budget.category == cat)
        )
    )
    budget = result.scalar_one_or_none()

    if not budget:
        return {
            "success": False,
            "message": f"No budget found for {category}. Use set_budget to create one first.",
        }

    budget.monthly_limit = Decimal(str(monthly_limit))
    budget.updated_at    = datetime.utcnow()
    await db.commit()

    return {
        "success":  True,
        "message":  f"Updated {category} budget to ₹{monthly_limit}/month.",
        "category": category,
        "new_limit": monthly_limit,
    }


# ─── Tool 6: delete_budget ────────────────────────────────────────────────────

async def delete_budget(user_id: str, category: str, db: AsyncSession) -> dict:
    """
    MCP Tool: delete_budget
    Removes a budget row. Spending history in the transactions table is unaffected.
    """
    from sqlalchemy import delete

    cat = Category(category)

    result = await db.execute(
        select(Budget).where(
            and_(Budget.user_id == user_id, Budget.category == cat)
        )
    )
    budget = result.scalar_one_or_none()

    if not budget:
        return {"success": False, "message": f"No budget found for {category}."}

    await db.execute(
        delete(Budget).where(
            and_(Budget.user_id == user_id, Budget.category == cat)
        )
    )
    await db.commit()

    return {
        "success":  True,
        "message":  f"Budget for {category} removed.",
        "category": category,
    }


# ─── Tool 7: get_budget_recommendations ──────────────────────────────────────

async def get_budget_recommendations(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_budget_recommendations
    Returns AI-friendly data Claude can use to suggest budget limits.
    Looks at last 30 days of actual spending for each category and flags:
      - Categories where spending > existing limit (budget too tight)
      - Categories with significant spend but NO budget set (gaps to fill)
    Claude then turns this data into natural-language suggestions.
    """
    from sqlalchemy import text
    from datetime import timedelta

    since = datetime.utcnow() - timedelta(days=30)

    # Actual spend per category in last 30 days (from expense-server's table)
    # category::text returns the enum value as a plain string so Python can use it as a dict key
    spend_result = await db.execute(
        text("""
            SELECT category::text, SUM(amount) as total
            FROM transactions
            WHERE user_id         = :user_id
              AND type::text      = 'expense'
              AND transaction_at >= :since
            GROUP BY category
        """),
        {"user_id": user_id, "since": since}
    )
    actual_spend = {row[0]: float(row[1]) for row in spend_result.all()}

    # Existing budgets
    budget_result = await db.execute(
        select(Budget).where(Budget.user_id == user_id)
    )
    existing = {b.category.value: float(b.monthly_limit) for b in budget_result.scalars().all()}

    over_budget   = []  # categories where spending exceeds the set limit
    missing_budget = [] # categories with spend but no budget set

    for category, spent in actual_spend.items():
        if category in existing:
            if spent > existing[category]:
                over_budget.append({
                    "category":    category,
                    "spent":       spent,
                    "limit":       existing[category],
                    "overage":     round(spent - existing[category], 2),
                    "suggestion":  round(spent * 1.1, 2),  # suggest 10% buffer above actual spend
                })
        else:
            # No budget set — suggest one based on actual spending + 10% buffer
            if spent > 100:  # ignore tiny amounts
                missing_budget.append({
                    "category":   category,
                    "spent":      spent,
                    "suggestion": round(spent * 1.1, 2),
                })

    return {
        "over_budget":    over_budget,
        "missing_budget": missing_budget,
        "actual_spend":   actual_spend,
        "existing_limits": existing,
        "has_recommendations": bool(over_budget or missing_budget),
    }