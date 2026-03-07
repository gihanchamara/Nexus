# Nexus SSH MCP Server

Gives Claude the ability to SSH into the remote Nexus deployment server to deploy and manage the trading platform.

## Tools

| Tool | Description |
|------|-------------|
| `run_command` | Run any shell command on the remote server |
| `upload` | Upload a local file via SFTP |
| `download` | Download a remote file via SFTP |
| `compose_up` | Start the Docker Compose stack |
| `compose_down` | Stop the Docker Compose stack |
| `compose_ps` | List container status |
| `read_log` | Tail a remote log file |

## Logging

Every SSH command is logged via `structlog` (JSON format) with:
- `ssh_command` — command text before execution
- `ssh_command_result` — exit_code, success flag, stdout/stderr preview (first 200 chars)
- `sftp_upload` / `sftp_download` — SFTP transfer paths
- `ssh_connected` / `ssh_reconnecting` / `ssh_disconnected` — connection lifecycle

## Setup

### 1. Install dependencies

```bash
cd Nexus
pip install -e ".[mcp]"
```

### 2. Configure connection (no secrets here)

Copy `.env.example` to `.env` and fill in:

```dotenv
SSH_HOST=your.server.ip.or.hostname
SSH_PORT=22
SSH_USER=ubuntu
SSH_KEYCHAIN_SERVICE=nexus-ssh
SSH_KEYCHAIN_USERNAME=nexus-deploy
```

### 3. Store SSH password in macOS Keychain (one-time)

```bash
keyring set nexus-ssh nexus-deploy
# Enter your SSH password when prompted
```

To verify:
```bash
python -c "import keyring; print(keyring.get_password('nexus-ssh', 'nexus-deploy'))"
```

### 4. Register with Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "nexus-ssh": {
      "command": "python",
      "args": ["-m", "mcp.ssh_server.server"],
      "cwd": "/Users/gihanchamara/projects/Trading/nexus/Nexus"
    }
  }
}
```

Restart Claude Code.

## Security

- SSH password stored in macOS Keychain — never written to disk or env files
- SSH user should have minimum permissions needed (not root if avoidable)
- Keep `.env` out of git (already in `.gitignore`)
