from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from models import Transaction, TransactionCreate, Category, TransactionType
from datetime import datetime, timedelta
from decimal import Decimal
import uuid


async def log_transaction(data: TransactionCreate, db: AsyncSession) -> dict:
    """
    MCP Tool: log_expense / log_income
    Saves a transaction and returns context the LLM can reason about.

    Design note on duplicates:
      We INTENTIONALLY do not block duplicates at the DB level. False-positive
      dedup is more annoying than letting an occasional dupe through. Instead
      we surface a 'recent_similar' field so the assistant can gently ask the
      user if the repeat was intentional. User can always call delete_transaction
      to remove any mistake.
    """
    now = datetime.utcnow()

    # ── Gentle duplicate awareness (NOT blocking) ─────────────────────────────
    # Find transactions in the last 5 minutes with the same user+type+amount+category.
    # This informs the LLM — it does NOT prevent the insert.
    awareness_window = now - timedelta(minutes=5)
    recent_similar_result = await db.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id        == data.user_id,
                Transaction.type           == data.type,
                Transaction.amount         == data.amount,
                Transaction.category       == data.category,
                Transaction.transaction_at >= awareness_window,
            )
        ).order_by(Transaction.transaction_at.desc()).limit(3)
    )
    recent_similar = [
        {
            "id":          str(r.id),
            "amount":      float(r.amount),
            "description": r.description,
            "logged_at":   r.transaction_at.isoformat(),
            "age_seconds": int((now - r.transaction_at).total_seconds()),
        }
        for r in recent_similar_result.scalars().all()
    ]

    # ── Insert the new transaction (always, even if similar ones exist) ──────
    tx = Transaction(
        id             = uuid.uuid4(),
        user_id        = data.user_id,
        type           = data.type,
        amount         = data.amount,
        category       = data.category,
        description    = data.description,
        payment_method = data.payment_method,
        merchant       = data.merchant,
        notes          = data.notes,
        transaction_at = data.transaction_at or now,
    )
    db.add(tx)
    await db.commit()
    await db.refresh(tx)

    # ── Give the LLM rich context to reason about ────────────────────────────
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Monthly total for this category
    category_total_result = await db.execute(
        select(func.sum(Transaction.amount)).where(
            and_(
                Transaction.user_id == data.user_id,
                Transaction.category == data.category,
                Transaction.type == TransactionType.EXPENSE,
                Transaction.transaction_at >= month_start,
            )
        )
    )
    category_month_total = float(category_total_result.scalar() or 0)

    # Monthly total spend across all categories
    total_spend_result = await db.execute(
        select(func.sum(Transaction.amount)).where(
            and_(
                Transaction.user_id == data.user_id,
                Transaction.type == TransactionType.EXPENSE,
                Transaction.transaction_at >= month_start,
            )
        )
    )
    month_total_spend = float(total_spend_result.scalar() or 0)

    # Monthly income
    income_result = await db.execute(
        select(func.sum(Transaction.amount)).where(
            and_(
                Transaction.user_id == data.user_id,
                Transaction.type == TransactionType.INCOME,
                Transaction.transaction_at >= month_start,
            )
        )
    )
    month_income = float(income_result.scalar() or 0)

    days_in_month   = 30
    days_elapsed    = (now - month_start).days + 1
    days_remaining  = days_in_month - days_elapsed
    burn_rate       = month_total_spend / days_elapsed if days_elapsed > 0 else 0
    projected_spend = burn_rate * days_in_month

    return {
        "success": True,
        "transaction_id": str(tx.id),
        "logged": {
            "amount":      float(data.amount),
            "category":    data.category.value,
            "description": data.description,
            "type":        data.type.value,
        },
        "monthly_context": {
            "category_spend_this_month": category_month_total,
            "total_spend_this_month":    month_total_spend,
            "total_income_this_month":   month_income,
            "days_elapsed":              days_elapsed,
            "days_remaining":            days_remaining,
            "daily_burn_rate":           round(burn_rate, 2),
            "projected_month_spend":     round(projected_spend, 2),
            "savings_so_far":            round(month_income - month_total_spend, 2),
        },
        # Non-blocking duplicate awareness: list of recent transactions matching
        # this one's user+type+amount+category in the last 5 minutes.
        # Empty list = no concerns. Non-empty = assistant should gently ask the user.
        "recent_similar": recent_similar,
        "message": f"Transaction of ₹{data.amount} logged under {data.category.value}.",
    }


