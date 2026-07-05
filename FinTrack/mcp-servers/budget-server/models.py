from sqlalchemy import Column, String, Numeric, DateTime, Integer, UniqueConstraint, Enum as SAEnum
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
# Same Category values as expense-server. We redefine them here because
# budget-server is an independent service — no shared package between servers.
# In a real monorepo you'd extract shared types to a common lib, but for learning
# MCP the services are intentionally self-contained.

class Category(str, enum.Enum):
    FOOD            = "food"
    TRANSPORT       = "transport"
    SHOPPING        = "shopping"
    ENTERTAINMENT   = "entertainment"
    HEALTH          = "health"
    UTILITIES       = "utilities"
    EMI             = "emi"
    INVESTMENT      = "investment"
    SALARY          = "salary"
    FREELANCE       = "freelance"
    OTHER           = "other"


# ─── SQLAlchemy ORM model ─────────────────────────────────────────────────────
# This is the "budgets" table in PostgreSQL.
# Each row = one user's monthly limit for one spending category.

class Budget(Base):
    __tablename__ = "budgets"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id             = Column(String(100), nullable=False, index=True)
    category            = Column(SAEnum(Category, native_enum=False, length=20), nullable=False)
    monthly_limit       = Column(Numeric(12, 2), nullable=False)

    # When spending reaches this % of the limit, Claude should warn the user.
    # Default is 80 — warn at 80% used. User can customize per category.
    alert_threshold_pct = Column(Integer, default=80, nullable=False)

    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # One budget per category per user. If user sets food budget twice,
    # the second call updates instead of inserting a duplicate.
    __table_args__ = (
        UniqueConstraint("user_id", "category", name="uq_user_category"),
    )


# ─── Pydantic schemas (request / response) ────────────────────────────────────
# These validate incoming JSON before it touches the database.

class BudgetCreate(BaseModel):
    user_id:             str
    category:            Category
    monthly_limit:       Decimal
    alert_threshold_pct: Optional[int] = 80   # optional — defaults to 80%


class BudgetResponse(BaseModel):
    id:                  str
    user_id:             str
    category:            Category
    monthly_limit:       Decimal
    alert_threshold_pct: int
    created_at:          datetime
    updated_at:          datetime

    class Config:
        from_attributes = True  # lets Pydantic read SQLAlchemy ORM objects directly