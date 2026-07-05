# FinTrack

A microservices-based AI-powered personal finance assistant built with FastAPI, PostgreSQL, and the Model Context Protocol (MCP).

## Architecture
- `ai-gateway` — orchestrator that talks to Claude
- `mcp-servers/expense-server` — MCP server for logging expenses/income
- `mcp-servers/budget-server` — MCP server for budgets & alerts

## Run locally
1. Copy `FinTrack/.env.example` to `FinTrack/.env` and fill in values.
2. `cd FinTrack && docker compose up --build`

See `FinTrack/LEARN.md` for a beginner-friendly walkthrough of the concepts.
