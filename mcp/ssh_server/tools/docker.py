from __future__ import annotations
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from ..ssh_client import SSHClient

_DEFAULT_DIR = "/opt/nexus"


def docker_compose_up(ssh: "SSHClient", working_dir: str = _DEFAULT_DIR, detach: bool = True) -> dict[str, Any]:
    """Start the Nexus Docker Compose stack on the remote server."""
    flag = "-d" if detach else ""
    return ssh.execute(f"cd {working_dir} && docker compose up {flag}".strip())


def docker_compose_down(ssh: "SSHClient", working_dir: str = _DEFAULT_DIR) -> dict[str, Any]:
    """Stop and remove the Nexus Docker Compose stack."""
    return ssh.execute(f"cd {working_dir} && docker compose down")


def docker_compose_ps(ssh: "SSHClient", working_dir: str = _DEFAULT_DIR) -> dict[str, Any]:
    """List running containers in the Nexus Docker Compose stack."""
    return ssh.execute(f"cd {working_dir} && docker compose ps")
