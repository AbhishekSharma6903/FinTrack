from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text
from models import Goal, GoalContribution, GoalType, GoalStatus
from datetime import datetime
from decimal import Decimal
import uuid


# ─── Shared helper: compute progress for a savings goal ──────────────────────

def _compute_savings_progress(goal: Goal, now: datetime = None) -> dict:
    """
    Computes all the forward-looking fields Claude needs to narrate goal progress.
    Extracted as a helper because both get_goals and get_goal_detail use it.

    The key output is `required_monthly` — how much the user needs to save per
    month from today to hit their target by the deadline. This is what turns
    "you've saved ₹15,000" into "you need ₹4,200/month more to hit your target".
    """
    if now is None:
        now = datetime.utcnow()

    current    = float(goal.current_amount or 0)
    target     = float(goal.target_amount or 0)
    remaining  = max(0, target - current)
    pct        = round((current / target * 100) if target > 0 else 0, 1)

    days_remaining    = None
    months_remaining  = None
    required_monthly  = None
    on_track          = None

    if goal.deadline:
        delta          = goal.deadline - now
        days_remaining = max(0, delta.days)
        months_remaining = max(0, round(days_remaining / 30, 1))

        if months_remaining > 0:
            required_monthly = round(remaining / months_remaining, 2)
        else:
            required_monthly = remaining  # deadline passed or today

        # On-track: has the user saved enough proportionally for time elapsed?
        total_days    = max(1, (goal.deadline - goal.created_at).days)
        elapsed_days  = (now - goal.created_at).days
        expected_pct  = min(100, elapsed_days / total_days * 100)
        on_track      = pct >= expected_pct

    # Status label Claude uses to set reply tone
    if goal.status == GoalStatus.COMPLETED or pct >= 100:
        status_label = "completed"
    elif on_track is False and months_remaining is not None and months_remaining < 1:
        status_label = "at_risk"
    elif on_track is False:
        status_label = "behind"
    elif on_track is True:
        status_label = "on_track"
    else:
        status_label = "in_progress"  # no deadline set

    return {
        "current_amount":   round(current, 2),
        "target_amount":    round(target, 2),
        "amount_remaining": round(remaining, 2),
        "percent_complete": pct,
        "days_remaining":   days_remaining,
        "months_remaining": months_remaining,
        "required_monthly": required_monthly,
        "on_track":         on_track,
        "status_label":     status_label,
    }


# ─── Tool 1: create_goal ─────────────────────────────────────────────────────

async def create_goal(
    user_id:        str,
    title:          str,
    goal_type:      str,
    db:             AsyncSession,
    target_amount:  float = None,
    deadline:       str   = None,    # ISO format string from Claude e.g. "2025-12-31"
    habit_category: str   = None,
    habit_limit:    float = None,
    notes:          str   = None,
) -> dict:
    """
    MCP Tool: create_goal
    Creates a savings or habit goal. Claude calls this when the user expresses
    an intention: "I want to save ₹50k for a MacBook by December" or
    "I want to keep my food spending under ₹4k a month".
    """
    gtype = GoalType(goal_type)

    # Parse deadline string to datetime if provided
    deadline_dt = None
    if deadline:
        try:
            deadline_dt = datetime.fromisoformat(deadline.replace("Z", ""))
        except ValueError:
            pass  # invalid date — create goal without deadline

    goal = Goal(
        id             = uuid.uuid4(),
        user_id        = user_id,
        title          = title,
        goal_type      = gtype,
        status         = GoalStatus.ACTIVE,
        target_amount  = Decimal(str(target_amount)) if target_amount else None,
        current_amount = Decimal("0"),
        deadline       = deadline_dt,
        habit_category = habit_category,
        habit_limit    = Decimal(str(habit_limit)) if habit_limit else None,
        notes          = notes,
    )
    db.add(goal)
    await db.commit()
    await db.refresh(goal)

    response = {
        "success":   True,
        "goal_id":   str(goal.id),
        "title":     title,
        "goal_type": goal_type,
        "message":   f"Goal created: '{title}'.",
    }

    if gtype == GoalType.SAVINGS and target_amount:
        response["target_amount"] = target_amount
        response["deadline"]      = deadline_dt.strftime("%d %b %Y") if deadline_dt else None
        if deadline_dt:
            months = max(1, round((deadline_dt - datetime.utcnow()).days / 30, 1))
            response["required_monthly"] = round(target_amount / months, 2)
            response["message"] = (
                f"Savings goal created: save ₹{target_amount:,.0f} by "
                f"{deadline_dt.strftime('%B %Y')}. "
                f"That's ₹{response['required_monthly']:,.2f}/month."
            )

    if gtype == GoalType.HABIT and habit_limit:
        response["habit_category"] = habit_category
        response["habit_limit"]    = habit_limit
        response["message"]        = (
            f"Habit goal created: keep {habit_category} spending under "
            f"₹{habit_limit:,.0f}/month."
        )

    return response


# ─── Tool 2: get_goals ────────────────────────────────────────────────────────

