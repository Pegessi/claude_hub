"""CursorSession implementation; compatibility dependencies are explicit."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ....models import ChatMode, ManagedSession, StreamModeOption
from .process import ProcessSession


class CursorSession(ProcessSession):
    """Cursor Agent native transport via ``stream-json`` stdout.

    Each turn spawns ``agent --trust --print --output-format stream-json
    --stream-partial-output`` with the user prompt on stdin. The
    conversation id is captured for ``--resume`` on the next turn.

    Images: Cursor's CLI has no structured image-input flag, so attached
    images are staged to temp files and referenced by absolute path in a
    sentinel-wrapped prompt block; the model reads them with its multimodal
    Read tool. The temp files are deleted when the one-shot process exits
    (see :meth:`_drain_oneshot_stdout`) and on :meth:`stop`.
    """

    adapter_id = "cursor-native"
    schema_version = 1
    supports_tool_timeline = True
    supports_approval_ui = True
    # Images are delivered as file references in the prompt (Read tool), not
    # via a structured CLI flag. Validated empirically against agent
    # v2026.09.02: the model reads the referenced file and answers about it.
    supports_images = True

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(
            session,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        # Staged image temp files for the next turn's prompt.
        self._staged_images: List[Path] = []
        # Image temp files owned by each in-flight one-shot process, keyed by
        # the stdout generation ``_spawn_oneshot`` assigns that process's
        # drain. A drain deletes ONLY its own generation's files in its
        # finally (see ``_drain_oneshot_stdout``). A single shared slot would
        # be clobbered by a later turn before a lingering previous drain is
        # cancelled, making that drain's cleanup delete the WRONG turn's
        # files (and leak the previous turn's).
        self._inflight_images_by_gen: Dict[int, List[Path]] = {}

    def _build_command(self) -> List[str]:
        cmd = [
            "agent",
            "--trust",
            "--print",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
        ]
        # Cursor does not read a model env var; the model must be passed via
        # the ``--model`` flag. If the session env carries ``CURSOR_MODEL``
        # (set by the frontend model picker), forward it as a flag.
        cursor_model = self.session.env.get("CURSOR_MODEL")
        if isinstance(cursor_model, str) and cursor_model:
            cmd.extend(["--model", cursor_model])
        if self.session.solo_mode and self._current_mode == ChatMode.DEFAULT.value:
            cmd.append("--yolo")
        if self._current_mode == ChatMode.PLAN.value:
            cmd.extend(["--mode", "plan"])
        cmd.extend(self._resume_arg())
        return cmd

    def available_modes(self) -> List[StreamModeOption]:
        if self._runtime.help_confirms_plan_mode("agent", "--mode"):
            return [self._runtime.default_mode(), self._runtime.plan_mode()]
        return [self._runtime.default_mode()]

    def _resume_arg(self) -> List[str]:
        """Cursor Agent's ``--resume`` is constructive: if the session does
        not exist it is created. We can therefore always pass ``--resume``
        with the pinned id, even on the first turn."""
        sid = self._conversation_id
        if sid:
            return ["--resume", sid]
        return []

    async def _send_text(self, text: str) -> None:
        cmd = self._build_command()
        # Cursor's CLI has no system-prompt flag, so deliver the
        # question-protocol guidance as a sentinel-wrapped prefix on the
        # prompt (mirroring Claude's --append-system-prompt). The adapter
        # strips the sentinel block on transcript read so it never reaches
        # the persisted timeline or the UI.
        prompt = self._runtime.wrap_question_guidance(text)
        # Reference any staged images by absolute path in a second
        # sentinel-wrapped block so the model reads them with its Read tool.
        image_paths = self._staged_images
        self._staged_images = []
        if image_paths:
            prompt = self._runtime.wrap_image_guidance(prompt, image_paths)
        # Transfer ownership of the image temp files to this turn's process
        # BEFORE spawning. ``_spawn_oneshot`` ends with exactly one generation
        # advance — the one ``_terminate_process`` performs while retiring the
        # previous reader — and spawns the drain with that generation, so
        # predict it and register the files under it: the drain deletes only
        # its own generation's files in its finally, never a later turn's.
        # ``_send_text`` runs under ``_send_lock`` and only after the previous
        # turn's ``acknowledge_turn_complete`` released the turn guard, so
        # ``_stdout_generation`` is stable here and the prediction is exact.
        # If the spawn fails (or the turn is cancelled before the reader task
        # exists), no process will read the files, so clean them up. Catch
        # ``CancelledError`` too — it is a ``BaseException`` and would
        # otherwise skip the cleanup and leak the files.
        generation = self._stdout_generation + 1
        self._inflight_images_by_gen[generation] = image_paths
        try:
            await self._spawn_oneshot(cmd, prompt)
        except (Exception, asyncio.CancelledError):
            self._inflight_images_by_gen.pop(generation, None)
            self._cleanup_images(image_paths)
            raise

    async def _drain_oneshot_stdout(self, generation: int) -> None:
        # The one-shot process has fully exited by the time the base drain
        # returns (natural EOF awaits proc.wait()) or is being terminated
        # (cancellation). Either way it has finished reading the staged
        # image files, so they are safe to delete. Pop ONLY this drain's
        # generation: a later turn may already have registered its own
        # files under a newer generation, and a shared-slot cleanup would
        # delete that later turn's files (and leak this turn's).
        try:
            await super()._drain_oneshot_stdout(generation)
        finally:
            self._cleanup_images(self._inflight_images_by_gen.pop(generation, []))

    async def stop(self) -> None:
        await super().stop()
        self._clear_staged_images()
        # super().stop() terminated the active drain (its finally popped its
        # own generation); sweep any generations still registered so no
        # in-flight image files survive shutdown.
        for paths in list(self._inflight_images_by_gen.values()):
            self._cleanup_images(paths)
        self._inflight_images_by_gen.clear()

    def _stage_images(self, images: List[bytes]) -> None:
        """Stage images to temp files referenced by path in the next prompt.

        Cursor's CLI has no structured image-input flag, so the bytes are
        staged to temp files (validated by magic bytes and size) inside the
        app-owned 0700 Cursor image temp directory and referenced by absolute
        path in the next prompt; the model reads them with its multimodal
        Read tool. Temp files are deleted when the one-shot process exits
        (success or failure) and on ``stop``.
        """
        if not images:
            return
        temp_dir = self._runtime.cursor_image_temp_dir()
        for img in images:
            media_type = self._runtime.detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            # Use a unique name inside the app-owned temp dir. ``mkstemp``
            # would create the file in the system temp dir; we want it under
            # our 0700 directory so the backend owns the lifecycle.
            fd, path = tempfile.mkstemp(
                prefix="cursor-img-",
                suffix=self._runtime.mime_extension(media_type),
                dir=str(temp_dir),
            )
            f = None
            try:
                f = os.fdopen(fd, "wb")
                f.write(img)
                # Flush explicitly so a write error (e.g. ENOSPC) surfaces
                # HERE and triggers the unlink + re-raise below. Without it
                # the bytes sit in the buffer until close(), whose OSError
                # the finally swallows — staging a silently truncated file
                # the model would then read.
                f.flush()
            except Exception:
                # Best-effort cleanup: never let a close/unlink error mask
                # the original write exception.
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            finally:
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
                else:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            # Files are created 0600 by mkstemp, but chmod to defeat umask.
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._staged_images.append(Path(path))

    def _clear_staged_images(self) -> None:
        staged = self._staged_images
        self._staged_images = []
        self._cleanup_images(staged)

    @staticmethod
    def _cleanup_images(paths: List[Path]) -> None:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Extract the conversation id from Cursor's system init record.

        Cursor's stream-json stdout begins with
        ``{type:"system", subtype:"init", session_id:"..."}``. That
        ``session_id`` is the value to pass to ``--resume`` on the next turn.
        """
        if record.get("type") != "system":
            return
        if record.get("subtype") != "init":
            return
        sid = record.get("session_id")
        if isinstance(sid, str) and sid:
            self._persist_conversation_id(sid)
            self._handshake_complete = True
