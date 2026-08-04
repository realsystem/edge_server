"""Tests for services module."""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import socket
import threading
import time

import pytest

from services import HealthStatus, ServiceHealth, ServiceChecker


class TestHealthStatus:
    """Tests for HealthStatus enum."""

    def test_status_values(self):
        """Test health status values."""
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"
        assert HealthStatus.UNKNOWN.value == "unknown"


class TestServiceHealth:
    """Tests for ServiceHealth dataclass."""

    def test_create_health(self):
        """Test creating health result."""
        health = ServiceHealth(
            name="test",
            status=HealthStatus.HEALTHY,
            message="OK",
            response_time=0.5,
        )
        assert health.name == "test"
        assert health.status == HealthStatus.HEALTHY
        assert health.response_time == 0.5

    def test_health_with_details(self):
        """Test health with details dict."""
        health = ServiceHealth(
            name="test",
            status=HealthStatus.HEALTHY,
            message="OK",
            details={"version": "1.0.0"},
        )
        assert health.details["version"] == "1.0.0"


class TestServiceChecker:
    """Tests for ServiceChecker."""

    def test_check_tcp_port_closed(self):
        """Test checking closed TCP port."""
        checker = ServiceChecker()
        # Use a port that's likely closed
        health = checker.check_tcp_port("127.0.0.1", 59999, timeout=1)
        assert health.status == HealthStatus.UNHEALTHY

    def test_check_tcp_port_open(self):
        """Test checking open TCP port."""
        # Create a listening socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)

        try:
            checker = ServiceChecker()
            health = checker.check_tcp_port("127.0.0.1", port, timeout=1)
            assert health.status == HealthStatus.HEALTHY
        finally:
            sock.close()

    def test_check_http_unreachable(self):
        """Test checking unreachable HTTP endpoint."""
        checker = ServiceChecker()
        health = checker.check_http("http://127.0.0.1:59999/", timeout=1)
        assert health.status == HealthStatus.UNHEALTHY

    def test_check_http_success(self):
        """Test checking working HTTP endpoint."""
        # Start a simple HTTP server
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, format, *args):
                pass  # Suppress logging

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            checker = ServiceChecker()
            health = checker.check_http(f"http://127.0.0.1:{port}/", timeout=2)
            assert health.status == HealthStatus.HEALTHY
            assert health.response_time is not None
        finally:
            thread.join(timeout=2)
            server.server_close()

    def test_check_http_json(self):
        """Test checking HTTP JSON endpoint."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"version": "1.2.3", "status": "ok"}).encode())

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            checker = ServiceChecker()
            health = checker.check_http_json(
                f"http://127.0.0.1:{port}/",
                jq_path=".version",
                timeout=2,
            )
            assert health.status == HealthStatus.HEALTHY
            assert health.details["extracted"] == "1.2.3"
        finally:
            thread.join(timeout=2)
            server.server_close()

    def test_check_http_expected_codes(self):
        """Test checking HTTP with expected codes including 401."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            checker = ServiceChecker()
            health = checker.check_http(
                f"http://127.0.0.1:{port}/",
                expected_codes=[200, 401],
                timeout=2,
            )
            assert health.status == HealthStatus.HEALTHY
        finally:
            thread.join(timeout=2)
            server.server_close()
