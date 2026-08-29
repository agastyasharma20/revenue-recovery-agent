"""
Structured JSON logging. Every log line is one JSON object with a
`trace_id` field (when supplied via `extra={"trace_id": ...}`), so you can
grep one event's full journey across every layer:

    grep '"trace_id": "<id>"' logs/agent.jsonl

or with jq:

    jq 'select(.trace_id=="<id>")' logs/agent.jsonl
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
            "layer": getattr(record, "layer", None),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str = "revenue_agent", log_path: str = "results/agent.jsonl") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured -- don't stack duplicate handlers
        return logger

    logger.setLevel(logging.INFO)

    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
