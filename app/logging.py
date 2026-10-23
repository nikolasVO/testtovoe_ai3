import json
import logging
import os
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        value = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                 "logger": record.name, "event": record.getMessage(), "pid": os.getpid()}
        for key in ("request_id", "task_id", "attempt_id", "provider", "status_code", "duration_ms"):
            if hasattr(record, key):
                value[key] = str(getattr(record, key))
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        return json.dumps(value, ensure_ascii=False)


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
