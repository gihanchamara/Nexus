from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class SSHConfig:
    """SSH connection parameters loaded from environment variables.

    The SSH password is NOT stored here — it is retrieved at runtime
    from macOS Keychain via the keyring library.
    """

    host: str = field(default_factory=lambda: os.environ["SSH_HOST"])
    port: int = field(default_factory=lambda: int(os.environ.get("SSH_PORT", "22")))
    user: str = field(default_factory=lambda: os.environ["SSH_USER"])
    keychain_service: str = field(
        default_factory=lambda: os.environ.get("SSH_KEYCHAIN_SERVICE", "nexus-ssh")
    )
    keychain_username: str = field(
        default_factory=lambda: os.environ.get("SSH_KEYCHAIN_USERNAME", "nexus-deploy")
    )
