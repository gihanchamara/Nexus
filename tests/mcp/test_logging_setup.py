"""TDD tests for logging_setup — rotating file handler support.

Each test is isolated: we tear down any file handlers added during the test
so they don't bleed into other tests (logging state is global in Python).
"""

from __future__ import annotations

import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_file_handlers():
    """Remove rotating file handlers added during each test."""
    root = logging.getLogger()
    before = set(id(h) for h in root.handlers)
    yield
    for h in list(root.handlers):
        if id(h) not in before and isinstance(h, TimedRotatingFileHandler):
            h.close()
            root.removeHandler(h)


# ── RED tests: all should fail before implementation ─────────────────────────

class TestRotatingFileHandlerCreated:
    def test_adds_timed_rotating_file_handler_when_log_dir_given(self, tmp_path):
        """configure_logging adds a TimedRotatingFileHandler when log_dir is given."""
        from mcp.ssh_server.logging_setup import configure_logging

        configure_logging(log_dir=str(tmp_path))

        root = logging.getLogger()
        rotating = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert len(rotating) == 1

    def test_no_file_handler_when_log_dir_omitted(self):
        """configure_logging without log_dir does NOT add a file handler."""
        from mcp.ssh_server.logging_setup import configure_logging

        configure_logging()  # no log_dir

        root = logging.getLogger()
        rotating = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
        assert len(rotating) == 0


class TestRotationConfig:
    def test_rotates_at_midnight(self, tmp_path):
        """Handler rotates at midnight (daily rotation)."""
        from mcp.ssh_server.logging_setup import configure_logging

        configure_logging(log_dir=str(tmp_path))

        root = logging.getLogger()
        handler = next(h for h in root.handlers if isinstance(h, TimedRotatingFileHandler))
        assert handler.when.upper() == "MIDNIGHT"

    def test_retains_30_backup_files(self, tmp_path):
        """Handler keeps 30 days of backups."""
        from mcp.ssh_server.logging_setup import configure_logging

        configure_logging(log_dir=str(tmp_path))

        root = logging.getLogger()
        handler = next(h for h in root.handlers if isinstance(h, TimedRotatingFileHandler))
        assert handler.backupCount == 30

    def test_log_filename_is_ssh_mcp_log(self, tmp_path):
        """Log file is named ssh_mcp.log inside the given log_dir."""
        from mcp.ssh_server.logging_setup import configure_logging

        configure_logging(log_dir=str(tmp_path))

        root = logging.getLogger()
        handler = next(h for h in root.handlers if isinstance(h, TimedRotatingFileHandler))
        assert Path(handler.baseFilename).name == "ssh_mcp.log"
        assert Path(handler.baseFilename).parent == tmp_path


class TestLogDirCreation:
    def test_creates_log_dir_if_it_does_not_exist(self, tmp_path):
        """configure_logging creates nested log directories automatically."""
        log_dir = tmp_path / "nested" / "logs"
        assert not log_dir.exists()

        from mcp.ssh_server.logging_setup import configure_logging

        configure_logging(log_dir=str(log_dir))

        assert log_dir.exists()


class TestFileOutput:
    def test_log_entry_appears_in_file(self, tmp_path):
        """A structlog message written after configure_logging appears in the log file."""
        from mcp.ssh_server.logging_setup import configure_logging, get_logger

        configure_logging(log_dir=str(tmp_path))
        log = get_logger("test.audit")
        log.info("ssh_command", command="ls /opt/nexus", host="192.168.1.67")

        # Flush handlers
        for h in logging.getLogger().handlers:
            h.flush()

        log_file = tmp_path / "ssh_mcp.log"
        assert log_file.exists(), "Log file was not created"
        content = log_file.read_text()
        assert "ssh_command" in content
        assert "ls /opt/nexus" in content

    def test_log_entries_are_valid_json(self, tmp_path):
        """Each line written to the log file is valid JSON."""
        from mcp.ssh_server.logging_setup import configure_logging, get_logger

        configure_logging(log_dir=str(tmp_path))
        log = get_logger("test.audit")
        log.info("ssh_command_result", command="hostname", exit_code=0, success=True)

        for h in logging.getLogger().handlers:
            h.flush()

        log_file = tmp_path / "ssh_mcp.log"
        for line in log_file.read_text().splitlines():
            if line.strip():
                parsed = json.loads(line)  # raises if invalid JSON
                assert "event" in parsed or "ssh_command_result" in str(parsed)
