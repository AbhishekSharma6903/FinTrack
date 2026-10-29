from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager
from openai import OpenAI, APIStatusError
import json
import os
from mcp_client import MCPClient

# ── Provider config from .env ──────────────────────────────────────────────────
# Switch providers by changing these three env vars — no code changes needed.
#
# Groq (recommended, free tier, no credit card):
#   AI_BASE_URL=https://api.groq.com/openai/v1
#   AI_API_KEY=gsk_...
#   AI_MODEL=llama-3.3-70b-versatile
#
# Ollama (local, fully offline, zero cost):
#   AI_BASE_URL=http://host.docker.internal:11434/v1
#   AI_API_KEY=ollama
#   AI_MODEL=llama3.2
#
# Gemini (via OpenAI-compatible endpoint):
#   AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
#   AI_API_KEY=AIza...
#   AI_MODEL=gemini-2.0-flash

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1")
AI_API_KEY  = os.getenv("AI_API_KEY",  "")
AI_MODEL    = os.getenv("AI_MODEL",    "llama-3.3-70b-versatile")

mcp = MCPClient()

# Tool cache — discovered once at startup, reused for every request.
_cached_tools: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cached_tools
    _cached_tools = await mcp.discover_tools()
    print(f"✅ ai-gateway ready — {len(_cached_tools)} tools cached | model: {AI_MODEL}")
    yield


