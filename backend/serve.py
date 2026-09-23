"""Unattended entry point.

Used by the autostart task. Differs from running uvicorn by hand in three ways
that matter when nobody is watching:

* **Logs to a file.** Launched via ``pythonw.exe`` there is no console, so
  anything written to stdout is lost. Without this, a crash leaves no trace.
* **Refuses to double-start.** If the port is already serving, it exits 0
  rather than dying with "address already in use" — which the scheduler would
  otherwise treat as a failure and retry forever.
* **Never exits silently on a startup error.** The traceback goes to the log
  before the process ends, so there is something to read afterwards.
"""

from __future__ import annotations

import logging
import logging.config
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

def _bind() -> tuple[str, int]:
    """Read the bind address from configuration, defaulting to loopback."""
    try:
        from app.config import get_settings

        settings = get_settings()
        return settings.bind_host, settings.bind_port
    except Exception:
        return "127.0.0.1", 8000


HOST, PORT = _bind()
# Health probes always go to loopback: 0.0.0.0 is not a connectable address.
PROBE_HOST = "127.0.0.1"


def _already_serving() -> bool:
    """True when something on this port answers our health endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        if probe.connect_ex((PROBE_HOST, PORT)) != 0:
            return False
    # The port is open — confirm it is actually this app, not something else.
    try:
        with urllib.request.urlopen(f"http://{PROBE_HOST}:{PORT}/api/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _log_config(log_path: Path) -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "file": {
                "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "file",
                "filename": str(log_path),
                "maxBytes": 2_000_000,
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "root": {"handlers": ["file"], "level": "INFO"},
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "WARNING", "propagate": False},
        },
    }


def main() -> int:
    data_dir = BASE_DIR.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "server.log"

    if _already_serving():
        # Exit cleanly: a non-zero exit would make the scheduler retry forever.
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("serve.py: already serving on this port, nothing to do\n")
        return 0

    logging.config.dictConfig(_log_config(log_path))
    logger = logging.getLogger("serve")

    try:
        import uvicorn

        logger.info("starting on http://%s:%d", HOST, PORT)
        uvicorn.run(
            "app.main:app",
            host=HOST,
            port=PORT,
            log_config=_log_config(log_path),
            access_log=False,
        )
        return 0
    except Exception:
        # Without this the traceback vanishes with the console-less process.
        logger.exception("failed to start")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
