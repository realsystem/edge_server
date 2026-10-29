"""Service health checks and monitoring."""

import json
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ssh import SSHClient, SSHResult


class HealthStatus(Enum):
    """Service health status."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
    STARTING = "starting"


@dataclass
class ServiceHealth:
    """Health check result for a service."""

    name: str
    status: HealthStatus
    message: str
    details: Optional[dict] = None
    response_time: Optional[float] = None


class ServiceChecker:
    """Check health of various services."""

    def __init__(self, ssh: Optional[SSHClient] = None):
        self.ssh = ssh

    @staticmethod
    def _run_local(args: list[str], timeout: int) -> SSHResult:
        """Run a local command and return it in the same shape as an SSH result."""
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return SSHResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
        except subprocess.TimeoutExpired:
            return SSHResult(-1, "", "Command timed out")
        except Exception as e:
            return SSHResult(-1, "", str(e))

    def check_tcp_port(self, host: str, port: int, timeout: int = 5) -> ServiceHealth:
        """Check if a TCP port is open."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
            return ServiceHealth(
                name=f"tcp:{port}",
                status=HealthStatus.HEALTHY,
                message=f"Port {port} is open",
            )
        except (socket.timeout, TimeoutError):
            return ServiceHealth(
                name=f"tcp:{port}",
                status=HealthStatus.UNHEALTHY,
                message=f"Connection to port {port} timed out",
            )
        except ConnectionRefusedError:
            return ServiceHealth(
                name=f"tcp:{port}",
                status=HealthStatus.UNHEALTHY,
                message=f"Port {port} is closed",
            )
        except Exception as e:
            return ServiceHealth(
                name=f"tcp:{port}",
                status=HealthStatus.UNKNOWN,
                message=str(e),
            )

    def check_http(
        self,
        url: str,
        expected_codes: Optional[list] = None,
        timeout: int = 10,
    ) -> ServiceHealth:
        """Check HTTP endpoint."""
        if expected_codes is None:
            expected_codes = [200]

        try:
            req = urllib.request.Request(url, method="GET")
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout) as response:
                elapsed = time.monotonic() - start
                code = response.getcode()
                if code in expected_codes:
                    return ServiceHealth(
                        name=url,
                        status=HealthStatus.HEALTHY,
                        message=f"HTTP {code}",
                        response_time=elapsed,
                    )
                return ServiceHealth(
                    name=url,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Unexpected HTTP {code}",
                    response_time=elapsed,
                )
        except urllib.error.HTTPError as e:
            if e.code in expected_codes:
                return ServiceHealth(
                    name=url,
                    status=HealthStatus.HEALTHY,
                    message=f"HTTP {e.code}",
                )
            return ServiceHealth(
                name=url,
                status=HealthStatus.UNHEALTHY,
                message=f"HTTP {e.code}: {e.reason}",
            )
        except urllib.error.URLError as e:
            return ServiceHealth(
                name=url,
                status=HealthStatus.UNHEALTHY,
                message=str(e.reason),
            )
        except Exception as e:
            return ServiceHealth(
                name=url,
                status=HealthStatus.UNKNOWN,
                message=str(e),
            )

    def check_http_json(
        self,
        url: str,
        jq_path: Optional[str] = None,
        timeout: int = 10,
    ) -> ServiceHealth:
        """Check HTTP endpoint and optionally extract JSON field."""
        try:
            req = urllib.request.Request(url, method="GET")
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout) as response:
                elapsed = time.monotonic() - start
                data = json.loads(response.read().decode())

                details = {}
                if jq_path:
                    # Simple path extraction (e.g., ".version" or ".status")
                    path_parts = jq_path.lstrip(".").split(".")
                    value = data
                    for part in path_parts:
                        if isinstance(value, dict):
                            value = value.get(part)
                        else:
                            value = None
                            break
                    details["extracted"] = value

                return ServiceHealth(
                    name=url,
                    status=HealthStatus.HEALTHY,
                    message="OK",
                    details=details,
                    response_time=elapsed,
                )
        except Exception as e:
            return ServiceHealth(
                name=url,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
            )

    def check_tailscale(self, host: str = "localhost") -> ServiceHealth:
        """Check Tailscale status via SSH or locally."""
        cmd = "tailscale status --json"
        if self.ssh:
            result = self.ssh.run(cmd)
            if not result.success:
                return ServiceHealth(
                    name="tailscale",
                    status=HealthStatus.UNHEALTHY,
                    message=result.stderr or "Failed to get status",
                )
            output = result.stdout
        else:
            local = self._run_local(cmd.split(), timeout=10)
            if not local.success:
                return ServiceHealth(
                    name="tailscale",
                    status=(
                        HealthStatus.UNKNOWN if local.returncode == -1 else HealthStatus.UNHEALTHY
                    ),
                    message=local.stderr or "Failed to get status",
                )
            output = local.stdout

        try:
            data = json.loads(output)
            backend_state = data.get("BackendState", "Unknown")
            if backend_state == "Running":
                dns_name = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
                return ServiceHealth(
                    name="tailscale",
                    status=HealthStatus.HEALTHY,
                    message=f"Connected as {dns_name}" if dns_name else "Running",
                    details={"backend_state": backend_state, "dns_name": dns_name},
                )
            return ServiceHealth(
                name="tailscale",
                status=HealthStatus.UNHEALTHY,
                message=f"State: {backend_state}",
                details={"backend_state": backend_state},
            )
        except (json.JSONDecodeError, AttributeError):
            return ServiceHealth(
                name="tailscale",
                status=HealthStatus.UNKNOWN,
                message="Failed to parse status",
            )

    def check_mqtt(
        self,
        host: str = "localhost",
        port: int = 1883,
        timeout: int = 5,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ) -> ServiceHealth:
        """Check MQTT broker via a $SYS subscribe test.

        Remotely this runs ``mosquitto_sub`` inside the ``mosquitto`` container.
        If the broker answers but rejects the credentials it is still up, so that
        is reported as healthy (with a note) rather than as an outage.
        """
        sub_args = ["mosquitto_sub", "-t", "$SYS/#", "-C", "1", "-W", str(timeout)]
        if user:
            sub_args += ["-u", user]
            if password:
                sub_args += ["-P", password]

        if self.ssh:
            sub_cmd = " ".join(shlex.quote(a) for a in sub_args)
            # Fall back to sudo when the SSH user is not in the docker group yet
            # (group membership added during deploy only applies to new logins).
            cmd = (
                f"docker exec mosquitto {sub_cmd} 2>&1 || "
                f"sudo -n docker exec mosquitto {sub_cmd} 2>&1"
            )
            result = self.ssh.run(cmd, timeout=timeout + 10)
        else:
            local_args = ["timeout", str(timeout + 2)] + sub_args + ["-h", host, "-p", str(port)]
            result = self._run_local(local_args, timeout=timeout + 5)

        if result.success:
            return ServiceHealth(
                name="mqtt",
                status=HealthStatus.HEALTHY,
                message=f"Broker responding on port {port}",
            )
        output = f"{result.stdout}\n{result.stderr}".lower()
        if "not authori" in output:
            return ServiceHealth(
                name="mqtt",
                status=HealthStatus.HEALTHY,
                message=f"Broker responding on port {port} (authentication required)",
            )
        return ServiceHealth(
            name="mqtt",
            status=HealthStatus.UNHEALTHY,
            message=(result.stderr or result.stdout or "No response").strip(),
        )

    def check_docker_container(self, container_name: str) -> ServiceHealth:
        """Check Docker container health status."""
        fmt = "{{.State.Status}}:{{if .State.Health}}{{.State.Health.Status}}{{end}}"
        if self.ssh:
            result = self.ssh.run(
                f"docker inspect --format={shlex.quote(fmt)} {shlex.quote(container_name)}"
            )
        else:
            result = self._run_local(
                ["docker", "inspect", f"--format={fmt}", container_name], timeout=10
            )

        if not result.success:
            return ServiceHealth(
                name=container_name,
                status=HealthStatus.UNHEALTHY,
                message="Container not found",
            )

        parts = result.stdout.strip().split(":")
        state = parts[0] if parts else "unknown"
        health = parts[1] if len(parts) > 1 else ""

        if state == "running":
            if health in ["healthy", ""]:
                return ServiceHealth(
                    name=container_name,
                    status=HealthStatus.HEALTHY,
                    message=f"Running{' (healthy)' if health == 'healthy' else ''}",
                    details={"state": state, "health": health},
                )
            elif health == "starting":
                return ServiceHealth(
                    name=container_name,
                    status=HealthStatus.STARTING,
                    message="Starting",
                    details={"state": state, "health": health},
                )
        return ServiceHealth(
            name=container_name,
            status=HealthStatus.UNHEALTHY,
            message=f"State: {state}, Health: {health}",
            details={"state": state, "health": health},
        )

    def check_homeassistant(self, host: str, port: int = 8123) -> ServiceHealth:
        """Check Home Assistant API."""
        url = f"http://{host}:{port}/api/"
        health = self.check_http(url, expected_codes=[200, 401])
        health.name = "homeassistant"
        return health

    def check_frigate(self, host: str, port: int = 5000) -> ServiceHealth:
        """Check Frigate API and extract version."""
        url = f"http://{host}:{port}/api/version"
        health = self.check_http_json(url, jq_path=".version")
        health.name = "frigate"
        if health.status == HealthStatus.HEALTHY and health.details:
            version = health.details.get("extracted", "unknown")
            health.message = f"v{version}"
        return health
