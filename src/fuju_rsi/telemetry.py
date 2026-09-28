"""可替换的观测出口；失败时追加最小化 JSONL 日志。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading


class Telemetry:
    def __init__(self, workspace, *, mode="auto", plugin=None, trace_sqlite_path=None):
        if mode not in ("auto", "log"):
            raise ValueError("telemetry mode 必须是 auto 或 log")
        self.path = Path(workspace) / "telemetry" / "events.jsonl"
        self.mode = mode
        self._lock = threading.Lock()
        self._plugin = plugin
        self._owns_plugin = False
        self._closed = False
        self._sent = self._logged = self._dropped = 0
        self._backend = "log"
        self._last_error = None
        if mode == "auto" and plugin is None:
            try:
                from .plugins.fuju_trace import FujuTracePlugin
                self._plugin = FujuTracePlugin(trace_sqlite_path or self.path.parent / "trace.sqlite")
                self._owns_plugin = True
            except ImportError:
                self._last_error = "trace_sdk_unavailable"
            except Exception as err:
                self._last_error = type(err).__name__

    def emit(self, event_type, *, experiment_id, agent_id, status, used_calls=None):
        # 仅允许这些字段进入观测；提示词、题目、答案、输出和验收明细一律不传。
        event = {"type": event_type, "timestamp": datetime.now(timezone.utc).isoformat(),
                 "experimentId": experiment_id, "agentId": agent_id, "status": status}
        if used_calls is not None:
            event["usedCalls"] = used_calls
        with self._lock:
            if self._closed:
                self._dropped += 1
                self._last_error = "telemetry_closed"
                return
            if self.mode == "auto" and self._plugin is not None:
                try:
                    # 插件只能看到副本；它改写事件也不能污染白名单回退日志。
                    if self._plugin.emit(dict(event)):
                        self._backend = "fuju-trace"
                        self._last_error = None
                        self._sent += 1
                        return
                except Exception as err:
                    self._last_error = type(err).__name__
                else:
                    self._last_error = "trace_unavailable"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
            except OSError as err:
                self._dropped += 1
                self._last_error = type(err).__name__
                return
            self._logged += 1
            self._backend = "log"
            if self.mode == "log":
                self._last_error = None

    def health(self):
        with self._lock:
            return {"backend": self._backend, "mode": self.mode,
                    "degraded": self._last_error is not None,
                    "lastErrorType": self._last_error, "logPath": str(self.path) if self._backend == "log" else None,
                    "sentEvents": self._sent, "loggedEvents": self._logged,
                    "droppedEvents": self._dropped, "closed": self._closed}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_plugin:
                try:
                    self._plugin.close()
                except Exception as err:
                    self._last_error = type(err).__name__
