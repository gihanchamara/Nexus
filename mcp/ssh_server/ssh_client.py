from __future__ import annotations

from typing import Any

import keyring
import paramiko

from .config import SSHConfig
from .logging_setup import get_logger

log = get_logger(__name__)


class SSHClient:
    """Lazy-connecting, auto-reconnecting SSH/SFTP client.

    Password is retrieved from macOS Keychain on first connect.
    Every command executed is logged with structlog (command, exit_code, truncated output).
    """

    def __init__(self, config: SSHConfig) -> None:
        self._config = config
        self._client: paramiko.SSHClient | None = None

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        """Open SSH connection, retrieving password from macOS Keychain."""
        password = keyring.get_password(
            self._config.keychain_service, self._config.keychain_username
        )
        if password is None:
            raise RuntimeError(
                f"No SSH password found in Keychain for service="
                f"'{self._config.keychain_service}' "
                f"username='{self._config.keychain_username}'. "
                f"Run: keyring set {self._config.keychain_service} "
                f"{self._config.keychain_username}"
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self._config.host,
            port=self._config.port,
            username=self._config.user,
            password=password,
            timeout=10,
        )
        self._client = client
        log.info(
            "ssh_connected",
            user=self._config.user,
            host=self._config.host,
            port=self._config.port,
        )

    def _ensure_connected(self) -> paramiko.SSHClient:
        """Return connected client, reconnecting if the session dropped."""
        if self._client is None:
            self.connect()
            return self._client  # type: ignore[return-value]

        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            log.warning("ssh_reconnecting", reason="transport_inactive")
            self.connect()

        return self._client  # type: ignore[return-value]

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            log.info("ssh_disconnected")

    # ── Shell ─────────────────────────────────────────────────────────────

    def execute(self, command: str) -> dict[str, Any]:
        """Run a shell command. Logs command, exit_code, and truncated output."""
        log.info("ssh_command", command=command)
        client = self._ensure_connected()
        _, stdout, stderr = client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        stdout_text = stdout.read().decode("utf-8", errors="replace")
        stderr_text = stderr.read().decode("utf-8", errors="replace")

        log.info(
            "ssh_command_result",
            command=command,
            exit_code=exit_code,
            stdout_preview=stdout_text[:200],
            stderr_preview=stderr_text[:200],
            success=(exit_code == 0),
        )
        return {"stdout": stdout_text, "stderr": stderr_text, "exit_code": exit_code}

    # ── SFTP ──────────────────────────────────────────────────────────────

    def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a local file to the remote server via SFTP. Logs the transfer."""
        log.info("sftp_upload", local_path=local_path, remote_path=remote_path)
        client = self._ensure_connected()
        with client.open_sftp() as sftp:
            sftp.put(local_path, remote_path)
        log.info("sftp_upload_done", local_path=local_path, remote_path=remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a file from the remote server via SFTP. Logs the transfer."""
        log.info("sftp_download", remote_path=remote_path, local_path=local_path)
        client = self._ensure_connected()
        with client.open_sftp() as sftp:
            sftp.get(remote_path, local_path)
        log.info("sftp_download_done", remote_path=remote_path, local_path=local_path)
