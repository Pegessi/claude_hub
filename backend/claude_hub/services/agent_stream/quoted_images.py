"""Agent-quoted image resolver/proxy layer.

Agents quote images in chat markdown using provider-specific **bare tokens** —
no scheme, directory, or extension — for example a Lark/Feishu ``img_v3_…``
key::

    ![Image](img_v3_0215v_f958a4be-ef9b-4a06-b887-0a048973208g)

The browser cannot resolve such a token directly. This module is the single
proxy/resolve layer between the markdown ``<img src>`` and the real bytes. It
is deliberately provider-pluggable:

* each image *source* (Lark, a future Slack/DingTalk/CDN token, a skill-local
  temp image, …) is a :class:`QuotedImageResolver`, registered once;
* the first resolver whose key pattern matches owns the request;
* adding a source is adding a resolver class + registration — the HTTP route
  and the frontend contract never change.

Every resolver sits on the same security base (see
:class:`LocalFileResolver`):

  1. **key allowlist** — a per-resolver anchored regex (no separators,
     traversal, or glob metacharacters), length bound, no control chars;
  2. **root confinement** — the resolved real path (symlinks fully expanded)
     must stay inside the resolver's allowed roots;
  3. **magic allowlist** — bytes must sniff as PNG/JPEG/GIF/WebP;
  4. **size bound** — hard read cap;
  5. **uniform denial** — any failure raises :class:`QuotedImageUnavailable`,
     which the route renders as an opaque 404.

The resolved result is intentionally open for non-file providers: a resolver
may return local bytes (:class:`LocalImage`) — the Lark case today — or, in
future, a remote URL to redirect/proxy (:class:`RedirectImage`) or inline
data (:class:`DataImage`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Pattern, Sequence, Tuple, Union

from .attachments import _magic_mime as sniff_image_mime

__all__ = [
    "DataImage",
    "LocalImage",
    "LarkImgV3Resolver",
    "QuotedImageResolver",
    "QuotedImageUnavailable",
    "QuotedImageResult",
    "RedirectImage",
    "LocalFileResolver",
    "register_resolver",
    "registered_resolvers",
    "resolve_quoted_image",
]

# Shared bounds (same order as the agent-image restricted reader).
QUOTED_IMAGE_KEY_MAX_LEN = 256
QUOTED_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

# Whitelisted image extensions a local-file resolver may attach to a key.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


class QuotedImageUnavailable(LookupError):
    """Raised by any layer when the image cannot/should not be served.

    The HTTP route turns this into a single opaque 404 so resolvers never leak
    whether the cause was a bad key, a missing file, an escape, or bad magic.
    """


# ── Resolver results ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalImage:
    """In-process image bytes with a sniffed MIME type."""

    data: bytes
    media_type: str


@dataclass(frozen=True)
class DataImage:
    """Inline data the resolver already holds (e.g. a decoded data URI).

    Kept as a distinct shape so a future resolver can return inline content
    without touching the route; unused by any built-in resolver today.
    """

    data: bytes
    media_type: str


@dataclass(frozen=True)
class RedirectImage:
    """An http(s) URL the route may redirect the browser to (or proxy later).

    No built-in resolver returns this yet; the shape exists so a future CDN
    provider can be added without changing the route signature.
    """

    location: str


#: What a resolver returns. The HTTP route switches on the concrete type.
QuotedImageResult = Union[LocalImage, DataImage, RedirectImage]


# ── Security base + registry ────────────────────────────────────────────────


class QuotedImageResolver:
    """Base class/protocol for a bare-token image provider.

    Subclasses either implement :meth:`_resolve` directly (free-form providers
    that return :class:`LocalImage`/:class:`RedirectImage`/:class:`DataImage`)
    or extend :class:`LocalFileResolver` for the usual "key → file under
    allowed roots" case.
    """

    #: Provider name, unique within the registry (used in logs/tests).
    name: str = "quoted-image"
    #: Anchored allowlist for the bare token. Must not permit path separators,
    #: glob metacharacters, or whitespace.
    key_pattern: Pattern[str] = re.compile(r"^$")
    #: Per-provider token length bound.
    max_key_length: int = QUOTED_IMAGE_KEY_MAX_LEN

    def matches(self, key: object) -> bool:
        """True when this resolver owns *key* (validation included)."""
        if not isinstance(key, str) or not key:
            return False
        if len(key) > self.max_key_length:
            return False
        # Reject control characters / NULs before any filesystem work.
        if any(ord(ch) < 0x20 for ch in key):
            return False
        return self.key_pattern.fullmatch(key) is not None

    def resolve(self, key: str) -> QuotedImageResult:
        """Validate then resolve. Do not override; implement ``_resolve``."""
        if not self.matches(key):
            raise QuotedImageUnavailable("bad key")
        return self._resolve(key)

    def _resolve(self, key: str) -> QuotedImageResult:
        raise NotImplementedError


class LocalFileResolver(QuotedImageResolver):
    """Security base for resolvers that map a key onto a local file.

    Subclasses define:

    * :meth:`roots` — directories the provider's files may live under;
    * :meth:`candidate_files` — files to try for a validated key (typically a
      fixed-shape glob under the roots);
    * :meth:`accepts_resolved` — optional extra shape check on the fully
      resolved file (default: must be under one of the roots).

    The base handles the read cap and magic-byte image allowlist.
    """

    def roots(self) -> Sequence[Path]:
        raise NotImplementedError

    def candidate_files(self, key: str) -> Iterable[Path]:
        raise NotImplementedError

    def accepts_resolved(self, real: Path, root: Path) -> bool:
        """Containment hook after full symlink resolution.

        Default: the file simply has to stay under *root*. Providers can
        override to enforce a fixed relative shape as well.
        """
        try:
            real.relative_to(root)
        except ValueError:
            return False
        return True

    def _resolve(self, key: str) -> LocalImage:
        resolved_roots: List[Path] = [
            root for root in (self._strict_root(r) for r in self.roots()) if root is not None
        ]
        if not resolved_roots:
            raise QuotedImageUnavailable("no roots")

        root_set = set(resolved_roots)
        resolved: Optional[Path] = None
        for candidate in self.candidate_files(key):
            try:
                real = candidate.resolve(strict=True)
            except OSError:
                continue
            if not real.is_file():
                continue
            for root in root_set:
                if self.accepts_resolved(real, root):
                    resolved = real
                    break
            if resolved is not None:
                break

        if resolved is None:
            raise QuotedImageUnavailable("no file")

        try:
            with open(resolved, "rb") as handle:
                data = handle.read(QUOTED_IMAGE_MAX_BYTES + 1)
        except OSError:
            raise QuotedImageUnavailable("read failed") from None

        if len(data) > QUOTED_IMAGE_MAX_BYTES:
            raise QuotedImageUnavailable("too large")

        mime = sniff_image_mime(data)
        if mime is None:
            raise QuotedImageUnavailable("bad magic")
        return LocalImage(data=data, media_type=mime)

    @staticmethod
    def _strict_root(root: Path) -> Optional[Path]:
        try:
            return root.expanduser().resolve(strict=True)
        except OSError:
            return None


# ── Registry ────────────────────────────────────────────────────────────────

_REGISTRY: List[QuotedImageResolver] = []


def register_resolver(resolver: QuotedImageResolver) -> None:
    """Register a provider at the end of the dispatch chain.

    First match wins, so order providers from most specific to most general.
    """
    _REGISTRY.append(resolver)


def registered_resolvers() -> Tuple[QuotedImageResolver, ...]:
    """Snapshot of the dispatch chain (tests/introspection)."""
    return tuple(_REGISTRY)


def resolve_quoted_image(key: str) -> QuotedImageResult:
    """Dispatch a bare token to the first matching resolver.

    Raises :class:`QuotedImageUnavailable` when no provider owns the key or
    the provider cannot serve it.
    """
    for resolver in _REGISTRY:
        if resolver.matches(key):
            return resolver.resolve(key)
    raise QuotedImageUnavailable("no provider")


# ── Provider #1: Lark/Feishu img_v3 ─────────────────────────────────────────


_LARK_RESOURCES_DIR = "lark-im-resources"


def default_lark_resource_root() -> Optional[Path]:
    """Directory containing ``<sender>/lark-im-resources/`` image trees.

    A function (not an import-time constant) so tests can point the default
    resolver at a tmp tree without registering a replacement.
    """
    try:
        return Path.home() / ".claude" / "oncall" / ".tmp_img"
    except RuntimeError:
        return None


class LarkImgV3Resolver(LocalFileResolver):
    """Lark ``img_v3_<key>`` images lark-cli downloaded to the oncall cache.

    Layout (fixed by lark-cli)::

        <root>/<sender>/lark-im-resources/img_v3_<key>.<ext>

    The glob anchor is fixed two levels below the root and the resolved file
    must retain that exact shape, so neither the sender segment nor the
    resources directory can be chosen by the caller.
    """

    name = "lark-img-v3"
    key_pattern = re.compile(r"img_v3_[A-Za-z0-9_-]+")

    def __init__(self, root_provider: Optional[Callable[[], Optional[Path]]] = None) -> None:
        # Default to the module-level function looked up at call time so tests
        # can monkeypatch ``default_lark_resource_root`` directly.
        self._root_provider = root_provider

    def _current_root(self) -> Optional[Path]:
        provider = self._root_provider or default_lark_resource_root
        return provider()

    def roots(self) -> Sequence[Path]:
        root = self._current_root()
        return [] if root is None else [root]

    def candidate_files(self, key: str) -> Iterable[Path]:
        root = self._current_root()
        if root is None:
            return ()
        # Glob each whitelisted suffix over the fixed two-level anchor.
        # ``key`` is charset-validated, so it carries no glob metacharacters or
        # separators; the only free segment is ``*`` (the sender directory).
        paths: List[Path] = []
        for suffix in _IMAGE_SUFFIXES:
            try:
                paths.extend(root.glob(f"*/{_LARK_RESOURCES_DIR}/{key}{suffix}"))
            except OSError:
                continue
        return paths

    def accepts_resolved(self, real: Path, root: Path) -> bool:
        try:
            relative = real.relative_to(root)
        except ValueError:
            return False
        # Exactly <sender>/lark-im-resources/<file> — no nested/loose files.
        return len(relative.parts) == 3 and relative.parts[1] == _LARK_RESOURCES_DIR


register_resolver(LarkImgV3Resolver())
