"""Tests for secrets module."""

import tempfile
from pathlib import Path

import pytest

from secret_store import SecretsManager


class TestSecretsManager:
    """Tests for SecretsManager."""

    def test_init_creates_directory(self):
        """Test initialization creates secrets directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            assert secrets_dir.exists()
            assert (secrets_dir / ".salt").exists()

    def test_set_and_get(self):
        """Test setting and getting secrets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            manager.set("TEST_KEY", "test_value")
            assert manager.get("TEST_KEY") == "test_value"

    def test_get_nonexistent(self):
        """Test getting nonexistent key returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            assert manager.get("NONEXISTENT") is None

    def test_list_keys(self):
        """Test listing secret keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            manager.set("KEY1", "value1")
            manager.set("KEY2", "value2")

            keys = manager.list_keys()
            assert "KEY1" in keys
            assert "KEY2" in keys

    def test_delete(self):
        """Test deleting a secret."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            manager.set("TO_DELETE", "value")
            assert manager.get("TO_DELETE") == "value"

            assert manager.delete("TO_DELETE") is True
            assert manager.get("TO_DELETE") is None

    def test_delete_nonexistent(self):
        """Test deleting nonexistent key returns False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            assert manager.delete("NONEXISTENT") is False

    def test_export(self):
        """Test exporting secrets as dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            manager.set("KEY1", "value1")
            manager.set("KEY2", "value2")

            exported = manager.export()
            assert exported == {"KEY1": "value1", "KEY2": "value2"}

    def test_export_shell(self):
        """Test exporting secrets as shell statements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            manager.set("MY_KEY", "my_value")

            shell_export = manager.export_shell()
            assert "export MY_KEY='my_value'" in shell_export

    def test_validate_missing(self):
        """Test validation with missing required secrets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            valid, missing = manager.validate()
            assert valid is False
            assert "TAILSCALE_AUTH_KEY" in missing

    def test_validate_complete(self):
        """Test validation with all required secrets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            for key in manager.REQUIRED_SECRETS:
                manager.set(key, "test_value")

            valid, missing = manager.validate()
            assert valid is True
            assert len(missing) == 0

    def test_clear(self):
        """Test clearing all secrets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"
            manager = SecretsManager(secrets_dir, password="testpass123")
            manager.init("testpass123")

            manager.set("KEY1", "value1")
            manager.clear()

            assert manager.list_keys() == []

    def test_persistence(self):
        """Test secrets persist across instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"

            # First instance
            manager1 = SecretsManager(secrets_dir, password="testpass123")
            manager1.init("testpass123")
            manager1.set("PERSISTENT_KEY", "persistent_value")

            # Second instance
            manager2 = SecretsManager(secrets_dir, password="testpass123")
            assert manager2.get("PERSISTENT_KEY") == "persistent_value"

    def test_wrong_password(self):
        """Test wrong password raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / ".edge-server-secrets"

            # Create with correct password
            manager1 = SecretsManager(secrets_dir, password="correct")
            manager1.init("correct")
            manager1.set("KEY", "value")

            # Try to read with wrong password
            manager2 = SecretsManager(secrets_dir, password="wrong")
            with pytest.raises(ValueError, match="decrypt"):
                manager2.get("KEY")
