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


def test_upload_file_calls_ssh_upload(mock_ssh):
    from mcp.ssh_server.tools.sftp import upload_file
    result = upload_file(mock_ssh, "/local/file.txt", "/remote/file.txt")
    mock_ssh.upload.assert_called_once_with("/local/file.txt", "/remote/file.txt")
    assert result["success"] is True


def test_download_file_calls_ssh_download(mock_ssh):
    from mcp.ssh_server.tools.sftp import download_file
    result = download_file(mock_ssh, "/remote/file.txt", "/local/file.txt")
    mock_ssh.download.assert_called_once_with("/remote/file.txt", "/local/file.txt")
    assert result["success"] is True


def test_upload_file_returns_error_on_exception(mock_ssh):
    mock_ssh.upload.side_effect = IOError("file not found")
    from mcp.ssh_server.tools.sftp import upload_file
    result = upload_file(mock_ssh, "/local/missing.txt", "/remote/x.txt")
    assert result["success"] is False
    assert "file not found" in result["error"]


def test_docker_compose_up(mock_ssh):
    from mcp.ssh_server.tools.docker import docker_compose_up
    result = docker_compose_up(mock_ssh, working_dir="/opt/nexus")
    mock_ssh.execute.assert_called_once_with(
        "cd /opt/nexus && docker compose up -d"
    )
    assert result["exit_code"] == 0


def test_docker_compose_down(mock_ssh):
    from mcp.ssh_server.tools.docker import docker_compose_down
    result = docker_compose_down(mock_ssh, working_dir="/opt/nexus")
    mock_ssh.execute.assert_called_once_with(
        "cd /opt/nexus && docker compose down"
    )


def test_docker_compose_ps(mock_ssh):
    mock_ssh.execute.return_value = {"stdout": "NAME   STATUS\nnexus  Up\n", "stderr": "", "exit_code": 0}
    from mcp.ssh_server.tools.docker import docker_compose_ps
    result = docker_compose_ps(mock_ssh, working_dir="/opt/nexus")
    assert "nexus" in result["stdout"]