async def get_expenses(
    user_id: str,
    db: AsyncSession,
    category: str = None,
    days: int = 30,
    limit: int = 20,
) -> dict:
    """
    MCP Tool: get_expenses
    Returns recent transactions with summary stats.
    """
    since = datetime.utcnow() - timedelta(days=days)

    filters = [
        Transaction.user_id == user_id,
        Transaction.transaction_at >= since,
    ]
    if category:
        try:
            filters.append(Transaction.category == Category(category))
        except ValueError:
            pass

    result = await db.execute(
        select(Transaction)
        .where(and_(*filters))
        .order_by(Transaction.transaction_at.desc())
        .limit(limit)
    )
    transactions = result.scalars().all()

    # Summary by category
    summary_result = await db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(and_(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.transaction_at >= since,
        ))
        .group_by(Transaction.category)
    )
    category_summary = {
        row[0].value: float(row[1]) for row in summary_result.all()
    }

    return {
        "transactions": [
            {
                "id":          str(tx.id),
                "amount":      float(tx.amount),
                "category":    tx.category.value,
                "type":        tx.type.value,
                "description": tx.description,
                "merchant":    tx.merchant,
                "date":        tx.transaction_at.isoformat(),
            }
            for tx in transactions
        ],
        "summary": {
            "total_transactions": len(transactions),
            "by_category":        category_summary,
            "period_days":        days,
        }
    }


async def get_monthly_summary(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_monthly_summary
    Full financial picture for the current month.
    """
    now         = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    expense_result = await db.execute(
        select(Transaction.category, func.sum(Transaction.amount))
        .where(and_(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.EXPENSE,
            Transaction.transaction_at >= month_start,
        ))
        .group_by(Transaction.category)
    )
    expenses_by_category = {
        row[0].value: float(row[1]) for row in expense_result.all()
    }

    income_result = await db.execute(
        select(func.sum(Transaction.amount)).where(and_(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.INCOME,
            Transaction.transaction_at >= month_start,
        ))
    )
    total_income  = float(income_result.scalar() or 0)
    total_expense = sum(expenses_by_category.values())
    savings       = total_income - total_expense
    savings_rate  = (savings / total_income * 100) if total_income > 0 else 0

    days_elapsed   = (now - month_start).days + 1
    days_remaining = 30 - days_elapsed

    return {
        "month":               now.strftime("%B %Y"),
        "income":              total_income,
        "total_expense":       total_expense,
        "savings":             round(savings, 2),
        "savings_rate_pct":    round(savings_rate, 2),
        "expenses_by_category": expenses_by_category,
        "days_elapsed":        days_elapsed,
        "days_remaining":      days_remaining,
        "top_category":        max(expenses_by_category, key=expenses_by_category.get)
                               if expenses_by_category else None,
    }


async def delete_transaction(transaction_id: str, user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: delete_transaction
    Permanently removes a single transaction by ID.

    Safety: requires user_id — a transaction can only be deleted by the user
    who owns it. If the transaction doesn't exist or belongs to someone else,
    we return success=False so the assistant can tell the user it couldn't
    find the transaction (without leaking info about other users' data).
    """
    try:
        tx_uuid = uuid.UUID(transaction_id)
    except (ValueError, TypeError):
        return {
            "success": False,
            "message": f"Invalid transaction_id format: '{transaction_id}' is not a valid UUID.",
        }

    result = await db.execute(
        select(Transaction).where(
            and_(
                Transaction.id      == tx_uuid,
                Transaction.user_id == user_id,
            )
        )
    )
    tx = result.scalar_one_or_none()

    if tx is None:
        return {
            "success": False,
            "transaction_id": transaction_id,
            "message": "No transaction found with that ID for this user. It may have already been deleted.",
        }

    # Snapshot the fields BEFORE deletion so we can echo them back in the response.
    deleted_snapshot = {
        "transaction_id": str(tx.id),
        "amount":         float(tx.amount),
        "category":       tx.category.value,
        "type":           tx.type.value,
        "description":    tx.description,
        "merchant":       tx.merchant,
        "transaction_at": tx.transaction_at.isoformat(),
    }

    await db.delete(tx)
    await db.commit()

    return {
        "success": True,
        "deleted": deleted_snapshot,
        "message": (
            f"Deleted {tx.type.value} of ₹{tx.amount} ({tx.category.value}"
            + (f" — {tx.description}" if tx.description else "")
            + ")."
        ),
    }

