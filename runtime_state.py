"""Runtime state tracking for MCP tool execution.

A tool call is the unit of work for a request-driven MCP server (there is no
poll loop), so counters are keyed off ``on_call_tool`` middleware events
instead of iterations.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastmcp.server.middleware import Middleware

# Isolated user errors are normal; only a sustained streak means the server is broken.
CONSECUTIVE_FAILURE_THRESHOLD = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServerRuntimeState:
    """Thread-safe counters and last-error state for MCP tool calls."""

    def __init__(self, failure_threshold: int = CONSECUTIVE_FAILURE_THRESHOLD) -> None:
        self._lock = threading.Lock()
        self._failure_threshold = failure_threshold
        self._started_at_utc = _now_iso()
        self._tool_call_count = 0
        self._failed_call_count = 0
        self._in_flight_call_count = 0
        self._consecutive_failure_count = 0
        self._last_call_tool: str | None = None
        self._last_call_started_utc: str | None = None
        self._last_call_completed_utc: str | None = None
        self._last_call_duration_ms: int | None = None
        self._last_error: str | None = None
        self._last_error_type: str | None = None
        self._last_error_tool: str | None = None
        self._last_error_utc: str | None = None

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    def record_call_started(self, tool_name: str) -> float:
        with self._lock:
            self._tool_call_count += 1
            self._in_flight_call_count += 1
            self._last_call_tool = tool_name
            self._last_call_started_utc = _now_iso()
        return time.perf_counter()

    def record_call_succeeded(self, started_at: float) -> None:
        with self._lock:
            self._in_flight_call_count = max(0, self._in_flight_call_count - 1)
            self._consecutive_failure_count = 0
            self._last_call_completed_utc = _now_iso()
            self._last_call_duration_ms = round((time.perf_counter() - started_at) * 1000)

    def record_call_failed(self, tool_name: str, error: BaseException, started_at: float) -> None:
        with self._lock:
            self._in_flight_call_count = max(0, self._in_flight_call_count - 1)
            self._failed_call_count += 1
            self._consecutive_failure_count += 1
            self._last_call_completed_utc = _now_iso()
            self._last_call_duration_ms = round((time.perf_counter() - started_at) * 1000)
            self._last_error = str(error)
            self._last_error_type = type(error).__name__
            self._last_error_tool = tool_name
            self._last_error_utc = self._last_call_completed_utc

    def is_healthy(self) -> bool:
        with self._lock:
            return self._consecutive_failure_count < self._failure_threshold

    def status(self, *, include_error_message: bool = False) -> dict[str, Any]:
        """Snapshot of runtime counters.

        ``include_error_message`` is off by default because exception text can
        carry upstream details that should not be exposed on a public probe.
        """
        with self._lock:
            snapshot: dict[str, Any] = {
                "running": True,
                "startedAtUtc": self._started_at_utc,
                "toolCallCount": self._tool_call_count,
                "failedCallCount": self._failed_call_count,
                "inFlightCallCount": self._in_flight_call_count,
                "consecutiveFailureCount": self._consecutive_failure_count,
                "failureThreshold": self._failure_threshold,
                "lastCallTool": self._last_call_tool,
                "lastCallStartedUtc": self._last_call_started_utc,
                "lastCallCompletedUtc": self._last_call_completed_utc,
                "lastCallDurationMs": self._last_call_duration_ms,
                "lastErrorType": self._last_error_type,
                "lastErrorTool": self._last_error_tool,
                "lastErrorUtc": self._last_error_utc,
            }
            if include_error_message:
                snapshot["lastError"] = self._last_error
            return snapshot

    def reset(self) -> None:
        self.__init__(self._failure_threshold)


runtime_state = ServerRuntimeState()


class RuntimeTrackingMiddleware(Middleware):
    """Feeds tool call outcomes into a ServerRuntimeState."""

    def __init__(self, state: ServerRuntimeState | None = None) -> None:
        self._state = state or runtime_state

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        tool_name = str(getattr(getattr(context, "message", None), "name", "unknown"))
        started_at = self._state.record_call_started(tool_name)
        try:
            result = await call_next(context)
        except Exception as error:
            self._state.record_call_failed(tool_name, error, started_at)
            raise
        self._state.record_call_succeeded(started_at)
        return result
