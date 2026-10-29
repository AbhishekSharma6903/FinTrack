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

class AssetType(str, enum.Enum):
    STOCK       = "stock"
    MUTUAL_FUND = "mutual_fund"
    CRYPTO      = "crypto"
    GOLD        = "gold"
    FD          = "fd"          # Fixed Deposit — no live price, stored manually


# ─── SQLAlchemy ORM models ────────────────────────────────────────────────────

class Holding(Base):
    """
    One row per user per symbol. Stores the user's position in a single asset.
    When the user buys more of the same symbol, we update avg_buy_price and quantity
    (average cost basis calculation) rather than inserting a new row.
    """
    __tablename__ = "holdings"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = Column(String(100), nullable=False, index=True)
    symbol         = Column(String(20),  nullable=False)   # e.g. "RELIANCE.NS"
    name           = Column(String(200), nullable=False)   # e.g. "Reliance Industries"
    asset_type     = Column(SAEnum(AssetType, native_enum=False, length=20), nullable=False)
    quantity       = Column(Numeric(18, 6), nullable=False)  # 6 decimals for MF units
    avg_buy_price  = Column(Numeric(12, 2), nullable=False)  # weighted average cost
    total_invested = Column(Numeric(14, 2), nullable=False)  # quantity × avg_buy_price
    notes          = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SipEntry(Base):
    """
    SIP instalment log. Each row = one monthly SIP payment for one fund.
    Separate from holdings so we can track SIP history even if the user
    later sells the holding.
    """
    __tablename__ = "sip_entries"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = Column(String(100), nullable=False, index=True)
    symbol     = Column(String(20),  nullable=False)
    amount     = Column(Numeric(12, 2), nullable=False)
    sip_date   = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class HoldingCreate(BaseModel):
    user_id:       str
    symbol:        str
    name:          str
    asset_type:    AssetType
    quantity:      Decimal
    avg_buy_price: Decimal
    notes:         Optional[str] = None


class HoldingResponse(BaseModel):
    id:             str
    user_id:        str
    symbol:         str
    name:           str
    asset_type:     AssetType
    quantity:       Decimal
    avg_buy_price:  Decimal
    total_invested: Decimal
    notes:          Optional[str]
    created_at:     datetime

    class Config:
        from_attributes = True
