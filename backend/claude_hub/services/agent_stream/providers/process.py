"""ProcessSession implementation; compatibility dependencies are explicit."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any, Dict, List, Optional

from .session import SessionCore

# Provider CLIs spawn their own children (bash tool shells, MCP servers).
# Starting each provider in a fresh session makes the direct child a process
# group leader (pgid == its pid), so teardown can signal the whole tree with
# killpg instead of orphaning the grandchildren. Unsupported on non-POSIX,
# where teardown falls back to signalling the direct child only.
_NEW_SESSION_KWARGS: Dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}


class ProcessSession(SessionCore):
    """Transport operations over the state owned by the typed base."""

    async def start(self) -> None:
        """Launch any persistent provider process (Codex app-server).

        For one-shot providers (Claude / Cursor) this is a no-op; the
        subprocess is spawned per turn in :meth:`send_message`.
        """
        self._started = True

    async def stop(self) -> None:
        """Terminate the provider subprocess and stop the readers."""
        self._started = False
        await self._terminate_process()
        # Resolve any pending turn completion so a caller awaiting the turn
        # does not hang after the transport is stopped.
        self._end_turn()

    async def restart_for_recovery(self) -> None:
        """Kill and relaunch the provider after a turn could not be cancelled.

        Persistent app-servers (Codex/TraeX) resume the same conversation in
        :meth:`start` (``thread/resume``), so the tab stays usable after a
        wedged turn instead of failing closed. The dying process's stdout EOF
        is marked expected via ``_client_requested_stop`` so the tailer treats
        that EOF as a restart handoff rather than a crashed server. The flag is
        deliberately left set: the push loop clears it only once the
        replacement transport is confirmed running, which also covers the case
        where the old queue's EOF sentinel is consumed slightly later.
        """
        self._client_requested_stop = True
        await self.stop()
        await self.start()

    def _invalidate_stdout_stream(self) -> None:
        """Retire the current stdout stream.

        Retired records and EOF sentinels stop being readable, and the next
        reader starts from the following generation.

        A reader's farewell is ambiguous by construction: a cancelled reader
        publishes an EOF sentinel so a consumer blocked in :meth:`read_line`
        cannot hang, and that sentinel reports *our* cancellation rather than
        the provider's end of stream. The two are indistinguishable by value,
        so the generation tag is the only witness. Retiring the stream is what
        makes the sentinel (and any records still queued behind it) unreadable,
        so a cancelled turn can never be mistaken for a provider that ended
        without a completion record.

        ``_terminate_process`` calls this before cancelling the reader it is
        about to kill, so shutdown, cancellation, and turn rollover all retire
        the stream they end, and none can forget to.
        """
        self._stdout_generation += 1

    async def _terminate_process(self) -> None:
        """Kill the current subprocess and its reader tasks.

        This does NOT call ``_end_turn`` — it is used both for full transport
        shutdown (where ``stop`` calls ``_end_turn`` separately) and for
        rolling over from one one-shot turn to the next (where the new turn's
        completion is resolved by the new process's EOF, not by the old
        process's death).
        """
        # Retire the reader BEFORE cancelling it: its ``finally`` runs while we
        # await it below, so the sentinel it publishes must already be stale.
        self._invalidate_stdout_stream()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        proc = self._process
        pgid = self._process_group
        self._process = None
        self._process_group = 0
        if proc is not None:
            await self._terminate_subprocess(proc, pgid)

    async def _terminate_subprocess(self, proc: asyncio.subprocess.Process, pgid: int) -> None:
        """SIGTERM the provider, then SIGKILL after the five-second grace period.

        When ``pgid`` is set the provider owns its process group, so the signal
        reaches its child tool/MCP processes too; otherwise only the direct
        child is signalled (non-POSIX, or a process not spawned by
        :meth:`_spawn_subprocess`), matching the previous behavior.
        """
        if not self._signal_process_group(pgid, signal.SIGTERM):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, OSError):
            if not self._signal_process_group(pgid, signal.SIGKILL):
                try:
                    proc.kill()
                except OSError:
                    pass
            # Reap the SIGKILLed child so it cannot linger as a zombie and
            # ``returncode`` is settled before teardown returns.
            try:
                await proc.wait()
            except OSError:
                pass

    @staticmethod
    def _signal_process_group(pgid: int, sig: int) -> bool:
        """Signal every process in the provider's group.

        Returns ``True`` when group signalling was attempted (including a
        group that is already gone), ``False`` when it is unavailable and the
        caller must target the direct child instead.
        """
        if pgid <= 0 or os.name != "posix":
            return False
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            # The whole group has already exited; ``proc.wait()`` reaps it.
            pass
        except PermissionError:
            return False
        return True

    async def _spawn_subprocess(
        self, cmd: List[str], env: Optional[Dict[str, str]]
    ) -> asyncio.subprocess.Process:
        """Launch a provider subprocess and record its process group.

        All provider spawn sites go through this so both the one-shot CLIs and
        the persistent app-server share the same piping and group semantics.
        """
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self._cwd or None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **_NEW_SESSION_KWARGS,
        )
        # With ``start_new_session`` the child is the group leader, so its pid
        # is the pgid. Leave this at zero on non-POSIX / pid-less transports
        # (test doubles) so teardown targets the direct child instead.
        if _NEW_SESSION_KWARGS:
            pid = getattr(process, "pid", None)
            self._process_group = pid if isinstance(pid, int) else 0
        return process

    async def read_line(self) -> Optional[Dict[str, Any]]:
        """Await one parsed JSON record from stdout, or ``None`` on EOF."""
        while True:
            generation, record = await self._stdout_queue.get()
            if generation == self._stdout_generation:
                return record

    async def _drain_oneshot_stdout(self, generation: int) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        cancelled = False
        # Read stdout in chunks and split on newlines manually. We cannot use
        # ``StreamReader.readline()`` because it enforces a 64 KiB line-length
        # limit (``LimitOverrunError``); Claude can emit single JSON records
        # larger than that (e.g. a tool result carrying a large file's
        # contents), which would crash the drain and make the tailer think the
        # provider exited without a completion record.
        buffer = b""
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", errors="ignore").strip()
                    if not text:
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        await self._stdout_queue.put((generation, record))
            # Process any trailing data without a newline.
            if buffer:
                text = buffer.decode("utf-8", errors="ignore").strip()
                if text:
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        record = None
                    if isinstance(record, dict):
                        await self._stdout_queue.put((generation, record))
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            self._runtime.logger().exception("native transport stdout drain failed")
        finally:
            if cancelled:
                # The turn was rolled over or the transport is shutting down.
                # ``_terminate_process`` will kill the subprocess after this
                # task returns, so we must NOT await ``proc.wait()`` here (it
                # could deadlock). Just signal EOF so the tailer's
                # ``read_line`` does not block forever.
                #
                # This sentinel reports our cancellation, not the provider's
                # end of stream; the tailer must never read it as the latter,
                # or a killed reader would look like a provider that exited
                # without a completion record. ``_terminate_process`` retires
                # this generation before cancelling us, so the sentinel is
                # stale on arrival and ``read_line`` discards it — publishing
                # it is inert, and nothing may rely on it as a wakeup. Nothing
                # is left parked on a silent stream either: whoever killed this
                # reader installs a replacement reader, breaks out of its
                # consume loop, or cancels the consumer.
                await self._stdout_queue.put((generation, None))
                return
            # Natural EOF: the provider's stdout has closed. Wait for the
            # process to fully exit so we can inspect its return code. A
            # nonzero exit (or an exit before any recognized completion
            # record) is a turn failure: capture the bounded stderr and
            # expose it as ``exit_error`` so the tailer can emit an ``error``
            # event and a failed ``turn_completed``.
            try:
                returncode = await proc.wait()
            except Exception:
                returncode = -1
            # Ensure the stderr drain has finished capturing all output before
            # we read the bounded buffer.
            stderr_task = self._stderr_task
            if stderr_task is not None:
                try:
                    await stderr_task
                except Exception:
                    pass
            if returncode != 0:
                stderr_text = self._runtime.redact_diagnostic(
                    self._stderr_buffer.decode("utf-8", errors="ignore").strip()
                )
                self._exit_error = f"provider exited with code {returncode}" + (
                    f": {stderr_text}" if stderr_text else ""
                )
            # Signal EOF to consumers. The tailer processes all queued records,
            # then this EOF sentinel. The turn guard is normally released at
            # ``TURN_COMPLETED`` (the provider's final record); the EOF handler
            # only calls ``acknowledge_turn_complete`` if no completion record
            # was seen (synthesizing a failed turn). We must NOT call
            # ``_end_turn`` here: doing so would clear ``_turn_in_flight``
            # before the tailer has consumed the turn's records, allowing a
            # concurrent ``send_message`` to overwrite ``_active_turn_id``
            # while this turn's records are still queued.
            await self._stdout_queue.put((generation, None))

    async def _drain_stderr(self) -> None:
        """Bounded stderr capture; surfaced as ``last_error`` on non-zero exit."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_buffer = (self._stderr_buffer + chunk)[
                    -self._runtime.stderr_buffer_max() :
                ]
        except asyncio.CancelledError:
            raise
        except Exception:
            self._runtime.logger().exception("native transport stderr drain failed")

    async def _spawn_oneshot(self, cmd: List[str], stdin_text: str) -> None:
        """Spawn a one-shot streaming subprocess for Claude/Cursor turns.

        The stdout queue is NOT replaced between turns because that would
        strand a consumer awaiting the old queue object. Instead, every item
        carries the process generation that produced it and ``read_line``
        discards records and EOF sentinels from superseded generations.
        """
        # Stop any previous turn's process before starting a new one. This
        # awaits the old reader before launching the replacement. We use
        # ``_terminate_process`` (not ``stop``) so we do NOT call
        # ``_end_turn`` — the new turn's completion is resolved by its own
        # provider output, not by the old process's death.
        #
        # ``_terminate_process`` also retires the previous reader, so the
        # generation it leaves behind is this process's own. That keeps the
        # advance per spawn at exactly one, which is what
        # ``CursorNativeSession._send_text`` relies on when it registers
        # staged images under the predicted next generation.
        provider_env = self._prepared_provider_env
        self._prepared_provider_env = None
        if provider_env is None:
            provider_env = await self._build_provider_env()
        await self._terminate_process()
        self.check_delivery_admission()
        generation = self._stdout_generation
        self._stderr_buffer = b""
        self._exit_error = None
        try:
            self._process = await self._spawn_subprocess(cmd, provider_env)
        except FileNotFoundError:
            self._last_error = f"provider binary not found: {cmd[0]}"
            self._end_turn()
            raise
        except OSError as exc:
            self._last_error = f"failed to launch provider: {exc}"
            self._end_turn()
            raise
        try:
            self.check_delivery_admission()
        except BaseException:
            # Spawn itself yields. A Stop/configuration change while it runs
            # must kill this empty process before its stdin receives a prompt.
            await self._terminate_process()
            raise
        self._started = True
        self._reader_task = asyncio.create_task(self._drain_oneshot_stdout(generation))
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        # Feed the prompt to the provider's stdin.
        if self._process.stdin is not None:
            self._process.stdin.write(stdin_text.encode("utf-8"))
            await self._process.stdin.drain()
            self._process.stdin.close()
