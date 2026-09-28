"""通过进程内存储写入最少量状态；不依赖 Trace HTTP 服务。"""
from __future__ import annotations

from pathlib import Path
import threading


class FujuTracePlugin:
    def __init__(self, sqlite_path=None, *, db=None, tenant_id=1, node_id=None):
        from fuju_trace import CollectingExporter, DbExporter, Tracer, connect
        if (sqlite_path is None) == (db is None):
            raise ValueError("提供 sqlite_path 或 db，不能同时提供")
        self._connect = connect
        self._db_exporter_type = DbExporter
        self._events = CollectingExporter()
        self._tracer = Tracer(exporter=self._events, node_id=node_id, agent_name="fuju-rsi")
        self._path = Path(sqlite_path) if sqlite_path is not None else None
        self._db = db
        self._owns_db = db is None
        self._tenant_id = tenant_id
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event):
        with self._lock:
            if self._closed:
                raise RuntimeError("Trace 插件已关闭")
            if self._db is None:
                # 延迟打开：工作区拿到独占锁、真正开始实验后才创建观测数据库。
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._db = self._connect(sqlite_path=self._path, tenant_id=self._tenant_id,
                                         initialize=True)
            self._events.events.clear()
            try:
                with self._tracer.trace("fuju-rsi " + event["type"], tenant_id=self._tenant_id) as trace:
                    with trace.span("experiment " + event["type"]) as span:
                        for key in ("experimentId", "agentId", "status", "usedCalls"):
                            if key in event:
                                span.set_attribute(key, event[key])
                # start/end 一起提交，避免每条观测两次事务或失败后留下半个 span。
                self._db_exporter_type(self._db, tenant_id=self._tenant_id).export_batch(self._events.events)
                return True
            finally:
                self._events.events.clear()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._tracer.close()
            # 注入的数据库属于调用方；默认 SQLite 连接才由插件负责关闭。
            if self._owns_db and self._db is not None:
                self._db.close()
