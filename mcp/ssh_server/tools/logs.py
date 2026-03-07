from __future__ import annotations
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from ..ssh_client import SSHClient


def tail_log(ssh: "SSHClient", remote_path: str, lines: int = 100) -> dict[str, Any]:
    """Fetch the last N lines of a log file from the remote server."""
    return ssh.execute(f"tail -n {lines} {remote_path}")
