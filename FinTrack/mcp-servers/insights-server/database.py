import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# insights-server has no tables of its own — it reads from tables owned by
# expense-server (transactions), budget-server (budgets), and invest-server (holdings).
# We still need a Base and engine for the session factory, but create_all is a no-op.

class Base(DeclarativeBase):
    pass


DATABASE_URL = os.getenv("DATABASE_URL", "").replace(
    "postgresql://", "postgresql+asyncpg://"
)

engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


async def init_db():
    """No-op for insights-server — it owns no tables."""
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