app = FastAPI(title="FinTrack AI Gateway", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    user_id: str
    history: list = []   # previous turns for multi-turn conversations
    # api_key removed — provider key now lives in .env, not per-request


class ChatResponse(BaseModel):
    reply:       str
    tools_used:  list[str]
    raw_results: list


SYSTEM_PROMPT = """You are FinTrack, a smart personal finance assistant for young Indians
who just started earning. You help them log expenses, track income, manage budgets,
build good financial habits, and achieve savings goals.

Be conversational, warm, and practical. Use ₹ for currency. When someone logs an expense,
give them useful context — are they on track? Should they be concerned? Any quick tip?

You have access to tools to log and retrieve financial data. Always use the appropriate
tool when the user mentions money, spending, income, goals, or wants to see their data.

TOOL USAGE RULES — follow these exactly:

1. user_id: NEVER ask the user for their user_id. It is always already known and will be
   automatically injected into every tool call. Just call the tool directly.

2. Budget check: After EVERY call to log_expense, ALWAYS call check_budget_on_expense
   with the same category. Use alert_level to shape reply tone:
     ok       → just confirm the expense, no budget mention needed
     warning  → mention % used, show ₹ remaining and days left
     danger   → stronger tone, show exact daily allowance left
     exceeded → tell them clearly they've gone over, suggest how to recover

3. Goal + spend connection: After log_expense, also call get_habit_status if the user
   has habit goals. If check_budget_on_expense or get_habit_status shows warning/danger/
   exceeded, proactively connect it to their goals — suggest where they can cut spending
   to hit their savings target faster.

4. Goal creation: When user expresses any financial intention ("I want to save X",
   "I want to cut down on Y", "I'm saving for Z"), immediately call create_goal.
   Do not ask clarifying questions first — create the goal with reasonable defaults.

Keep responses concise. No bullet-point overload. Talk like a knowledgeable friend,
not a financial textbook."""


def _mcp_tools_to_openai(tools: list[dict]) -> list[dict]:
    """
    Converts MCP tool definitions to OpenAI function-calling format.

    MCP format (what our servers return):
      { "name": "log_expense", "description": "...", "input_schema": { ...JSON Schema... } }

    OpenAI format (what the SDK expects):
      { "type": "function", "function": { "name": "...", "description": "...", "parameters": { ...JSON Schema... } } }

    The conversion is trivial — just rename input_schema → parameters and wrap it.
    This is dramatically simpler than the old Gemini converter because OpenAI's format
    IS JSON Schema, which is exactly what our MCP servers already return.
    """
    return [
        {
            "type": "function",
            "function": {
                "name":        tool["name"],
                "description": tool.get("description", ""),
                "parameters":  tool.get("input_schema", {"type": "object", "properties": {}}),
            }
        }
        for tool in tools
    ]


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not _cached_tools:
        raise HTTPException(status_code=503, detail="No MCP servers reachable")

    if not AI_API_KEY:
        raise HTTPException(status_code=500, detail="AI_API_KEY not set in environment. Check your .env file.")

    # One client instance per request — stateless, thread-safe.
    # base_url switches the provider: Groq, Ollama, Gemini, or any OpenAI-compatible API.
    client = OpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)
    openai_tools = _mcp_tools_to_openai(_cached_tools)

    tools_used  = []
    raw_results = []

    # Build message list: system prompt first, then history, then current message.
    # OpenAI's message format: [{"role": "system"|"user"|"assistant"|"tool", "content": "..."}]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for msg in req.history:
        if isinstance(msg.get("content"), str):
            role = "assistant" if msg["role"] == "assistant" else "user"
            messages.append({"role": role, "content": msg["content"]})

    messages.append({"role": "user", "content": req.message})

    # ── Agentic tool loop ──────────────────────────────────────────────────────
    # The model may call multiple tools before giving a final text reply.
    # Each iteration: call model → if tool_calls → execute → append results → repeat.
    # Loop ends when finish_reason == "stop" (model is done with tool calls).

    while True:
        try:
            response = client.chat.completions.create(
                model       = AI_MODEL,
                messages    = messages,
                tools       = openai_tools,
                tool_choice = "auto",   # model decides when to use tools vs reply directly
            )
        except APIStatusError as e:
            status = e.status_code or 500
            if status == 429:
                raise HTTPException(status_code=429, detail=f"Rate limit hit on {AI_MODEL}. Wait a moment and retry.")
            elif status in (401, 403):
                raise HTTPException(status_code=status, detail="AI provider rejected the API key. Check AI_API_KEY in your .env.")
            else:
                raise HTTPException(status_code=status, detail=f"AI provider error: {e.message}")

        choice      = response.choices[0]
        finish      = choice.finish_reason
        print(f"📨 finish_reason: {finish} | model: {AI_MODEL}")

        # "stop" means the model produced a final text reply — return it
        if finish == "stop" or not choice.message.tool_calls:
            return ChatResponse(
                reply       = choice.message.content or "I processed your request.",
                tools_used  = tools_used,
                raw_results = raw_results,
            )

        # "tool_calls" means the model wants to call one or more tools
        # Step 1: add the assistant's tool-call message to history
        messages.append(choice.message)   # this is an assistant message with tool_calls attached

        # Step 2: execute each tool and collect results
        for tc in choice.message.tool_calls:
            tool_name = tc.function.name
            tool_args = json.loads(tc.function.arguments)

            # Always inject user_id server-side — never trust what the model sends
            tool_args["user_id"] = req.user_id

            print(f"🔧 Calling tool: {tool_name} with {tool_args}")
            result = await mcp.execute_tool(tool_name, tool_args)

            tools_used.append(tool_name)
            raw_results.append(result)

            # Step 3: append each tool result as a "tool" role message
            # tool_call_id links this result back to the specific tool call above
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      json.dumps(result),
            })

        # Loop back — model will now reason about the tool results and either
        # call more tools or produce a final text reply


@app.get("/health")
async def health():
    global _cached_tools
    # Re-discover if cache is empty (e.g. servers came up after gateway started)
    if not _cached_tools:
        _cached_tools = await mcp.discover_tools()
    return {
        "status":      "ok",
        "tools_count": len(_cached_tools),
        "tools":       [t["name"] for t in _cached_tools],
        "model":       AI_MODEL,
        "provider":    AI_BASE_URL,
    }
