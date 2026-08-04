"""Secrets management - wrapper for secrets.sh or direct implementation."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64


@dataclass
class Secret:
    """A single secret."""

    key: str
    value: str


class SecretsManager:
    """Manage encrypted secrets."""

    REQUIRED_SECRETS = [
        "TAILSCALE_AUTH_KEY",
        "MQTT_USER",
        "MQTT_PASS",
    ]

    OPTIONAL_SECRETS = [
        "REOLINK_USER",
        "REOLINK_PASS",
        "EXTERNAL_DRIVE_UUID",
    ]

    def __init__(self, secrets_dir: Optional[Path] = None, password: Optional[str] = None):
        self.secrets_dir = secrets_dir or Path.home() / ".edge-server-secrets"
        self.secrets_file = self.secrets_dir / "secrets.enc"
        self.salt_file = self.secrets_dir / ".salt"
        self._password = password
        self._fernet: Optional[Fernet] = None
        self._cache: Dict[str, str] = {}

    def _get_password(self) -> str:
        """Get password from environment or prompt."""
        if self._password:
            return self._password
        if pwd := os.environ.get("SECRETS_PASSWORD"):
            return pwd
        raise ValueError(
            "No password provided. Set SECRETS_PASSWORD or pass password to constructor."
        )

    def _get_fernet(self) -> Fernet:
        """Get or create Fernet instance."""
        if self._fernet:
            return self._fernet

        password = self._get_password()

        if self.salt_file.exists():
            salt = self.salt_file.read_bytes()
        else:
            salt = os.urandom(16)
            self.secrets_dir.mkdir(parents=True, exist_ok=True)
            self.secrets_dir.chmod(0o700)
            self.salt_file.write_bytes(salt)
            self.salt_file.chmod(0o600)

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        self._fernet = Fernet(key)
        return self._fernet

    def init(self, password: str) -> None:
        """Initialize secrets storage with password."""
        self._password = password
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.chmod(0o700)

        # Create salt
        salt = os.urandom(16)
        self.salt_file.write_bytes(salt)
        self.salt_file.chmod(0o600)

        # Create empty secrets file
        self._fernet = None  # Reset to use new salt
        self._cache = {}
        self._save()

    def _load(self) -> None:
        """Load secrets from encrypted file."""
        if not self.secrets_file.exists():
            self._cache = {}
            return

        fernet = self._get_fernet()
        encrypted = self.secrets_file.read_bytes()
        try:
            decrypted = fernet.decrypt(encrypted).decode()
            self._cache = {}
            for line in decrypted.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    self._cache[key.strip()] = value.strip()
        except Exception:
            raise ValueError("Failed to decrypt secrets. Wrong password?")

    def _save(self) -> None:
        """Save secrets to encrypted file."""
        fernet = self._get_fernet()
        content = "\n".join(f"{k}={v}" for k, v in sorted(self._cache.items()))
        encrypted = fernet.encrypt(content.encode())
        self.secrets_file.write_bytes(encrypted)
        self.secrets_file.chmod(0o600)

    def get(self, key: str) -> Optional[str]:
        """Get a secret value."""
        if not self._cache:
            self._load()
        return self._cache.get(key)

    def set(self, key: str, value: str) -> None:
        """Set a secret value."""
        if not self._cache and self.secrets_file.exists():
            self._load()
        self._cache[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        """Delete a secret."""
        if not self._cache:
            self._load()
        if key in self._cache:
            del self._cache[key]
            self._save()
            return True
        return False

    def list_keys(self) -> list:
        """List all secret keys."""
        if not self._cache:
            self._load()
        return list(self._cache.keys())

    def export(self) -> Dict[str, str]:
        """Export all secrets as a dictionary."""
        if not self._cache:
            self._load()
        return dict(self._cache)

    def export_shell(self) -> str:
        """Export secrets as shell export statements."""
        secrets = self.export()
        return "\n".join(f"export {k}='{v}'" for k, v in secrets.items())

    def validate(self) -> tuple:
        """Validate that required secrets are set. Returns (valid, missing)."""
        if not self._cache:
            self._load()
        missing = [k for k in self.REQUIRED_SECRETS if not self._cache.get(k)]
        return len(missing) == 0, missing

    def clear(self) -> None:
        """Clear all secrets."""
        self._cache = {}
        if self.secrets_file.exists():
            self.secrets_file.unlink()


class SecretsShellWrapper:
    """Wrapper around existing secrets.sh script."""

    def __init__(self, script_path: Path, home_dir: Optional[Path] = None):
        self.script_path = script_path
        self.home_dir = home_dir

    def _run(self, *args, password: Optional[str] = None) -> tuple:
        """Run secrets.sh with arguments."""
        env = os.environ.copy()
        if self.home_dir:
            env["HOME"] = str(self.home_dir)
        if password:
            env["SECRETS_PASSWORD"] = password

        result = subprocess.run(
            [str(self.script_path)] + list(args),
            capture_output=True,
            text=True,
            env=env,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()

    def init(self, password: str) -> bool:
        """Initialize secrets storage."""
        # For init, we need to provide password via stdin
        env = os.environ.copy()
        if self.home_dir:
            env["HOME"] = str(self.home_dir)
        env["SECRETS_PASSWORD"] = password

        result = subprocess.run(
            [str(self.script_path), "init"],
            input=f"{password}\n{password}\n",
            capture_output=True,
            text=True,
            env=env,
        )
        return result.returncode == 0

    def get(self, key: str, password: Optional[str] = None) -> Optional[str]:
        """Get a secret value."""
        success, stdout, _ = self._run("get", key, password=password)
        return stdout if success else None

    def set(self, key: str, value: str, password: Optional[str] = None) -> bool:
        """Set a secret value."""
        success, _, _ = self._run("set", key, value, password=password)
        return success

    def list_keys(self, password: Optional[str] = None) -> list:
        """List all secret keys."""
        success, stdout, _ = self._run("list", password=password)
        if success:
            return [line.strip() for line in stdout.split("\n") if line.strip()]
        return []

    def export(self, password: Optional[str] = None) -> str:
        """Export secrets as shell export statements."""
        success, stdout, _ = self._run("export", password=password)
        return stdout if success else ""
