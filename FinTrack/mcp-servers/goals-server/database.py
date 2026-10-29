import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from models import Base

# Replace the standard PostgreSQL scheme with asyncpg's scheme.
# asyncpg is a non-blocking driver — it never holds up the event loop while
# waiting for the database. psycopg2 (the default) would block.
DATABASE_URL = os.getenv("DATABASE_URL", "").replace(
    "postgresql://", "postgresql+asyncpg://"
)

engine = create_async_engine(DATABASE_URL, echo=False)

# expire_on_commit=False: after a commit, SQLAlchemy normally marks objects as
# "expired" so the next access re-fetches from DB. We disable this so we can
# read the object's attributes immediately after a commit without an extra query.
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


async def init_db():
    """Creates goals and goal_contributions tables on startup if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency — yields one DB session per request, closes it after."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
