from sqlalchemy import Column, String, Numeric, DateTime, Text, Boolean, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.postgresql import UUID
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from decimal import Decimal
import uuid
import enum


class Base(DeclarativeBase):
    pass


# ─── Enums ────────────────────────────────────────────────────────────────────

class GoalType(str, enum.Enum):
    SAVINGS = "savings"   # target amount + deadline — "save ₹50k for MacBook by December"
    HABIT   = "habit"     # recurring spend cap — "keep food under ₹4k/month"


class GoalStatus(str, enum.Enum):
    ACTIVE    = "active"
    COMPLETED = "completed"
    PAUSED    = "paused"
    CANCELLED = "cancelled"


# ─── SQLAlchemy ORM models ────────────────────────────────────────────────────

class Goal(Base):
    """
    One row per goal per user. A goal persists across months — this is what
    makes goals different from expenses (point-in-time) or budgets (current-month).
    A savings goal accumulates progress; a habit goal resets each month.
    """
    __tablename__ = "goals"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = Column(String(100), nullable=False, index=True)
    title          = Column(String(200), nullable=False)
    goal_type      = Column(SAEnum(GoalType, native_enum=False, length=20), nullable=False)
    status         = Column(SAEnum(GoalStatus, native_enum=False, length=20), default=GoalStatus.ACTIVE, nullable=False)

    # ── Savings goal fields (nullable for habit goals) ────────────────────────
    target_amount  = Column(Numeric(14, 2), nullable=True)   # e.g. 50000
    current_amount = Column(Numeric(14, 2), default=0)       # accumulated so far
    deadline       = Column(DateTime, nullable=True)         # target completion date

    # ── Habit goal fields (nullable for savings goals) ────────────────────────
    habit_category = Column(String(20), nullable=True)       # e.g. "food", "shopping"
    habit_limit    = Column(Numeric(12, 2), nullable=True)   # monthly spend cap

    notes          = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GoalContribution(Base):
    """
    One row per deposit toward a savings goal.
    Separate from the goal row so we have a full history Claude can narrate.
    "You've made 3 deposits totalling ₹15,000" — that data lives here.
    Habit goals don't use contributions (they read from transactions instead).
    """
    __tablename__ = "goal_contributions"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = Column(String(100), nullable=False, index=True)
    goal_id    = Column(UUID(as_uuid=True), ForeignKey("goals.id"), nullable=False)
    amount     = Column(Numeric(12, 2), nullable=False)
    note       = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class GoalCreate(BaseModel):
    user_id:        str
    title:          str
    goal_type:      GoalType
    target_amount:  Optional[Decimal] = None
    deadline:       Optional[datetime] = None
    habit_category: Optional[str] = None
    habit_limit:    Optional[Decimal] = None
    notes:          Optional[str] = None


class GoalResponse(BaseModel):
    id:             str
    user_id:        str
    title:          str
    goal_type:      GoalType
    status:         GoalStatus
    target_amount:  Optional[Decimal]
    current_amount: Optional[Decimal]
    deadline:       Optional[datetime]
    habit_category: Optional[str]
    habit_limit:    Optional[Decimal]
    notes:          Optional[str]
    created_at:     datetime

    class Config:
        from_attributes = True
