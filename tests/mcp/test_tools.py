import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_ssh():
    ssh = MagicMock()
    ssh.execute.return_value = {"stdout": "ok\n", "stderr": "", "exit_code": 0}
    return ssh


def test_execute_command_returns_result(mock_ssh):
    from mcp.ssh_server.tools.shell import execute_command
    result = execute_command(mock_ssh, "ls -la")
    mock_ssh.execute.assert_called_once_with("ls -la")
    assert result["stdout"] == "ok\n"
    assert result["exit_code"] == 0
