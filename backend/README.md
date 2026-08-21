# Claude Hub Backend

FastAPI backend for Claude Hub's persistent terminal and Agent Workspace flows.

## Agent Tree

Agents that need to spawn children, wait on directed events, ACK a cursor, or
retry with a stable `call_id` should read
[docs/AGENT_TREE.md](../docs/AGENT_TREE.md). Public spawn is `managed_task`
only (Claude / Codex / Cursor). `native_subagent` and `external_job` return
HTTP 422. There is no Agent Tree UI yet.

REST lives under `/api/agent-tree` (see also http://localhost:8173/docs).

## Development

### Installation

```bash
cd backend
uv sync --dev
```

### Running

```bash
uv run uvicorn claude_hub.main:app --reload --host 0.0.0.0 --port 8173
```

### API Docs

- Swagger UI: http://localhost:8173/docs
- ReDoc: http://localhost:8173/redoc
