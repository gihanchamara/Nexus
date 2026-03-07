"""Structured logging configuration for the SSH MCP server.

Uses structlog (already a Nexus dependency) with JSON output so logs
can be ingested by the same log pipeline as the rest of the platform.
"""

from __future__ import annotations

import logging
import structlog


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog for the MCP server process."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )
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
