"""
Conversational memory for the ai-gateway.

Design decisions (see Session 2 plan):
  - Postgres-backed (reuses the existing DB cluster shared with MCP servers)
  - Gateway-owned (memory is a gateway concern, NOT an MCP tool)
  - Full OpenAI message object stored (role + content + tool_calls + tool_call_id)
  - Sliding window of last N messages (default 20)
  - One rolling conversation per user (threads can be added later)

Public API:
  - init_memory_db()              — create table on startup
  - load_history(user_id, limit)  — list of OpenAI-compatible messages
  - save_message(user_id, msg)    — persist a single message
  - clear_history(user_id)        — delete all history for user
  - get_full_history(user_id)     — paginated full dump (for UI/debug)
"""

import os
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Text, DateTime, Index, select, delete
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


# ── DB plumbing ────────────────────────────────────────────────────────────────
# Reuse the same Postgres DATABASE_URL as the MCP servers — one cluster, many tables.
# Each service owns its own tables; here the gateway owns `conversations`.

DATABASE_URL = os.getenv("DATABASE_URL", "").replace(
    "postgresql://", "postgresql+asyncpg://"
)

_engine = create_async_engine(DATABASE_URL, echo=False)
_SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── Model ──────────────────────────────────────────────────────────────────────
# One row = one message in a conversation.
# role       : 'user' | 'assistant' | 'tool'
# content    : the text (nullable because assistant tool-call messages have null content)
# tool_calls : JSON array of tool calls the assistant requested (nullable)
# tool_call_id: for role='tool' messages, references the call this response belongs to

class ConversationMessage(Base):
    __tablename__ = "conversations"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id      = Column(String(100), nullable=False, index=True)
    role         = Column(String(20),  nullable=False)
    content      = Column(Text,        nullable=True)
    tool_calls   = Column(JSONB,       nullable=True)   # list[dict] or null
    tool_call_id = Column(String(100), nullable=True)   # only set when role='tool'
    created_at   = Column(DateTime,    nullable=False, default=datetime.utcnow)

    __table_args__ = (
        # Composite index for the hot query: "last N messages for user X"
        Index("idx_conv_user_created", "user_id", "created_at"),
    )


