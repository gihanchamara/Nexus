"""Nexus SSH MCP Server.

Exposes SSH/SFTP tools for deploying and configuring the Nexus trading
platform on a remote server. Password is retrieved from macOS Keychain —
never stored in files or environment variables.

Usage (Claude Code settings.json):
    {
      "mcpServers": {
        "nexus-ssh": {
          "command": "python",
          "args": ["-m", "mcp.ssh_server.server"],
          "cwd": "/path/to/Nexus"
        }
      }
    }

One-time password setup:
    keyring set nexus-ssh nexus-deploy
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP

from .config import SSHConfig
from .logging_setup import configure_logging, get_logger
from .ssh_client import SSHClient
from .tools.docker import docker_compose_down, docker_compose_ps, docker_compose_up
from .tools.logs import tail_log
from .tools.sftp import download_file, upload_file
from .tools.shell import execute_command

# Load .env from the project root (two levels up from this file: mcp/ssh_server/ -> mcp/ -> Nexus/)
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Configure logging before creating any objects
configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
log = get_logger(__name__)

# Lazy singletons — initialized on first tool call so the server starts even
# without SSH_HOST set (allows Claude Code to load the MCP server before .env is configured).
_ssh: SSHClient | None = None


def _get_ssh() -> SSHClient:
    global _ssh
    if _ssh is None:
        config = SSHConfig()
        _ssh = SSHClient(config)
        log.info("nexus_ssh_mcp_ready", host=config.host, user=config.user)
    return _ssh


log.info("nexus_ssh_mcp_starting")

mcp = FastMCP(
    name="nexus-ssh",
    instructions=(
        "SSH tools for deploying and configuring the Nexus trading platform "
        "on the remote server. Always check exit_code in command results — "
        "non-zero means failure. Use docker_compose_ps to verify services are up."
    ),
)


# ── Tool registrations ────────────────────────────────────────────────────────

@mcp.tool()
def run_command(command: str) -> dict:
    """Run any shell command on the remote Nexus server.

    Args:
        command: Shell command to execute (e.g. 'ls /opt/nexus', 'systemctl status docker').

    Returns:
        stdout, stderr, and exit_code (0 = success).
    """
    return execute_command(_get_ssh(), command)


@mcp.tool()
def upload(local_path: str, remote_path: str) -> dict:
    """Upload a local file to the remote server over SFTP.

    Args:
        local_path: Absolute path on this machine (e.g. '/Users/me/nexus/.env').
        remote_path: Absolute path on the remote server (e.g. '/opt/nexus/.env').
    """
    return upload_file(_get_ssh(), local_path, remote_path)


@mcp.tool()
def download(remote_path: str, local_path: str) -> dict:
    """Download a file from the remote server over SFTP.

    Args:
        remote_path: Absolute path on the remote server.
        local_path: Where to save locally (absolute path).
    """
    return download_file(_get_ssh(), remote_path, local_path)


@mcp.tool()
def compose_up(working_dir: str = "/opt/nexus", detach: bool = True) -> dict:
    """Start the Nexus Docker Compose stack.

    Args:
        working_dir: Directory containing docker-compose.yml on the remote server.
        detach: Run in background (True) or foreground (False).
    """
    return docker_compose_up(_get_ssh(), working_dir, detach)


@mcp.tool()
def compose_down(working_dir: str = "/opt/nexus") -> dict:
    """Stop and remove the Nexus Docker Compose stack."""
    return docker_compose_down(_get_ssh(), working_dir)


@mcp.tool()
def compose_ps(working_dir: str = "/opt/nexus") -> dict:
    """List all containers in the Nexus Docker Compose stack with their status."""
    return docker_compose_ps(_get_ssh(), working_dir)


@mcp.tool()
def read_log(remote_path: str, lines: int = 100) -> dict:
    """Read the last N lines of a log file from the remote server.

    Args:
        remote_path: Absolute path to the log file on the remote server.
        lines: Number of lines to return (default 100).
    """
    return tail_log(_get_ssh(), remote_path, lines)


if __name__ == "__main__":
    mcp.run()
