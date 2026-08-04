"""Service health checks and monitoring."""

import json
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ssh import SSHClient


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

    def check_tcp_port(self, host: str, port: int, timeout: int = 5) -> ServiceHealth:
        """Check if a TCP port is open."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return ServiceHealth(
                    name=f"tcp:{port}",
                    status=HealthStatus.HEALTHY,
                    message=f"Port {port} is open",
                )
            return ServiceHealth(
                name=f"tcp:{port}",
                status=HealthStatus.UNHEALTHY,
                message=f"Port {port} is closed",
            )
        except TimeoutError:
            return ServiceHealth(
                name=f"tcp:{port}",
                status=HealthStatus.UNHEALTHY,
                message=f"Connection to port {port} timed out",
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
            import time

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
            import time

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
            try:
                proc = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=10)
                if proc.returncode != 0:
                    return ServiceHealth(
                        name="tailscale",
                        status=HealthStatus.UNHEALTHY,
                        message=proc.stderr or "Failed to get status",
                    )
                output = proc.stdout
            except Exception as e:
                return ServiceHealth(
                    name="tailscale",
                    status=HealthStatus.UNKNOWN,
                    message=str(e),
                )

        try:
            data = json.loads(output)
            backend_state = data.get("BackendState", "Unknown")
            if backend_state == "Running":
                dns_name = data.get("Self", {}).get("DNSName", "").rstrip(".")
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
        except json.JSONDecodeError:
            return ServiceHealth(
                name="tailscale",
                status=HealthStatus.UNKNOWN,
                message="Failed to parse status",
            )

    def check_mqtt(
        self, host: str = "localhost", port: int = 1883, timeout: int = 5
    ) -> ServiceHealth:
        """Check MQTT broker via subscribe test."""
        cmd = f"timeout {timeout} mosquitto_sub -h {host} -p {port} -t '$SYS/#' -C 1 -W {timeout}"
        if self.ssh:
            # Run via docker exec on remote
            cmd = f"docker exec mosquitto mosquitto_sub -t '$SYS/#' -C 1 -W {timeout}"
            result = self.ssh.run(cmd, timeout=timeout + 5)
        else:
            try:
                result_proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=timeout + 5
                )

                class Result:
                    success = result_proc.returncode == 0
                    stderr = result_proc.stderr

                result = Result()
            except Exception as e:
                return ServiceHealth(
                    name="mqtt",
                    status=HealthStatus.UNKNOWN,
                    message=str(e),
                )

        if result.success:
            return ServiceHealth(
                name="mqtt",
                status=HealthStatus.HEALTHY,
                message=f"Broker responding on port {port}",
            )
        return ServiceHealth(
            name="mqtt",
            status=HealthStatus.UNHEALTHY,
            message=result.stderr if hasattr(result, "stderr") else "No response",
        )

    def check_docker_container(self, container_name: str) -> ServiceHealth:
        """Check Docker container health status."""
        cmd = f"docker inspect --format='{{{{.State.Status}}}}:{{{{.State.Health.Status}}}}' {container_name}"
        if self.ssh:
            result = self.ssh.run(cmd)
        else:
            try:
                proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)

                class Result:
                    success = proc.returncode == 0
                    stdout = proc.stdout.strip()
                    stderr = proc.stderr

                result = Result()
            except Exception as e:
                return ServiceHealth(
                    name=container_name,
                    status=HealthStatus.UNKNOWN,
                    message=str(e),
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
