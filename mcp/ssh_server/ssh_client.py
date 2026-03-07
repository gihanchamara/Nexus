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
            allow_agent=False,    # don't try SSH agent (password-only)
            look_for_keys=False,  # don't scan ~/.ssh/* key files
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

    def _sudo_password(self) -> str:
        """Retrieve sudo password from Keychain.

        Looks up service='nexus-ssh-sudo', username=<SSH user> first.
        Falls back to the SSH login password if no sudo-specific entry exists.
        """
        sudo_pwd = keyring.get_password("nexus-ssh-sudo", self._config.user)
        if sudo_pwd is not None:
            return sudo_pwd
        # Fall back to SSH login password (common on personal servers)
        ssh_pwd = keyring.get_password(
            self._config.keychain_service, self._config.keychain_username
        )
        if ssh_pwd is None:
            raise RuntimeError("No sudo or SSH password found in Keychain")
        return ssh_pwd

    def execute(self, command: str) -> dict[str, Any]:
        """Run a shell command. Logs command, exit_code, and truncated output."""
        log.info("ssh_command", command=command)
        result = self._run_command(command)
        log.info(
            "ssh_command_result",
            command=command,
            exit_code=result["exit_code"],
            stdout_preview=result["stdout"][:200],
            stderr_preview=result["stderr"][:200],
            success=(result["exit_code"] == 0),
        )
        return result

    def execute_sudo(self, command: str) -> dict[str, Any]:
        """Run a command with sudo, piping password via stdin (-S flag).

        Password is retrieved from Keychain (service='nexus-ssh-sudo', username=<SSH user>).
        Falls back to the SSH login password if no dedicated sudo entry exists.
        """
        sudo_pwd = self._sudo_password()
        # -S reads password from stdin; -p '' suppresses the prompt string
        wrapped = f"echo {sudo_pwd!r} | sudo -S -p '' {command}"
        log.info("ssh_sudo_command", command=command)
        result = self._run_command(wrapped)
        log.info(
            "ssh_sudo_result",
            command=command,
            exit_code=result["exit_code"],
            success=(result["exit_code"] == 0),
        )
        return result

    def _run_command(self, command: str) -> dict[str, Any]:
        """Internal: execute a raw command string and return output dict."""
        client = self._ensure_connected()
        _, stdout, stderr = client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        return {
            "stdout": stdout.read().decode("utf-8", errors="replace"),
            "stderr": stderr.read().decode("utf-8", errors="replace"),
            "exit_code": exit_code,
        }

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
