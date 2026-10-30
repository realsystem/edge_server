"""SSH connection and remote command execution."""

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


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
        port: int = 22,
        timeout: int = 10,
        retries: int = 3,
        key_file: str | None = None,
        strict_host_key_checking: str = "accept-new",
    ):
        self.host = host
        self.user = user
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.key_file = key_file
        self.strict_host_key_checking = strict_host_key_checking

    def _ssh_args(self) -> list[str]:
        """Build SSH command arguments."""
        args = [
            "ssh",
            "-o",
            "ConnectTimeout=" + str(self.timeout),
            "-o",
            "StrictHostKeyChecking=" + self.strict_host_key_checking,
            "-o",
            "BatchMode=yes",
            "-p",
            str(self.port),
        ]
        if self.key_file:
            args.extend(["-i", self.key_file])
        args.append(f"{self.user}@{self.host}")
        return args

    def _scp_args(self) -> list[str]:
        """Build SCP command arguments."""
        args = [
            "scp",
            "-o",
            "ConnectTimeout=" + str(self.timeout),
            "-o",
            "StrictHostKeyChecking=" + self.strict_host_key_checking,
            "-o",
            "BatchMode=yes",
            "-P",
            str(self.port),
        ]
        if self.key_file:
            args.extend(["-i", self.key_file])
        return args

    def run(
        self,
        command: str,
        timeout: int | None = None,
        sudo: bool = False,
        input: str | None = None,
    ) -> SSHResult:
        """Execute a remote command.

        ``input`` is written to the remote command's stdin. When it is not given,
        stdin is /dev/null so the remote side can never block on (or steal) the
        local terminal.
        """
        if sudo:
            command = f"sudo {command}"

        args = self._ssh_args() + [command]
        cmd_timeout = timeout or self.timeout * 10

        try:
            result = subprocess.run(
                args,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
            )
            return SSHResult(result.returncode, result.stdout.strip(), result.stderr.strip())
        except subprocess.TimeoutExpired:
            return SSHResult(-1, "", "Command timed out")
        except Exception as e:
            return SSHResult(-1, "", str(e))

    def run_with_progress(
        self,
        command: str,
        timeout: int | None = None,
        sudo: bool = False,
        on_progress: Callable[[float], None] | None = None,
    ) -> SSHResult:
        """Execute a remote command with progress callback (elapsed time)."""
        if sudo:
            command = f"sudo {command}"

        args = self._ssh_args() + [command]
        cmd_timeout = timeout or self.timeout * 10

        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            return SSHResult(-1, "", str(e))

        # communicate() keeps draining stdout/stderr while we wait. Polling with
        # process.poll() alone deadlocks once the remote output fills the pipe
        # buffer (~64 KiB), which apt-heavy setup scripts easily exceed.
        start_time = time.monotonic()
        while True:
            elapsed = time.monotonic() - start_time
            if cmd_timeout and elapsed > cmd_timeout:
                process.kill()
                stdout, _ = process.communicate()
                return SSHResult(-1, (stdout or "").strip(), "Command timed out")
            if on_progress:
                on_progress(elapsed)
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue
            except Exception as e:
                process.kill()
                return SSHResult(-1, "", str(e))

        return SSHResult(process.returncode, (stdout or "").strip(), (stderr or "").strip())

    def test_connection(self) -> bool:
        """Test if SSH connection works, retrying up to ``retries`` times."""
        attempts = max(1, self.retries)
        for attempt in range(attempts):
            if self.run("true").success:
                return True
            if attempt < attempts - 1:
                time.sleep(1)
        return False

    def test_sudo(self) -> bool:
        """Test if sudo works without password."""
        result = self.run("sudo -n true")
        return result.success

    def get_os_info(self) -> str | None:
        """Get OS information."""
        result = self.run("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'")
        return result.stdout if result.success else None

    def path_exists(self, path: str) -> bool:
        """Check if a path exists on remote."""
        result = self.run(f"[ -e {shlex.quote(path)} ]")
        return result.success

    def mkdir(self, path: str, sudo: bool = False) -> bool:
        """Create directory on remote."""
        result = self.run(f"mkdir -p {shlex.quote(path)}", sudo=sudo)
        return result.success

    def copy_files(self, local_paths: list[Path], remote_dir: str) -> bool:
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
        elapsed = 0.0
        while elapsed < timeout:
            if self.run("true").success:
                return True, elapsed
            time.sleep(interval)
            elapsed = time.monotonic() - start
        return False, elapsed

    def run_script(
        self,
        script_path: str,
        env: dict[str, str] | None = None,
        sudo: bool = True,
        timeout: int = 600,
        on_progress: Callable[[float], None] | None = None,
    ) -> SSHResult:
        """Run a script on remote host."""
        env_str = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
        sudo_prefix = "sudo -E " if sudo else ""
        script = shlex.quote(script_path)
        prefix = f"{env_str} " if env_str else ""
        command = f'cd "$(dirname {script})" && {prefix}{sudo_prefix}{script} 2>&1'
        if on_progress:
            return self.run_with_progress(command, timeout=timeout, on_progress=on_progress)
        return self.run(command, timeout=timeout)

    def get_docker_containers(self, filter_name: str | None = None) -> list[dict]:
        """Get list of running docker containers."""
        cmd = "docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'"
        if filter_name:
            cmd += f" | grep -F -- {shlex.quote(filter_name)}"
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
