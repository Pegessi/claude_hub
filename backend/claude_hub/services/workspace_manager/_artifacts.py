"""Artifact previews and markdown document discovery."""

import subprocess

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _ArtifactsMixin:
    def preview_artifact(
        self,
        workspace_id: str,
        artifact_ref: str,
        report_id: str | None = None,
    ) -> WorkspaceArtifactPreview:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)
        if workspace.target != ExecutionTarget.LOCAL:
            raise ValueError("Artifact previews are only available for local workspaces")

        artifact_ref = self._clean_markdown_ref(artifact_ref)
        if not artifact_ref:
            raise ValueError("Artifact path is required")
        if not self._markdown_ref_belongs_to_workspace_report(
            workspace_id, artifact_ref, report_id
        ):
            raise KeyError(artifact_ref)
        path = self._resolve_workspace_markdown_path(workspace, artifact_ref, report_id)
        return self._read_markdown_preview(artifact_ref, path)

    def _read_markdown_preview(
        self,
        artifact_ref: str,
        resolved: Path,
    ) -> WorkspaceArtifactPreview:
        try:
            size_bytes = resolved.stat().st_size
            truncated = size_bytes > ARTIFACT_PREVIEW_MAX_BYTES
            content = resolved.read_bytes()[:ARTIFACT_PREVIEW_MAX_BYTES].decode(
                "utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise ValueError("Artifact could not be read") from exc
        return WorkspaceArtifactPreview(
            path=artifact_ref,
            filename=resolved.name,
            content=content,
            size_bytes=size_bytes,
            truncated=truncated,
        )

    def _clean_markdown_ref(self, artifact_ref: str) -> str:
        value = artifact_ref.strip()
        value = value.split("#", 1)[0].split("?", 1)[0]
        match = re.match(r"^(.+\.(?:md|markdown|mdown|mkd)):\d+$", value, re.IGNORECASE)
        if match:
            value = match.group(1)
        return value.strip()

    @staticmethod
    def _path_looks_like_real_file(raw: str) -> bool:
        """Return True only if *raw* plausibly represents a real filesystem path.

        Rejects entries that contain control characters, obviously descriptive
        punctuation (e.g. full sentences embedded in changed_files), total length
        or per-component length exceeding POSIX NAME_MAX / PATH_MAX, or ASCII
        whitespace inside a path component. These guards keep downstream
        ``Path``/``.resolve()`` calls from throwing ``OSError(ENAMETOOLONG)`` on
        macOS when an agent report accidentally puts a prose string into
        ``changed_files``.
        """
        if not raw:
            return False
        value = raw.strip()
        if not value or len(value.encode("utf-8", "ignore")) > _PATH_TOTAL_MAX_BYTES:
            return False
        # Reject obvious control characters / NULs early.
        if any(ord(ch) < 0x20 for ch in value):
            return False
        # Disallow NUL bytes.
        if "\x00" in value:
            return False
        parts = value.split("/")
        for part in parts:
            if not part:
                # Leading, trailing, or doubled slashes are fine (skip empty segment).
                continue
            part_bytes = len(part.encode("utf-8", "ignore"))
            if part_bytes > _PATH_COMPONENT_NAME_MAX_BYTES:
                return False
            # A real path component does not contain parentheses, brackets,
            # colons after the basename, semicolons, or multiple consecutive
            # spaces — these strongly indicate prose rather than a filename.
            if any(ch in part for ch in ("(", ")", "[", "]", ";", "{", "}")):
                return False
            if "  " in part:
                return False
        return True

    @staticmethod
    def _safe_lower_suffix(raw_ref: str) -> str:
        """Return ``Path(raw_ref).suffix.lower()`` without propagating ``OSError``.

        On macOS ``pathlib`` can raise ``OSError(63, 'File name too long')``
        even for what look like pure string operations when a path component
        exceeds ``NAME_MAX``. Callers that iterate report-provided
        ``changed_files`` / ``artifact_refs`` must use this helper instead of
        constructing a ``Path`` directly just to read the suffix.
        """
        if not raw_ref:
            return ""
        try:
            return Path(raw_ref).suffix.lower()
        except (OSError, ValueError):
            pass
        # Fallback: pure string suffix extraction from the last path segment.
        basename = raw_ref.replace("\\", "/").rsplit("/", 1)[-1]
        dot = basename.rfind(".")
        if dot <= 0 or dot == len(basename) - 1:
            return ""
        return basename[dot:].lower()

    def _markdown_ref_belongs_to_workspace_report(
        self,
        workspace_id: str,
        artifact_ref: str,
        report_id: str | None,
    ) -> bool:
        snapshot_ref = str(self.snapshot_path(workspace_id))
        if artifact_ref == snapshot_ref:
            return True
        reports = list(self.reports.values())
        if report_id:
            report = self.reports.get(report_id)
            reports = [report] if report else []
        if any(
            report is not None
            and report.workspace_id == workspace_id
            and (
                artifact_ref in {self._clean_markdown_ref(ref) for ref in report.artifact_refs}
                or artifact_ref in {self._clean_markdown_ref(ref) for ref in report.changed_files}
            )
            for report in reports
        ):
            return True
        if report_id:
            return False
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return False
        try:
            self._resolve_workspace_markdown_path(workspace, artifact_ref)
        except (KeyError, ValueError, OSError):
            return False
        return True

    def _resolve_workspace_markdown_path(
        self,
        workspace: Workspace,
        artifact_ref: str,
        report_id: str | None = None,
    ) -> Path:
        if not self._path_looks_like_real_file(artifact_ref):
            raise KeyError(artifact_ref)
        if self._safe_lower_suffix(artifact_ref) not in MARKDOWN_ARTIFACT_SUFFIXES:
            raise ValueError("Only Markdown artifact previews are supported")
        try:
            snapshot_path = self.snapshot_path(workspace.id).resolve()
        except OSError as exc:
            raise KeyError(artifact_ref) from exc
        try:
            path = Path(artifact_ref).expanduser()
        except (OSError, ValueError):
            raise KeyError(artifact_ref)
        try:
            is_absolute = path.is_absolute()
        except OSError:
            is_absolute = False
        if is_absolute:
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise KeyError(artifact_ref) from exc
            if resolved == snapshot_path:
                return resolved
            self._ensure_path_under_roots(
                resolved,
                self._markdown_allowed_roots(workspace, report_id),
                artifact_ref,
            )
            return resolved

        roots = self._markdown_allowed_roots(workspace, report_id)
        for root in roots:
            try:
                resolved = (root / path).resolve(strict=True)
                self._ensure_path_under_roots(resolved, [root], artifact_ref)
            except (OSError, KeyError):
                continue
            return resolved
        raise KeyError(artifact_ref)

    def _ensure_path_under_roots(
        self,
        resolved: Path,
        roots: list[Path],
        artifact_ref: str,
    ) -> None:
        if not resolved.is_file():
            raise KeyError(artifact_ref)
        for root in roots:
            try:
                resolved.relative_to(root)
                return
            except ValueError:
                continue
        raise KeyError(artifact_ref)

    def _markdown_allowed_roots(
        self,
        workspace: Workspace,
        report_id: str | None = None,
    ) -> list[Path]:
        try:
            roots: list[Path] = [Path(workspace.path).expanduser().resolve()]
        except (OSError, ValueError):
            roots = []
        report = self.reports.get(report_id) if report_id else None
        if report:
            session = self.sessions.get(report.session_id)
            if session and session.workspace_path:
                try:
                    session_root = Path(session.workspace_path).expanduser().resolve()
                except (OSError, ValueError):
                    session_root = None
                if session_root is not None and session_root not in roots:
                    roots.append(session_root)
        # Agents work inside isolated git worktrees, so a report's markdown
        # artifact often lives only in a worktree rather than under workspace.path
        # or the session workspace_path. Add the workspace's git worktree
        # directories as allowed roots so those artifacts resolve for preview.
        for worktree_root in self._git_worktree_roots(workspace):
            if worktree_root not in roots:
                roots.append(worktree_root)
        return roots

    def _git_worktree_roots(self, workspace: Workspace) -> list[Path]:
        """Return resolved git worktree directories for *workspace*.

        Worktrees are enumerated via ``git worktree list --porcelain`` run from
        the workspace path. The result is cached per workspace for a short TTL so
        building the board (which resolves every report ref) does not spawn one
        subprocess per ref. Returns an empty list when git is unavailable, the
        path is not a git repository, or the call times out.
        """
        try:
            base = Path(workspace.path).expanduser().resolve()
        except (OSError, ValueError):
            return []

        cache = self._worktree_root_cache
        now = _now().timestamp()
        cached = cache.get(workspace.id)
        if cached is not None:
            cached_at, cached_roots = cached
            if now - cached_at < WORKTREE_ROOT_CACHE_TTL_SECONDS:
                return list(cached_roots)

        roots = self._read_git_worktree_roots(base)
        cache[workspace.id] = (now, roots)
        return list(roots)

    @staticmethod
    def _read_git_worktree_roots(base: Path) -> list[Path]:
        try:
            result = subprocess.run(
                ["git", "-C", str(base), "worktree", "list", "--porcelain"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=WORKTREE_LIST_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        roots: list[Path] = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("worktree "):
                continue
            raw = line[len("worktree ") :].strip()
            if not raw:
                continue
            try:
                resolved = Path(raw).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if resolved not in roots:
                roots.append(resolved)
        return roots

    def markdown_documents_for_workspace(
        self,
        workspace_id: str,
    ) -> list[WorkspaceMarkdownDocument]:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)
        if workspace.target != ExecutionTarget.LOCAL:
            return []

        documents: dict[tuple[str, str, str | None], WorkspaceMarkdownDocument] = {}
        for report in self.reports_for_workspace(workspace_id):
            for source, refs in (
                (WorkspaceMarkdownDocumentSource.ARTIFACT, report.artifact_refs),
                (WorkspaceMarkdownDocumentSource.CHANGED_FILE, report.changed_files),
            ):
                for raw_ref in refs:
                    artifact_ref = self._clean_markdown_ref(raw_ref)
                    if not self._path_looks_like_real_file(artifact_ref):
                        continue
                    if self._safe_lower_suffix(artifact_ref) not in MARKDOWN_ARTIFACT_SUFFIXES:
                        continue
                    try:
                        resolved = self._resolve_workspace_markdown_path(
                            workspace,
                            artifact_ref,
                            report.id,
                        )
                    except (KeyError, ValueError, OSError):
                        continue
                    self._add_markdown_document(
                        documents,
                        source=source,
                        artifact_ref=artifact_ref,
                        resolved=resolved,
                        task_id=report.task_id,
                        report_id=report.id,
                        session_id=report.session_id,
                    )

        snapshot = self.snapshot_path(workspace_id)
        try:
            snapshot_resolved = snapshot.resolve() if snapshot.exists() else None
        except OSError:
            snapshot_resolved = None
        if snapshot_resolved is not None:
            self._add_markdown_document(
                documents,
                source=WorkspaceMarkdownDocumentSource.SNAPSHOT,
                artifact_ref=str(snapshot),
                resolved=snapshot_resolved,
                task_id=None,
                report_id=None,
                session_id=None,
                label="Workspace snapshot",
            )

        self._add_discovered_markdown_documents(workspace, documents)
        return sorted(
            documents.values(),
            key=lambda item: (
                item.source != WorkspaceMarkdownDocumentSource.ARTIFACT,
                item.source != WorkspaceMarkdownDocumentSource.CHANGED_FILE,
                item.source != WorkspaceMarkdownDocumentSource.SNAPSHOT,
                item.label.lower(),
            ),
        )

    def _add_markdown_document(
        self,
        documents: dict[tuple[str, str, str | None], WorkspaceMarkdownDocument],
        *,
        source: WorkspaceMarkdownDocumentSource,
        artifact_ref: str,
        resolved: Path,
        task_id: str | None,
        report_id: str | None,
        session_id: str | None,
        label: str | None = None,
    ) -> None:
        try:
            stat = resolved.stat()
        except OSError:
            stat = None
        key = (source.value, artifact_ref, report_id)
        documents.setdefault(
            key,
            WorkspaceMarkdownDocument(
                id="::".join(part for part in key if part),
                path=artifact_ref,
                label=label or self._display_markdown_path(artifact_ref, resolved),
                source=source,
                task_id=task_id,
                report_id=report_id,
                session_id=session_id,
                size_bytes=stat.st_size if stat else None,
                updated_at=datetime.fromtimestamp(stat.st_mtime) if stat else None,
            ),
        )

    def _display_markdown_path(self, artifact_ref: str, resolved: Path) -> str:
        try:
            path = Path(artifact_ref)
            is_absolute = path.is_absolute()
        except (OSError, ValueError):
            is_absolute = False
        if not is_absolute:
            return artifact_ref
        for workspace in self.workspaces.values():
            try:
                return str(resolved.relative_to(Path(workspace.path).expanduser().resolve()))
            except (ValueError, OSError):
                continue
        try:
            return resolved.name
        except OSError:
            return artifact_ref

    def _add_discovered_markdown_documents(
        self,
        workspace: Workspace,
        documents: dict[tuple[str, str, str | None], WorkspaceMarkdownDocument],
    ) -> None:
        root = Path(workspace.path).expanduser().resolve()
        if not root.exists():
            return
        candidates: list[Path] = []
        for pattern in ("*.md", "docs/**/*.md"):
            for path in root.glob(pattern):
                if len(candidates) >= MARKDOWN_DISCOVERY_LIMIT:
                    break
                if any(part in MARKDOWN_DISCOVERY_EXCLUDED_DIRS for part in path.parts):
                    continue
                if not path.is_file():
                    continue
                candidates.append(path)
        for path in candidates[:MARKDOWN_DISCOVERY_LIMIT]:
            try:
                resolved = path.resolve(strict=True)
                artifact_ref = str(resolved.relative_to(root))
            except (OSError, ValueError):
                continue
            self._add_markdown_document(
                documents,
                source=WorkspaceMarkdownDocumentSource.DISCOVERED,
                artifact_ref=artifact_ref,
                resolved=resolved,
                task_id=None,
                report_id=None,
                session_id=None,
            )
