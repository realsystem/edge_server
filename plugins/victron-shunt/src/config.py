"""Configuration management for victron-shunt."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None


CONFIG_PATHS = [
    Path.home() / ".config" / "victron-shunt.yaml",
    Path("/etc/victron-shunt/config.yaml"),
]


@dataclass
class MQTTConfig:
    """MQTT connection settings."""

    host: str = "localhost"
    port: int = 1883
    user: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: str = "victron/smartshunt"


@dataclass
class ShutdownConfig:
    """Power loss shutdown settings."""

    enabled: bool = False
    delay_minutes: int = 10
    min_soc: Optional[float] = None  # Also shutdown if SoC drops below this


@dataclass
class Config:
    """Victron Smart Shunt configuration."""

    address: Optional[str] = None
    key: Optional[str] = None
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    shutdown: ShutdownConfig = field(default_factory=ShutdownConfig)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load config from file, with env var overrides."""
        config = cls()

        # Load from file
        if config_path:
            config._load_file(config_path)
        else:
            for path in CONFIG_PATHS:
                if path.exists():
                    config._load_file(path)
                    break

        # Apply env var overrides
        config._apply_env_overrides()

        return config

    def _load_file(self, path: Path) -> None:
        """Load configuration from YAML file."""
        if yaml is None:
            raise RuntimeError("PyYAML not installed (pip install pyyaml)")

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        self.address = data.get("address", self.address)
        self.key = data.get("key", self.key)

        if "mqtt" in data:
            mqtt = data["mqtt"]
            self.mqtt.host = mqtt.get("host", self.mqtt.host)
            self.mqtt.port = mqtt.get("port", self.mqtt.port)
            self.mqtt.user = mqtt.get("user", self.mqtt.user)
            self.mqtt.password = mqtt.get("password", self.mqtt.password)
            self.mqtt.topic_prefix = mqtt.get("topic_prefix", self.mqtt.topic_prefix)

    def _apply_env_overrides(self) -> None:
        """Override config values from environment variables."""
        if os.environ.get("VICTRON_ADDRESS"):
            self.address = os.environ["VICTRON_ADDRESS"]
        if os.environ.get("VICTRON_KEY"):
            self.key = os.environ["VICTRON_KEY"]
        if os.environ.get("MQTT_HOST"):
            self.mqtt.host = os.environ["MQTT_HOST"]
        if os.environ.get("MQTT_PORT"):
            self.mqtt.port = int(os.environ["MQTT_PORT"])
        if os.environ.get("MQTT_USER"):
            self.mqtt.user = os.environ["MQTT_USER"]
        if os.environ.get("MQTT_PASS"):
            self.mqtt.password = os.environ["MQTT_PASS"]

    def save(self, path: Path) -> None:
        """Save configuration to YAML file."""
        if yaml is None:
            raise RuntimeError("PyYAML not installed (pip install pyyaml)")

        data = {
            "address": self.address,
            "key": self.key,
            "mqtt": {
                "host": self.mqtt.host,
                "port": self.mqtt.port,
                "user": self.mqtt.user,
                "topic_prefix": self.mqtt.topic_prefix,
            },
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        os.chmod(path, 0o600)

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors = []
        if not self.address:
            errors.append("address: required (device MAC address)")
        if not self.key:
            errors.append("key: required (encryption key from Victron Connect)")
        elif len(self.key.replace(" ", "").replace("-", "").replace(":", "")) != 32:
            errors.append("key: must be 32 hex characters")
        return errors
