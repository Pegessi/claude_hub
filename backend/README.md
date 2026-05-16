# Claude Hub Backend

FastAPI backend for Claude Hub's persistent terminal and Agent Workspace flows.

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
