import os
import pytest
from unittest.mock import patch


def test_ssh_config_reads_env_vars():
    env = {
        "SSH_HOST": "10.0.0.1",
        "SSH_PORT": "2222",
        "SSH_USER": "admin",
        "SSH_KEYCHAIN_SERVICE": "my-svc",
        "SSH_KEYCHAIN_USERNAME": "my-user",
    }
    with patch.dict(os.environ, env, clear=False):
        import importlib
        import mcp.ssh_server.config as cfg_mod
        importlib.reload(cfg_mod)
        cfg = cfg_mod.SSHConfig()

    assert cfg.host == "10.0.0.1"
    assert cfg.port == 2222
    assert cfg.user == "admin"
    assert cfg.keychain_service == "my-svc"
    assert cfg.keychain_username == "my-user"


def test_ssh_config_default_port():
    env = {
        "SSH_HOST": "10.0.0.1",
        "SSH_USER": "ubuntu",
        "SSH_KEYCHAIN_SERVICE": "nexus-ssh",
        "SSH_KEYCHAIN_USERNAME": "nexus-deploy",
    }
    with patch.dict(os.environ, env, clear=False):
        import importlib
        import mcp.ssh_server.config as cfg_mod
        importlib.reload(cfg_mod)
        cfg = cfg_mod.SSHConfig()

    assert cfg.port == 22
