import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from models import Base

# asyncpg is the async PostgreSQL driver. SQLAlchemy's default driver (psycopg2)
# is synchronous — it would block the event loop. asyncpg never blocks.
# We swap the scheme prefix at runtime so the .env can use the standard "postgresql://" URL.
DATABASE_URL = os.getenv("DATABASE_URL", "").replace(
    "postgresql://", "postgresql+asyncpg://"
)

# echo=False keeps logs clean. Set echo=True temporarily if you need to debug SQL queries.
engine = create_async_engine(DATABASE_URL, echo=False)

# async_sessionmaker creates a factory that produces AsyncSession objects on demand.
# expire_on_commit=False means objects stay readable after a commit (important for returning
# data from tools without triggering a lazy-load on a closed session).
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


async def init_db():
    """Called on server startup — creates the 'budgets' table if it doesn't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """
    FastAPI dependency — yields a DB session for each request and closes it after.
    Usage in a route: db: AsyncSession = Depends(get_db)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()