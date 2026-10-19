"""Allowlisted source-text attachment previews using the Markdown file boundary."""

from pathlib import Path
from typing import Literal

from claude_hub.models import ManagedSession, StreamAttachmentPreview
from claude_hub.services.agent_stream.local_markdown import read_text_preview

_AttachmentKind = Literal["markdown", "html", "code", "data", "text"]
_AttachmentFormat = tuple[_AttachmentKind, str]

# These are display hints only. Every allowed file is UTF-8-validated and returned
# in JSON; HTML, scripts and notebook cells are never served as active content.
_SUFFIX_FORMATS: dict[str, _AttachmentFormat] = {
    ".md": ("markdown", "markdown"),
    ".markdown": ("markdown", "markdown"),
    ".mdown": ("markdown", "markdown"),
    ".mkd": ("markdown", "markdown"),
    ".py": ("code", "python"),
    ".pyi": ("code", "python"),
    ".js": ("code", "javascript"),
    ".jsx": ("code", "javascript"),
    ".mjs": ("code", "javascript"),
    ".cjs": ("code", "javascript"),
    ".ts": ("code", "typescript"),
    ".tsx": ("code", "typescript"),
    ".c": ("code", "c"),
    ".h": ("code", "c"),
    ".cc": ("code", "cpp"),
    ".cpp": ("code", "cpp"),
    ".cxx": ("code", "cpp"),
    ".hh": ("code", "cpp"),
    ".hpp": ("code", "cpp"),
    ".hxx": ("code", "cpp"),
    ".cu": ("code", "cpp"),
    ".cuh": ("code", "cpp"),
    ".go": ("code", "go"),
    ".rs": ("code", "rust"),
    ".java": ("code", "java"),
    ".kt": ("code", "kotlin"),
    ".kts": ("code", "kotlin"),
    ".swift": ("code", "swift"),
    ".rb": ("code", "ruby"),
    ".php": ("code", "php"),
    ".sh": ("code", "bash"),
    ".bash": ("code", "bash"),
    ".zsh": ("code", "bash"),
    ".fish": ("code", "plaintext"),
    ".sql": ("code", "sql"),
    ".css": ("code", "css"),
    ".scss": ("code", "scss"),
    ".less": ("code", "less"),
    ".html": ("html", "xml"),
    ".htm": ("html", "xml"),
    ".vue": ("code", "xml"),
    ".svelte": ("code", "xml"),
    ".json": ("data", "json"),
    ".jsonc": ("data", "json"),
    ".jsonl": ("data", "json"),
    ".ndjson": ("data", "json"),
    ".ipynb": ("data", "json"),
    ".yaml": ("data", "yaml"),
    ".yml": ("data", "yaml"),
    ".toml": ("data", "ini"),
    ".xml": ("data", "xml"),
    ".csv": ("data", "plaintext"),
    ".tsv": ("data", "plaintext"),
    ".ini": ("data", "ini"),
    ".txt": ("text", "plaintext"),
    ".log": ("text", "plaintext"),
    ".conf": ("text", "plaintext"),
    ".diff": ("text", "diff"),
    ".patch": ("text", "diff"),
    ".rst": ("text", "plaintext"),
}
_NAME_FORMATS: dict[str, _AttachmentFormat] = {
    "dockerfile": ("code", "dockerfile"),
    "makefile": ("code", "makefile"),
    "cmakelists.txt": ("code", "cmake"),
    "readme": ("text", "plaintext"),
    "license": ("text", "plaintext"),
}


def read_attachment_preview(session: ManagedSession, raw_path: str) -> StreamAttachmentPreview:
    """Read an allowlisted local attachment without extending Chat file access."""
    preview = read_text_preview(
        session,
        raw_path,
        allowed_suffixes=_SUFFIX_FORMATS,
        allowed_names=_NAME_FORMATS,
    )
    path = Path(preview.path)
    kind, language = (
        _NAME_FORMATS[path.name.lower()]
        if path.name.lower() in _NAME_FORMATS
        else _SUFFIX_FORMATS[path.suffix.lower()]
    )
    return StreamAttachmentPreview(
        path=preview.path,
        filename=preview.filename,
        content=preview.content,
        size_bytes=preview.size_bytes,
        truncated=preview.truncated,
        kind=kind,
        language=language,
    )
