"""Dot journal and git transaction boundary."""

from claude_hub.services.dot.storage.cards import (
    _DAY_RE,
    _JOURNAL_HEADER_RE,
    Optional,
    Path,
    datetime,
    logger,
    os,
    re,
    subprocess,
)
from claude_hub.services.dot.storage.memory import DotMemoryStore


class DotStore(DotMemoryStore):

    # ── journal ─────────────────────────────────────────────────────────

    def journal_path(self, day: str) -> Path:
        return self.journal_dir / f"{day}.md"

    def append_journal(self, who: str, text: str, at: Optional[datetime] = None) -> None:
        self.ensure()
        moment = (at or self._utc_now()).astimezone()
        path = self.journal_path(moment.strftime("%Y-%m-%d"))
        # A line in the text that looks like an entry header would read as a turn of its own.
        # Reading turns "\r\n" and "\r" into "\n": escape what they will read as.
        body = re.sub(r"\r\n?", "\n", text.strip())
        body = _JOURNAL_HEADER_RE.sub(lambda match: " " + match.group(0), body)
        entry = f"## {moment.strftime('%H:%M')} · {who}\n\n{body}\n\n"
        # The folder may have been removed by hand since ``ensure`` ran.
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as stream:
            stream.write(entry)

    def journal_days(self) -> list[str]:
        self.ensure()
        # Day files only: a README or notes file dropped in by hand is not a day.
        return sorted(
            path.stem for path in self.journal_dir.glob("*.md") if _DAY_RE.fullmatch(path.stem)
        )

    def recent_journal(self, max_entries: int = 8, max_chars: int = 400) -> list[str]:
        """Return the newest journal entries (oldest first), each shortened."""
        entries: list[str] = []
        for day in reversed(self.journal_days()[-3:]):
            try:
                # A cut-off write must not hide the rest of the recent talk.
                text = self.journal_path(day).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parts = _JOURNAL_HEADER_RE.split(text)
            # split() yields [preamble, time, who, body, time, who, body, ...]
            chunks = [(parts[i], parts[i + 1], parts[i + 2]) for i in range(1, len(parts) - 2, 3)]
            for clock, who, body in reversed(chunks):
                head = f"{clock} {who}"
                body = " ".join(body.split())
                if len(body) > max_chars:
                    body = body[: max_chars - 1] + "…"
                entries.append(f"[{day} {head}] {body}")
                if len(entries) >= max_entries:
                    return list(reversed(entries))
        return list(reversed(entries))

    # ── git ─────────────────────────────────────────────────────────────

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.home),
                "-c",
                "user.name=Dot",
                "-c",
                "user.email=dot@claude-hub.local",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            # Never fall through to a repo above the home (a broken .git in a home
            # inside another checkout would commit Dot's files there).
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(self.home.resolve().parent)},
            check=False,
        )

    def commit(self, message: str) -> bool:
        """Commit every change in the home. Returns True when a commit was made."""
        if not (self.home / ".git").exists():
            return False
        with self._git_lock:
            try:
                self._git("add", "-A")
                if self._git("diff", "--cached", "--quiet").returncode == 0:
                    return False
                result = self._git("commit", "-q", "-m", message)
            except (OSError, subprocess.SubprocessError):
                logger.warning("dot memory commit failed", exc_info=True)
                return False
            if result.returncode != 0:
                logger.warning("dot memory commit failed: %s", result.stderr.strip())
                return False
            return True
