from sqlalchemy import Column, String, Numeric, DateTime, Text, Enum as SAEnum
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

class TransactionType(str, enum.Enum):
    EXPENSE = "expense"
    INCOME  = "income"


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


class PaymentMethod(str, enum.Enum):
    UPI         = "upi"
    CASH        = "cash"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD  = "debit_card"
    NET_BANKING = "net_banking"
    OTHER       = "other"


# ─── SQLAlchemy ORM model ─────────────────────────────────────────────────────

class Transaction(Base):
    __tablename__ = "transactions"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = Column(String(100), nullable=False, index=True)
    type           = Column(SAEnum(TransactionType, native_enum=False, length=20), nullable=False)
    amount         = Column(Numeric(12, 2), nullable=False)
    category       = Column(SAEnum(Category, native_enum=False, length=20), nullable=False)
    description    = Column(Text, nullable=True)
    payment_method = Column(SAEnum(PaymentMethod, native_enum=False, length=20), default=PaymentMethod.UPI)
    merchant       = Column(String(200), nullable=True)
    notes          = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    transaction_at = Column(DateTime, default=datetime.utcnow)  # actual time of expense


# ─── Pydantic schemas (request / response) ────────────────────────────────────

class TransactionCreate(BaseModel):
    user_id:        str
    type:           TransactionType
    amount:         Decimal
    category:       Category
    description:    Optional[str] = None
    payment_method: Optional[PaymentMethod] = PaymentMethod.UPI
    merchant:       Optional[str] = None
    notes:          Optional[str] = None
    transaction_at: Optional[datetime] = None


class TransactionResponse(BaseModel):
    id:             str
    user_id:        str
    type:           TransactionType
    amount:         Decimal
    category:       Category
    description:    Optional[str]
    payment_method: PaymentMethod
    merchant:       Optional[str]
    notes:          Optional[str]
    created_at:     datetime
    transaction_at: datetime

    class Config:
        from_attributes = True