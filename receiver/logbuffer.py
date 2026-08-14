"""In-memory ring buffer of recent log records, for the admin Logs panel.

Attached as a logging handler so everything logged by the app (webhook
events, ARI originate results, errors) is queryable over HTTP without
SSHing in to tail container logs. `docker compose logs` still gets
everything too.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

_MAX = 500


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = _MAX) -> None:
        super().__init__()
        self._records: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # never let logging blow up a request
            msg = record.msg
        with self._lock:
            self._seq += 1
            self._records.append(
                {
                    "seq": self._seq,
                    "time": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": msg,
                }
            )

    def records(self, since_seq: int = 0, level: str | None = None) -> list[dict]:
        with self._lock:
            items = [r for r in self._records if r["seq"] > since_seq]
        if level and level != "ALL":
            wanted = logging.getLevelName(level)
            items = [r for r in items if logging.getLevelName(r["level"]) >= wanted]
        return items

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for r in self._records:
                out[r["level"]] = out.get(r["level"], 0) + 1
        return out


buffer = RingBufferHandler()
