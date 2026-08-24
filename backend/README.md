# Claude Hub Backend

FastAPI backend for Claude Hub's persistent terminal and Agent Workspace flows.

## Task Graph / TaskMailbox (primary)

Agents orchestrating work via durable Task events should read
[docs/TASK_GRAPH.md](../docs/TASK_GRAPH.md). Use `claude-hub task`
(tree/events/wait/ack/followup/start) and
`/api/workspaces/{id}/tasks/*`. Worker and reviewer agents are Task session assignments
(`session_id`, `review_session_id`, `target_session_id` on start).
The optional Resident agent is an independent long-running agent; if it
participates in Task work it must use explicit Task assignment — it is not a mailbox consumer (not a Task root).

Legacy AgentRun projection remains under `/api/agent-tree` and
`uv run claude-hub agent-tree` as legacy compat projection only (spawn/wait/ack on
deprecated run ids). New work must use Task Graph APIs.

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