async def init_memory_db():
    """Create the conversations table if it doesn't exist. Called from gateway lifespan."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Load / save / clear ────────────────────────────────────────────────────────

# How many extra messages to fetch when looking for `limit` COMPLETE turns.
# A single user turn can expand into 1 user + N (assistant+tool) pairs + 1 final
# assistant text. So we fetch 3× the target and then trim down to a sane slice.
_FETCH_OVERSHOOT = 3


async def load_history(user_id: str, limit: int = 20) -> list[dict]:
    """
    Return the last `limit` messages for this user as OpenAI-compatible dicts.
    Ordered oldest→newest so they can be prepended directly to a new chat.

    Smart boundary handling: naïvely slicing the last N rows can split a tool-
    chain apart — leaving orphan tool responses or assistant-tool_call messages
    that have no partner. Models like Groq's gpt-oss-* reject these with
    cryptic errors ("Tools should have a name!").

    Fix: fetch more than we need, then walk backwards keeping only well-formed
    segments. We ONLY start the returned history with a 'user' message (never
    a mid-flight tool_call) to guarantee the LLM sees a valid conversation.
    """
    async with _SessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user_id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit * _FETCH_OVERSHOOT)
        )
        rows = list(result.scalars().all())

    if not rows:
        return []

    # Convert to chronological order (oldest first)
    rows.reverse()

    # Walk forward and segment the messages into turns.
    # A "turn" starts at a user message and ends either at the next user
    # message (exclusive) or at EOF. Within a turn we keep everything —
    # assistant tool_calls, tool results, final assistant text.
    turns: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        msg = _row_to_message(row)
        if msg["role"] == "user":
            if current:
                turns.append(current)
            current = [msg]
        else:
            # Skip orphan non-user messages before the first user turn starts
            if current:
                current.append(msg)
    if current:
        turns.append(current)

    # Drop any turn that's malformed:
    #   - missing a final assistant text reply (crashed mid-stream)
    #   - has an assistant tool_call without a matching tool response
    valid_turns = [t for t in turns if _is_valid_turn(t)]

    # Keep the most-recent turns that fit within `limit` messages total
    selected: list[dict] = []
    for turn in reversed(valid_turns):
        if len(selected) + len(turn) > limit:
            break
        selected = turn + selected

    return selected


def _row_to_message(row: ConversationMessage) -> dict:
    """Convert a DB row to the OpenAI message dict shape."""
    msg: dict = {"role": row.role}
    if row.content is not None:
        msg["content"] = row.content
    if row.tool_calls is not None:
        msg["tool_calls"] = row.tool_calls
    if row.tool_call_id is not None:
        msg["tool_call_id"] = row.tool_call_id
    return msg


def _is_valid_turn(turn: list[dict]) -> bool:
    """
    A valid turn:
      - starts with role='user'
      - every assistant tool_call message is followed by a tool response for
        each tool_call_id (within the turn)
      - ends with an assistant text message (role='assistant' and content)
    Malformed turns (crashed mid-stream) are dropped so the LLM never sees them.
    """
    if not turn or turn[0].get("role") != "user":
        return False

    # Collect all tool_call_ids the assistant requested across this turn
    requested_ids: set[str] = set()
    responded_ids: set[str] = set()
    for msg in turn:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    return False
                fn = tc.get("function") or {}
                if not fn.get("name"):
                    return False   # "Tools should have a name!" guard
                if tc.get("id"):
                    requested_ids.add(tc["id"])
        elif msg.get("role") == "tool":
            if msg.get("tool_call_id"):
                responded_ids.add(msg["tool_call_id"])

    # Every requested tool call must have a matching response
    if requested_ids != responded_ids:
        return False

    # Turn must end with an assistant text reply (not a tool_call message)
    last = turn[-1]
    if last.get("role") != "assistant" or not last.get("content"):
        return False

    return True



async def save_message(user_id: str, message: dict) -> None:
    """
    Persist a single message. Accepts the same dict shape OpenAI/Groq uses.

    Expected keys:
      role         : required
      content      : optional (string, or null for assistant tool-call messages)
      tool_calls   : optional (list of tool call objects — ChatCompletionMessageToolCall)
      tool_call_id : optional (string, only for role='tool')

    OpenAI SDK returns tool_calls as pydantic objects; we normalize to plain dicts
    so JSONB can serialize them cleanly.
    """
    tool_calls = message.get("tool_calls")
    if tool_calls:
        # Convert pydantic objects (if any) to plain dicts for JSONB storage
        tool_calls = [
            tc if isinstance(tc, dict) else _serialize_tool_call(tc)
            for tc in tool_calls
        ]

    row = ConversationMessage(
        user_id      = user_id,
        role         = message["role"],
        content      = message.get("content"),
        tool_calls   = tool_calls,
        tool_call_id = message.get("tool_call_id"),
    )
    async with _SessionLocal() as session:
        session.add(row)
        await session.commit()


def _serialize_tool_call(tc) -> dict:
    """Convert an OpenAI SDK ToolCall pydantic object to a plain dict."""
    if hasattr(tc, "model_dump"):
        return tc.model_dump()
    if hasattr(tc, "dict"):
        return tc.dict()
    # Last resort: build it manually
    return {
        "id":   getattr(tc, "id", None),
        "type": getattr(tc, "type", "function"),
        "function": {
            "name":      tc.function.name,
            "arguments": tc.function.arguments,
        },
    }


async def clear_history(user_id: str) -> int:
    """Delete all messages for a user. Returns number of rows deleted."""
    async with _SessionLocal() as session:
        result = await session.execute(
            delete(ConversationMessage).where(ConversationMessage.user_id == user_id)
        )
        await session.commit()
        return result.rowcount or 0


async def get_full_history(user_id: str, limit: int = 500) -> list[dict]:
    """
    For UI / debug — returns messages with timestamps and IDs so a client can
    render a chat log. Different shape from load_history (which is LLM-shaped).
    """
    async with _SessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user_id)
            .order_by(ConversationMessage.created_at.asc())
            .limit(limit)
        )
        rows = result.scalars().all()

    return [
        {
            "id":           str(row.id),
            "role":         row.role,
            "content":      row.content,
            "tool_calls":   row.tool_calls,
            "tool_call_id": row.tool_call_id,
            "created_at":   row.created_at.isoformat(),
        }
        for row in rows
    ]

