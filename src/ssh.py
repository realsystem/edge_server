"""SSH connection and remote command execution."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class SSHResult:
    """Result of an SSH command."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


class SSHClient:
    """SSH client for remote command execution."""

    def __init__(
        self,
        host: str,
        user: str,
        timeout: int = 10,
        retries: int = 3,
        key_file: Optional[str] = None,
        strict_host_key_checking: str = "accept-new",
    ):
        self.host = host
        self.user = user
        self.timeout = timeout
        self.retries = retries
        self.key_file = key_file
        self.strict_host_key_checking = strict_host_key_checking

    def _ssh_args(self) -> List[str]:
        """Build SSH command arguments."""
        args = [
            "ssh",
            "-o", "ConnectTimeout=" + str(self.timeout),
            "-o", "StrictHostKeyChecking=" + self.strict_host_key_checking,
            "-o", "BatchMode=yes",
        ]
        if self.key_file:
            args.extend(["-i", self.key_file])
        args.append(f"{self.user}@{self.host}")
        return args

    def _scp_args(self) -> List[str]:
        """Build SCP command arguments."""
        args = [
            "scp",
            "-o", "ConnectTimeout=" + str(self.timeout),
            "-o", "StrictHostKeyChecking=" + self.strict_host_key_checking,
            "-o", "BatchMode=yes",
        ]
        if self.key_file:
            args.extend(["-i", self.key_file])
        return args

    def run(self, command: str, timeout: Optional[int] = None, sudo: bool = False) -> SSHResult:
        """Execute a remote command."""
        if sudo:
            command = f"sudo {command}"

        args = self._ssh_args() + [command]
        cmd_timeout = timeout or self.timeout * 10

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
            )
            return SSHResult(result.returncode, result.stdout.strip(), result.stderr.strip())
        except subprocess.TimeoutExpired:
            return SSHResult(-1, "", "Command timed out")
        except Exception as e:
            return SSHResult(-1, "", str(e))

    def test_connection(self) -> bool:
        """Test if SSH connection works."""
        result = self.run("true")
        return result.success

    def test_sudo(self) -> bool:
        """Test if sudo works without password."""
        result = self.run("sudo -n true")
        return result.success

    def get_os_info(self) -> Optional[str]:
        """Get OS information."""
        result = self.run("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'")
        return result.stdout if result.success else None

    def path_exists(self, path: str) -> bool:
        """Check if a path exists on remote."""
        result = self.run(f"[ -e {path} ]")
        return result.success

    def mkdir(self, path: str, sudo: bool = False) -> bool:
        """Create directory on remote."""
        result = self.run(f"mkdir -p {path}", sudo=sudo)
        return result.success

    def copy_files(self, local_paths: List[Path], remote_dir: str) -> bool:
        """Copy files to remote host."""
        args = self._scp_args() + ["-r"]
        args.extend(str(p) for p in local_paths)
        args.append(f"{self.user}@{self.host}:{remote_dir}/")

        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            return result.returncode == 0
        except Exception:
            return False

    def copy_file(self, local_path: Path, remote_path: str) -> bool:
        """Copy a single file to remote host."""
        args = self._scp_args()
        args.append(str(local_path))
        args.append(f"{self.user}@{self.host}:{remote_path}")

        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            return result.returncode == 0
        except Exception:
            return False

    def reboot(self) -> bool:
        """Reboot the remote host."""
        result = self.run("reboot", sudo=True)
        return result.returncode in [0, 255]  # 255 = connection closed (expected)

    def wait_for_host(self, timeout: int = 120, interval: int = 5) -> tuple[bool, float]:
        """Wait for host to come back online after reboot."""
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < timeout:
            if self.test_connection():
                return True, elapsed
            time.sleep(interval)
        return False, elapsed

    def run_script(
        self, script_path: str, env: Optional[dict] = None, sudo: bool = True, timeout: int = 600
    ) -> SSHResult:
        """Run a script on remote host."""
        env_str = " ".join(f"{k}='{v}'" for k, v in (env or {}).items())
        if env_str:
            command = f"cd $(dirname {script_path}) && {env_str} {'sudo -E' if sudo else ''} {script_path}"
        else:
            command = f"cd $(dirname {script_path}) && {'sudo' if sudo else ''} {script_path}"
        return self.run(command, timeout=timeout)

    def get_docker_containers(self, filter_name: Optional[str] = None) -> List[dict]:
        """Get list of running docker containers."""
        cmd = "docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'"
        if filter_name:
            cmd += f" | grep {filter_name}"
        result = self.run(cmd)
        if not result.success:
            return []

        containers = []
        for line in result.stdout.split("\n"):
            if "|" in line:
                parts = line.split("|")
                containers.append(
                    {
                        "name": parts[0],
                        "status": parts[1] if len(parts) > 1 else "",
                        "ports": parts[2] if len(parts) > 2 else "",
                    }
                )
        return containers
