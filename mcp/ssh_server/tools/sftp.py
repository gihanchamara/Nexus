from __future__ import annotations
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from ..ssh_client import SSHClient


def upload_file(ssh: "SSHClient", local_path: str, remote_path: str) -> dict[str, Any]:
    """Upload a local file to the remote server via SFTP."""
    try:
        ssh.upload(local_path, remote_path)
        return {"success": True, "local_path": local_path, "remote_path": remote_path}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def download_file(ssh: "SSHClient", remote_path: str, local_path: str) -> dict[str, Any]:
    """Download a file from the remote server via SFTP."""
    try:
        ssh.download(remote_path, local_path)
        return {"success": True, "remote_path": remote_path, "local_path": local_path}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