async def get_goals(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_goals
    Returns all active goals with pre-computed progress for each.
    Claude uses this to give a full goal overview.
    """
    result = await db.execute(
        select(Goal).where(
            and_(Goal.user_id == user_id, Goal.status == GoalStatus.ACTIVE)
        ).order_by(Goal.created_at)
    )
    goals = result.scalars().all()

    if not goals:
        return {
            "has_goals": False,
            "message":   "No active goals. Try 'I want to save ₹50,000 for a MacBook by December'.",
        }

    goal_list = []
    for g in goals:
        entry = {
            "goal_id":   str(g.id),
            "title":     g.title,
            "goal_type": g.goal_type.value,
            "status":    g.status.value,
        }
        if g.goal_type == GoalType.SAVINGS:
            entry.update(_compute_savings_progress(g))
        else:
            entry["habit_category"] = g.habit_category
            entry["habit_limit"]    = float(g.habit_limit or 0)
        goal_list.append(entry)

    return {
        "has_goals":   True,
        "goals":       goal_list,
        "total_goals": len(goal_list),
    }


# ─── Tool 3: add_goal_contribution ───────────────────────────────────────────

async def add_goal_contribution(
    user_id: str,
    goal_id: str,
    amount:  float,
    db:      AsyncSession,
    note:    str = None,
) -> dict:
    """
    MCP Tool: add_goal_contribution
    Logs a deposit toward a savings goal and recomputes progress.
    This is the tool that makes savings goals feel alive — each deposit
    brings the user closer and Claude can show the updated trajectory.

    Example: "I put 5000 toward my MacBook fund"
    → logs contribution, updates current_amount, returns new progress + required_monthly
    """
    # Fetch the goal
    result = await db.execute(
        select(Goal).where(
            and_(Goal.id == uuid.UUID(goal_id), Goal.user_id == user_id)
        )
    )
    goal = result.scalar_one_or_none()

    if not goal:
        return {"success": False, "message": f"Goal not found: {goal_id}"}
    if goal.goal_type != GoalType.SAVINGS:
        return {"success": False, "message": "Contributions can only be added to savings goals."}
    if goal.status != GoalStatus.ACTIVE:
        return {"success": False, "message": f"Goal is {goal.status.value}, not active."}

    # Log the contribution row (history)
    contribution = GoalContribution(
        id         = uuid.uuid4(),
        user_id    = user_id,
        goal_id    = goal.id,
        amount     = Decimal(str(amount)),
        note       = note,
    )
    db.add(contribution)

    # Update the running total on the goal row
    goal.current_amount = Decimal(str(round(float(goal.current_amount or 0) + amount, 2)))
    goal.updated_at     = datetime.utcnow()

    # Auto-complete if target reached
    if float(goal.current_amount) >= float(goal.target_amount or 0):
        goal.status = GoalStatus.COMPLETED

    await db.commit()

    # Count total contributions for context
    count_result = await db.execute(
        select(func.count(GoalContribution.id)).where(
            GoalContribution.goal_id == goal.id
        )
    )
    contribution_count = int(count_result.scalar() or 0)

    progress = _compute_savings_progress(goal)

    return {
        "success":             True,
        "goal_id":             str(goal.id),
        "title":               goal.title,
        "this_contribution":   amount,
        "contribution_number": contribution_count,
        "auto_completed":      goal.status == GoalStatus.COMPLETED,
        **progress,   # spreads all progress fields into the top-level response
        "message": (
            f"🎉 Goal completed! You've saved ₹{float(goal.current_amount):,.0f}!"
            if goal.status == GoalStatus.COMPLETED else
            f"₹{amount:,.0f} added. You're {progress['percent_complete']}% there. "
            f"₹{progress['amount_remaining']:,.0f} to go."
            + (f" Need ₹{progress['required_monthly']:,.0f}/month to hit your deadline."
               if progress['required_monthly'] else "")
        )
    }


# ─── Tool 4: get_goal_detail ─────────────────────────────────────────────────

async def get_goal_detail(user_id: str, goal_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_goal_detail
    Full detail for one goal including contribution history.
    Use when the user asks about a specific goal by name or when Claude needs
    richer data than get_goals provides.
    """
    result = await db.execute(
        select(Goal).where(
            and_(Goal.id == uuid.UUID(goal_id), Goal.user_id == user_id)
        )
    )
    goal = result.scalar_one_or_none()

    if not goal:
        return {"success": False, "message": f"Goal not found."}

    detail = {
        "goal_id":   str(goal.id),
        "title":     goal.title,
        "goal_type": goal.goal_type.value,
        "status":    goal.status.value,
        "notes":     goal.notes,
        "created":   goal.created_at.strftime("%d %b %Y"),
    }

    if goal.goal_type == GoalType.SAVINGS:
        # Full progress computation
        detail.update(_compute_savings_progress(goal))

        # Contribution history
        contribs_result = await db.execute(
            select(GoalContribution)
            .where(GoalContribution.goal_id == goal.id)
            .order_by(GoalContribution.created_at.desc())
            .limit(10)
        )
        contributions = contribs_result.scalars().all()
        detail["contributions"] = [
            {
                "amount": float(c.amount),
                "note":   c.note,
                "date":   c.created_at.strftime("%d %b %Y"),
            }
            for c in contributions
        ]
        detail["total_contributions"] = len(contributions)

    else:
        detail["habit_category"] = goal.habit_category
        detail["habit_limit"]    = float(goal.habit_limit or 0)

    return detail


# ─── Tool 5: update_goal ─────────────────────────────────────────────────────

async def update_goal(
    user_id:       str,
    goal_id:       str,
    db:            AsyncSession,
    title:         str   = None,
    target_amount: float = None,
    deadline:      str   = None,
    habit_limit:   float = None,
    notes:         str   = None,
) -> dict:
    """
    MCP Tool: update_goal
    Edit a goal's title, target, deadline, or habit limit.
    Only provided fields are updated; others stay unchanged.
    """
    result = await db.execute(
        select(Goal).where(
            and_(Goal.id == uuid.UUID(goal_id), Goal.user_id == user_id)
        )
    )
    goal = result.scalar_one_or_none()

    if not goal:
        return {"success": False, "message": "Goal not found."}

    if title:         goal.title         = title
    if target_amount: goal.target_amount = Decimal(str(target_amount))
    if habit_limit:   goal.habit_limit   = Decimal(str(habit_limit))
    if notes:         goal.notes         = notes
    if deadline:
        try:
            goal.deadline = datetime.fromisoformat(deadline.replace("Z", ""))
        except ValueError:
            pass

    goal.updated_at = datetime.utcnow()
    await db.commit()

    return {
        "success": True,
        "goal_id": goal_id,
        "message": f"Goal '{goal.title}' updated.",
    }


# ─── Tool 6: complete_goal ────────────────────────────────────────────────────

async def complete_goal(user_id: str, goal_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: complete_goal
    Manually mark a goal as completed (e.g. user bought the item they were saving for).
    """
    result = await db.execute(
        select(Goal).where(
            and_(Goal.id == uuid.UUID(goal_id), Goal.user_id == user_id)
        )
    )
    goal = result.scalar_one_or_none()

    if not goal:
        return {"success": False, "message": "Goal not found."}

    goal.status     = GoalStatus.COMPLETED
    goal.updated_at = datetime.utcnow()
    await db.commit()

    return {
        "success": True,
        "title":   goal.title,
        "message": f"🎉 Goal '{goal.title}' marked as completed! Great work!",
    }


# ─── Tool 7: get_habit_status ─────────────────────────────────────────────────

async def get_habit_status(user_id: str, db: AsyncSession) -> dict:
    """
    MCP Tool: get_habit_status
    For all active habit goals — how much has the user spent this month
    vs their self-imposed limit for each category?

    This is cross-server data access: reads from expense-server's 'transactions'
    table directly. Uses the ::text cast because category and type are stored as
    PostgreSQL enum-typed VARCHAR columns — plain string comparisons fail silently
    without the cast.
    """
    # Fetch all active habit goals
    result = await db.execute(
        select(Goal).where(
            and_(
                Goal.user_id  == user_id,
                Goal.goal_type == GoalType.HABIT,
                Goal.status    == GoalStatus.ACTIVE,
            )
        )
    )
    habits = result.scalars().all()

    if not habits:
        return {
            "has_habits": False,
            "message":    "No habit goals set. Try 'I want to keep food spending under ₹4000/month'.",
        }

    now         = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month  = 30
    days_elapsed   = max(1, (now - month_start).days + 1)
    days_remaining = max(0, days_in_month - days_elapsed)

    habit_statuses = []

    for habit in habits:
        # Cross-table query with ::text cast (see docstring above for why)
        spend_result = await db.execute(
            text("""
                SELECT COALESCE(SUM(amount), 0)
                FROM transactions
                WHERE user_id          = :user_id
                  AND category::text   = :category
                  AND type::text       = 'expense'
                  AND transaction_at  >= :month_start
            """),
            {
                "user_id":    user_id,
                "category":   habit.habit_category,
                "month_start": month_start,
            }
        )
        spent = float(spend_result.scalar() or 0)
        limit = float(habit.habit_limit or 0)
        pct   = round((spent / limit * 100) if limit > 0 else 0, 1)

        remaining     = round(max(0, limit - spent), 2)
        daily_allowed = round(remaining / days_remaining, 2) if days_remaining > 0 else 0

        # Same alert levels as budget-server for consistency
        if pct > 100:
            alert_level = "exceeded"
        elif pct >= 95:
            alert_level = "danger"
        elif pct >= 80:
            alert_level = "warning"
        else:
            alert_level = "ok"

        habit_statuses.append({
            "goal_id":       str(habit.id),
            "title":         habit.title,
            "category":      habit.habit_category,
            "monthly_limit": limit,
            "spent":         spent,
            "remaining":     remaining,
            "pct_used":      pct,
            "days_remaining": days_remaining,
            "daily_allowed": daily_allowed,
            "alert_level":   alert_level,
        })

    return {
        "has_habits":  True,
        "habits":      habit_statuses,
        "checked_month": month_start.strftime("%B %Y"),
    }
