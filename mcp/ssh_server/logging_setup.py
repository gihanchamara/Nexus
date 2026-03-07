"""Structured logging configuration for the SSH MCP server.

Uses structlog (already a Nexus dependency) with JSON output so logs
can be ingested by the same log pipeline as the rest of the platform.

When `log_dir` is supplied, a TimedRotatingFileHandler is added that
writes one JSON line per entry to `<log_dir>/ssh_mcp.log`, rotating at
midnight UTC and keeping 30 days of history.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog

_LOG_FILENAME = "ssh_mcp.log"
_BACKUP_COUNT = 30


def configure_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    """Configure structlog for the MCP server process.

    Args:
        level:   Log level string (default "INFO").
        log_dir: If given, also write JSON logs to a daily-rotating file
                 ``<log_dir>/ssh_mcp.log``.  The directory is created if it
                 does not already exist.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Ensure a stream handler is in place (basicConfig is a no-op if
    # the root logger already has handlers, so be explicit).
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(message)s", level=log_level)
    else:
        root.setLevel(log_level)

    # ── Rotating file handler ─────────────────────────────────────────────
    if log_dir is not None:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        file_handler = TimedRotatingFileHandler(
            filename=str(log_path / _LOG_FILENAME),
            when="midnight",
            interval=1,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        file_handler.setLevel(log_level)
        root.addHandler(file_handler)

    # ── structlog pipeline ────────────────────────────────────────────────
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
