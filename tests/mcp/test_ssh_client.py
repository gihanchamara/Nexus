import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_config():
    from mcp.ssh_server.config import SSHConfig
    cfg = SSHConfig.__new__(SSHConfig)
    cfg.host = "10.0.0.1"
    cfg.port = 22
    cfg.user = "ubuntu"
    cfg.keychain_service = "nexus-ssh"
    cfg.keychain_username = "nexus-deploy"
    return cfg


@patch("mcp.ssh_server.ssh_client.keyring")
@patch("mcp.ssh_server.ssh_client.paramiko.SSHClient")
def test_connect_uses_keychain_password(mock_ssh_cls, mock_keyring, mock_config):
    mock_keyring.get_password.return_value = "secret123"
    mock_ssh = MagicMock()
    mock_ssh_cls.return_value = mock_ssh

    from mcp.ssh_server.ssh_client import SSHClient
    client = SSHClient(mock_config)
    client.connect()

    mock_keyring.get_password.assert_called_once_with("nexus-ssh", "nexus-deploy")
    mock_ssh.connect.assert_called_once_with(
        "10.0.0.1", port=22, username="ubuntu", password="secret123", timeout=10,
        allow_agent=False, look_for_keys=False,
    )


@patch("mcp.ssh_server.ssh_client.keyring")
@patch("mcp.ssh_server.ssh_client.paramiko.SSHClient")
def test_raises_if_no_password_in_keychain(mock_ssh_cls, mock_keyring, mock_config):
    mock_keyring.get_password.return_value = None

    from mcp.ssh_server.ssh_client import SSHClient
    client = SSHClient(mock_config)

    with pytest.raises(RuntimeError, match="No SSH password found in Keychain"):
        client.connect()


@patch("mcp.ssh_server.ssh_client.keyring")
@patch("mcp.ssh_server.ssh_client.paramiko.SSHClient")
def test_execute_command_returns_stdout(mock_ssh_cls, mock_keyring, mock_config):
    mock_keyring.get_password.return_value = "secret"
    mock_ssh = MagicMock()
    mock_ssh_cls.return_value = mock_ssh

    stdout_mock = MagicMock()
    stdout_mock.read.return_value = b"hello world\n"
    stdout_mock.channel.recv_exit_status.return_value = 0
    stderr_mock = MagicMock()
    stderr_mock.read.return_value = b""
    mock_ssh.exec_command.return_value = (MagicMock(), stdout_mock, stderr_mock)

    from mcp.ssh_server.ssh_client import SSHClient
    client = SSHClient(mock_config)
    client._client = mock_ssh  # inject already-connected client

    result = client.execute("echo hello")

    assert result["stdout"] == "hello world\n"
    assert result["exit_code"] == 0
