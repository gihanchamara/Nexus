from __future__ import annotations
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from ..ssh_client import SSHClient


def execute_command(ssh: "SSHClient", command: str) -> dict[str, Any]:
    """Run an arbitrary shell command on the remote server."""
    return ssh.execute(command)
