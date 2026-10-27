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
#
# Caveat: because providers own their own session, they no longer die
# implicitly when the backend's own process group is signalled (e.g. a
# terminal SIGINT). Clean shutdown depends on the lifespan handler awaiting
# every transport's ``stop``; a hard ``SIGKILL`` of the backend leaves the
# provider groups running.
# Also note a descendant that itself calls setsid(2)/setpgid(2) leaves the
# provider's group and can no longer be reached by these killpg calls.
_NEW_SESSION_KWARGS: Dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}

# One shared grace budget for teardown: counted from the group SIGTERM, it
# covers BOTH the direct child's exit and the whole group draining. A fast
# leader does not restart the clock for lazy descendants.
_TERMINATE_GRACE_SECONDS = 5.0
# How often to poll ``killpg(pgid, 0)`` while waiting for the group to drain.
_GROUP_POLL_INTERVAL = 0.1
# Bounded wait for the direct child after SIGKILL; teardown must never hang.
_KILL_REAP_TIMEOUT = 2.0
# Best-effort window for killed descendants to disappear after reaping the
# direct child. A final ``killpg(pgid, 0)`` check is sufficient; an unrelated
# process that somehow joined the pgid must not strand teardown forever.
_POST_KILL_GROUP_DRAIN_SECONDS = 1.0


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
        """SIGTERM the provider's group, then SIGKILL whatever survives grace.

        The five-second grace budget is shared: it starts at the SIGTERM and
        covers both the direct child's exit and the group draining. Provider
        CLIs normally exit on SIGTERM while TERM-trapping descendants (MCP
        servers shutting down, trapped shells, nohup-style children) stay
        alive reparented to init; leader death alone is therefore not treated
        as group death. The group is polled with ``killpg(pgid, 0)`` until it
        disappears or the deadline expires, and then the WHOLE group is
        escalated to SIGKILL. ``proc.wait()`` is awaited concurrently only as
        a promptness optimization: asyncio does not resolve it until every
        inherited stdout/stderr/stdio pipe has disconnected, which a
        surviving pipe-holding grandchild delays. When the wait resolves
        early while a pipe-free descendant lingers, group liveness polling
        is paced by an explicit poll-interval sleep so a completed future
        cannot busy-spin the loop back-to-back until the deadline.

        If teardown itself is cancelled during grace, the group is SIGKILLed
        immediately and the direct child gets a final bounded, shielded reap
        before ``CancelledError`` propagates, so cancellation never leaves a
        live tree or a zombie. When ``pgid`` is unset (non-POSIX, a process
        not spawned by :meth:`_spawn_subprocess`, or group signalling denied
        by ``PermissionError``) only the direct child is signalled, matching
        the previous behavior. Descendants that called ``setsid``/``setpgid``
        themselves escaped the group and cannot be reached here.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TERMINATE_GRACE_SECONDS
        # ``_signal_process_group`` returns False when group signalling is
        # unavailable; fall back to direct-child semantics for the whole
        # teardown (signals, liveness checks and escalation), rather than
        # polling a group we demonstrably cannot reach.
        group_mode = self._signal_process_group(pgid, signal.SIGTERM)
        if not group_mode:
            try:
                proc.terminate()
            except OSError:
                pass

        wait_task: asyncio.Task[int] = asyncio.ensure_future(proc.wait())
        try:
            while loop.time() < deadline:
                if wait_task.done():
                    # The leader is already reaped. In fallback mode that is
                    # the whole teardown condition. In group mode a descendant
                    # may still be shutting down; if it dropped the inherited
                    # stdio pipes at exec, the wait task resolves with the
                    # group still alive and can no longer pace this loop, so
                    # sleep the poll interval instead of re-awaiting the
                    # completed future — re-awaiting it returns immediately
                    # and busy-spins killpg(pgid, 0) until the deadline.
                    if not group_mode or not self._process_group_alive(pgid):
                        return
                    remaining = max(0.0, deadline - loop.time())
                    await asyncio.sleep(min(_GROUP_POLL_INTERVAL, remaining))
                    continue
                remaining = max(0.0, deadline - loop.time())
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wait_task),
                        timeout=min(_GROUP_POLL_INTERVAL, remaining),
                    )
                except (asyncio.TimeoutError, OSError):
                    # Poll interval elapsed, or the reap itself errored; in
                    # the latter case the deadline path still SIGKILLs, and
                    # signalling an already-dead pid is tolerated below.
                    pass
                # The leader must be reaped AND (in group mode) every group
                # member gone. With pipe-holding descendants the two converge
                # within a tick: once the last member dies the inherited pipe
                # fds close and the wait task resolves. A descendant that
                # dropped the pipes is handled by the paced branch above.
                if wait_task.done() and (not group_mode or not self._process_group_alive(pgid)):
                    return
            # Grace elapsed with the leader or a descendant still alive:
            # escalate the WHOLE group (or the direct child in fallback).
            self._escalate_to_sigkill(pgid, group_mode, proc)
            await self._settle_after_kill(loop, pgid, group_mode, wait_task)
        except asyncio.CancelledError:
            # Teardown was cancelled mid-grace: no more waiting. SIGKILL the
            # tree immediately; the final reap runs in ``finally``.
            self._escalate_to_sigkill(pgid, group_mode, proc)
            raise
        finally:
            await self._reap_subprocess(wait_task)

    @staticmethod
    def _escalate_to_sigkill(pgid: int, group_mode: bool, proc: asyncio.subprocess.Process) -> None:
        """SIGKILL the group, or the direct child when groups are unusable."""
        if group_mode and ProcessSession._signal_process_group(pgid, signal.SIGKILL):
            return
        try:
            proc.kill()
        except OSError:
            pass

    @staticmethod
    async def _settle_after_kill(
        loop: asyncio.AbstractEventLoop,
        pgid: int,
        group_mode: bool,
        wait_task: "asyncio.Task[int]",
    ) -> None:
        """Reap the SIGKILLed leader and briefly observe the group draining."""
        if not wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=_KILL_REAP_TIMEOUT)
            except (asyncio.TimeoutError, OSError):
                pass
        if group_mode:
            drain_deadline = loop.time() + _POST_KILL_GROUP_DRAIN_SECONDS
            while loop.time() < drain_deadline:
                if not ProcessSession._process_group_alive(pgid):
                    break
                await asyncio.sleep(_GROUP_POLL_INTERVAL)

    @staticmethod
    async def _reap_subprocess(wait_task: "asyncio.Task[int]") -> None:
        """Final bounded, shielded reap of the direct child.

        Runs on every teardown path (success, deadline escalation and
        cancellation) so the child can never linger as a zombie. Swallowing
        ``CancelledError`` here means a second cancellation while reaping
        cannot abort the best-effort wait: the original cancellation still
        propagates from the caller.
        """
        if not wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=_KILL_REAP_TIMEOUT)
            except (asyncio.CancelledError, asyncio.TimeoutError, OSError):
                pass
        if wait_task.done() and not wait_task.cancelled():
            try:
                wait_task.result()
            except (asyncio.CancelledError, OSError):
                pass

    @staticmethod
    def _process_group_alive(pgid: int) -> bool:
        """Whether any process still exists in ``pgid``.

        A signal-0 check: ``ProcessLookupError`` means the group is empty,
        ``PermissionError`` means a member exists under another uid (treat it
        as alive so teardown keeps waiting / escalates).
        """
        if pgid <= 0 or os.name != "posix":
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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

        Caveat: a descendant that itself calls ``setsid``/``setpgid`` leaves
        this group and cannot be signalled or reaped by teardown's killpg.
        """
        # Reset BEFORE the await on every platform: if a stop()/restart races
        # the spawn, teardown must not see the previous process's pgid, and
        # non-POSIX no longer relies on a prior teardown having cleared this.
        self._process_group = 0
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
