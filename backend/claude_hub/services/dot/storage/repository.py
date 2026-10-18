"""Dot filesystem, profile, card, and attachment repository."""

import subprocess
from abc import ABC, abstractmethod
from datetime import datetime

from claude_hub.services.dot.storage.cards import (
    _CONTROL_RE,
    _GITIGNORE,
    AVATAR_IMAGE_BYTES,
    AVATAR_TEXT_LIMIT,
    CARD_KINDS,
    DEFAULT_NAME,
    DEFAULT_ROLE,
    MAX_BODY_CHARS,
    MAX_FILE_BYTES,
    NAME_LIMIT,
    Any,
    Card,
    Iterable,
    Optional,
    Path,
    _parse_json,
    _slug,
    hashlib,
    json,
    logger,
    mimetypes,
    os,
    parse_card,
    re,
    resolve_dot_home,
    shutil,
    tempfile,
    threading,
    utc_now,
    uuid,
)


class DotRepository(ABC):
    @abstractmethod
    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run one git command under the store's shared git lock."""
        ...

    @abstractmethod
    def commit(self, message: str) -> bool:
        """Commit the memory home and report whether it changed."""
        ...

    def _utc_now(self) -> datetime:
        return utc_now()

    def _now_iso(self) -> str:
        return self._utc_now().isoformat(timespec="seconds")

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = (home or resolve_dot_home()).expanduser()
        self._lock = threading.RLock()
        # Commits use their own lock: git can take seconds and must not block
        # JSON reads and writes (some run on the event loop). Writes are
        # atomic replaces, so ``git add`` never sees half a file.
        self._git_lock = threading.Lock()
        self._ready = False

    @property
    def lock(self) -> threading.RLock:
        """The store lock, for callers that read, decide and write state."""
        return self._lock

    # ── layout ──────────────────────────────────────────────────────────

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def journal_dir(self) -> Path:
        return self.home / "journal"

    @property
    def files_dir(self) -> Path:
        return self.home / "files"

    @property
    def role_path(self) -> Path:
        return self.home / "DOT.md"

    @property
    def index_path(self) -> Path:
        return self.home / "index.sqlite"

    def ensure(self) -> None:
        """Create the home layout and git repo once per process."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            for directory in (self.home, self.journal_dir, self.files_dir):
                directory.mkdir(parents=True, exist_ok=True)
            for kind in CARD_KINDS:
                (self.memory_dir / kind).mkdir(parents=True, exist_ok=True)
            if not self.role_path.exists():
                self.role_path.write_text(DEFAULT_ROLE, encoding="utf-8")
            gitignore = self.home / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(_GITIGNORE, encoding="utf-8")
            for name, empty in (
                ("focus.json", []),
                ("work.json", []),
                ("inbox.json", []),
                ("lessons.json", []),
                ("state.json", {}),
            ):
                if not (self.home / name).exists():
                    self._write_json(name, empty)
            if not (self.home / ".git").exists() and shutil.which("git"):
                self._git("init", "-q")
                self.commit("dot: init memory home")
            self._ready = True

    # ── json files ──────────────────────────────────────────────────────

    def _read_json(self, name: str, default: Any, *, update: bool = False) -> Any:
        """Read a JSON file; lists keep only their objects.

        A missing file is ``default``. A file that is not the expected JSON (a bad
        hand edit) is moved aside first, so the next write cannot erase it. A file
        that cannot be read is ``default`` for a reader, but an error for an
        ``update``: writing the default back would erase it.
        """
        path = self.home / name
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return default
        except OSError:
            if update:
                raise
            logger.warning("dot: cannot read %s", path.name, exc_info=True)
            return default
        value = _parse_json(raw)
        if not isinstance(value, type(default)):
            self._set_aside(path, raw)
            return default
        if isinstance(value, list):
            value = [item for item in value if isinstance(item, dict)]
        return value

    def _set_aside(self, path: Path, raw: bytes) -> None:
        with self._lock:
            try:
                # Only the bad copy that was read: a writer may have replaced it since.
                if path.read_bytes() != raw:
                    return
                stamp = self._utc_now().strftime("%Y%m%dT%H%M%S%f")
                path.rename(path.with_name(f"{path.name}.corrupt-{stamp}"))
            except OSError:
                return
        logger.warning("dot: %s was not valid JSON; kept it as %s.corrupt-*", path.name, path.name)

    def _write_json(self, name: str, value: Any) -> None:
        self._atomic_write(self.home / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def read_state(self) -> dict[str, Any]:
        self.ensure()
        return dict(self._read_json("state.json", {}))

    def update_state(self, **changes: Any) -> dict[str, Any]:
        self.ensure()
        with self._lock:
            state = self._read_json("state.json", {}, update=True)
            for key, value in changes.items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
            self._write_json("state.json", state)
            return dict(state)

    def read_role(self) -> str:
        self.ensure()
        try:
            return self.role_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            # Unreadable or not UTF-8 (saved by an editor in another encoding).
            return DEFAULT_ROLE

    # ── identity ────────────────────────────────────────────────────────

    def read_identity(self) -> dict[str, Any]:
        """Dot's name and avatar. Never creates the home (read by every status call)."""
        raw = self._read_json("identity.json", {})
        name = " ".join(str(raw.get("name") or "").split())[:NAME_LIMIT]
        avatar = str(raw.get("avatar") or "").strip()[:AVATAR_TEXT_LIMIT]
        image = raw.get("avatar_image")
        if not (isinstance(image, str) and self.resolve_file(image) is not None):
            image = None
        return {"name": name or DEFAULT_NAME, "avatar": avatar, "avatar_image": image}

    def update_identity(
        self,
        *,
        name: Optional[str] = None,
        avatar: Optional[str] = None,
        avatar_image: Optional[str] = None,
    ) -> dict[str, Any]:
        """Change the given fields; ``None`` keeps a field, ``""`` clears avatar/image."""
        self.ensure()
        with self._lock:
            identity = self.read_identity()
            if name is not None:
                clean = " ".join(name.split())
                if not clean or len(clean) > NAME_LIMIT or _CONTROL_RE.search(clean):
                    raise ValueError(f"name must be 1-{NAME_LIMIT} characters")
                identity["name"] = clean
            if avatar is not None:
                clean = avatar.strip()
                if len(clean) > AVATAR_TEXT_LIMIT or _CONTROL_RE.search(clean):
                    raise ValueError("avatar must be a short emoji or text")
                identity["avatar"] = clean
            if avatar_image is not None:
                if avatar_image and self.resolve_file(avatar_image) is None:
                    raise ValueError(f"file not found in Dot home: {avatar_image}")
                identity["avatar_image"] = avatar_image or None
            self._write_json("identity.json", identity)
            return identity

    def store_avatar(self, data: bytes) -> str:
        """Store an avatar image (PNG, JPEG, GIF or WebP; no SVG); return its path."""
        if len(data) > AVATAR_IMAGE_BYTES:
            raise ValueError(f"avatar image must be at most {AVATAR_IMAGE_BYTES // 1024} KB")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            suffix = ".gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ValueError("avatar must be a PNG, JPEG, GIF or WebP image")
        return self.store_file(data, f"avatar{suffix}")

    # ── cards ───────────────────────────────────────────────────────────

    def iter_card_paths(self) -> Iterable[Path]:
        self.ensure()
        for kind in CARD_KINDS:
            yield from sorted((self.memory_dir / kind).glob("*.md"))

    def list_cards(
        self, kind: Optional[str] = None, *, include_archived: bool = False
    ) -> list[Card]:
        cards: list[Card] = []
        for path in self.iter_card_paths():
            if kind and path.parent.name != kind:
                continue
            card = self.read_card_path(path)
            if card is None or (card.archived and not include_archived):
                continue
            cards.append(card)
        return cards

    @staticmethod
    def read_card_path(path: Path) -> Optional[Card]:
        try:
            return parse_card(path.read_text(encoding="utf-8"), path)
        except (OSError, ValueError):
            # Unreadable or not UTF-8: skipped like a file without a header.
            return None

    def find_card_path(self, card_id: str) -> Optional[Path]:
        card_id = card_id.strip()
        if not card_id or "/" in card_id or card_id.startswith("."):
            return None
        for kind in CARD_KINDS:
            candidate = self.memory_dir / kind / f"{card_id}.md"
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                # A name too long for the filesystem is no card.
                return None
        for path in self.iter_card_paths():
            card = self.read_card_path(path)
            if card is not None and card.id == card_id:
                return path
        return None

    def get_card(self, card_id: str) -> Optional[Card]:
        path = self.find_card_path(card_id)
        return self.read_card_path(path) if path is not None else None

    def new_card_id(self, kind: str, title: str) -> str:
        return f"{kind[0]}-{_slug(title)}-{uuid.uuid4().hex[:6]}"

    def save_card(self, card: Card) -> Card:
        """Create or replace a card. Moves the file if its kind changed."""
        self.ensure()
        if card.kind not in CARD_KINDS:
            raise ValueError(f"kind must be one of {', '.join(CARD_KINDS)}")
        card.title = card.title.strip()
        if not card.title:
            raise ValueError("title is required")
        if len(card.body) > MAX_BODY_CHARS:
            raise ValueError(f"body is longer than {MAX_BODY_CHARS} characters")
        if card.id and (
            card.id != card.id.strip()
            or card.id.startswith(".")
            or any(char in card.id for char in "/\\")
            or _CONTROL_RE.search(card.id)
        ):
            raise ValueError(f"bad card id: {card.id!r}")
        with self._lock:
            stamp = self._now_iso()
            if not card.id:
                card.id = self.new_card_id(card.kind, card.title)
            old_path = self.find_card_path(card.id)
            card.created = card.created or stamp
            card.updated = stamp
            path = self.memory_dir / card.kind / f"{card.id}.md"
            self._atomic_write(path, card.to_text())
            if old_path is not None and old_path != path:
                old_path.unlink(missing_ok=True)
            card.path = path
            return card

    def archive_card(self, card_id: str) -> Optional[Card]:
        card = self.get_card(card_id)
        if card is None:
            return None
        card.archived = True
        return self.save_card(card)

    def delete_card(self, card_id: str) -> bool:
        path = self.find_card_path(card_id)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        return True

    # ── files ───────────────────────────────────────────────────────────

    def store_file(self, data: bytes, filename: str) -> str:
        """Store bytes under ``files/`` by content hash; return the home-relative path."""
        self.ensure()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file is too large")
        suffix = Path(filename).suffix.lower()[:12]
        if not re.fullmatch(r"(\.[a-z0-9]+)?", suffix):
            suffix = ""
        digest = hashlib.sha256(data).hexdigest()[:16]
        path = self.files_dir / f"{digest}{suffix}"
        if not path.exists():
            with self._lock:
                self.files_dir.mkdir(parents=True, exist_ok=True)
                fd, raw = tempfile.mkstemp(prefix=".file.", suffix=".tmp", dir=self.files_dir)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(raw, path)
        return str(path.relative_to(self.home))

    def resolve_home_path(self, relative: str) -> Optional[Path]:
        """Resolve a home-relative path, refusing escapes."""
        return self._resolve_inside(self.home, relative)

    def resolve_file(self, relative: str) -> Optional[Path]:
        """Resolve a ``files/...`` path, refusing anything outside ``files/``."""
        return self._resolve_inside(self.files_dir, relative)

    def _resolve_inside(self, root: Path, relative: str) -> Optional[Path]:
        try:
            candidate = (self.home / relative).resolve()
            candidate.relative_to(root.resolve())
            return candidate if candidate.is_file() else None
        except (OSError, ValueError):
            # Escapes, NUL bytes and over-long names are all "no such file".
            return None

    @staticmethod
    def guess_mime(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
