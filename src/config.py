"""Configuration management for edge server deployment."""

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TimeoutConfig:
    """Timeout settings for each phase."""

    discovery: int = 30
    prerequisites: int = 60
    initial_setup: int = 300
    reboot_wait: int = 120
    secrets: int = 60
    deployment_base: int = 600
    deployment_security: int = 900
    verification: int = 120
    probe_interval: int = 5
    probe_count: int = 3


@dataclass
class NetworkConfig:
    """Network configuration defaults."""

    default_dns: str = "8.8.8.8"
    default_gateway: str = ""
    static_ip: str = ""


@dataclass
class ServiceConfig:
    """Service verification settings."""

    verify_tailscale: bool = True
    verify_homeassistant: bool = True
    verify_mqtt: bool = True
    verify_frigate: bool = True
    ha_port: int = 8123
    mqtt_port: int = 1883
    frigate_port: int = 5000


@dataclass
class RollbackConfig:
    """Rollback settings."""

    enabled: bool = True
    snapshot_before_deploy: bool = True
    restore_on_failure: bool = True
    snapshot_dir: str = "/var/lib/edge-server-snapshots"


@dataclass
class SSHConfig:
    """SSH connection settings."""

    user: str = ""
    timeout: int = 10
    retries: int = 3


@dataclass
class Config:
    """Main configuration container."""

    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    services: ServiceConfig = field(default_factory=ServiceConfig)
    rollback: RollbackConfig = field(default_factory=RollbackConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load configuration from file, falling back to defaults."""
        config = cls()

        if config_path is None:
            config_path = Path("bootstrap.cfg")

        if not config_path.exists():
            return config

        parser = configparser.ConfigParser()
        parser.read(config_path)

        # Timeouts
        if parser.has_section("timeouts"):
            t = config.timeouts
            t.discovery = parser.getint("timeouts", "DISCOVERY", fallback=t.discovery)
            t.prerequisites = parser.getint("timeouts", "PREREQUISITES", fallback=t.prerequisites)
            t.initial_setup = parser.getint("timeouts", "INITIAL_SETUP", fallback=t.initial_setup)
            t.reboot_wait = parser.getint("timeouts", "REBOOT_WAIT", fallback=t.reboot_wait)
            t.secrets = parser.getint("timeouts", "SECRETS", fallback=t.secrets)
            t.deployment_base = parser.getint(
                "timeouts", "DEPLOYMENT_BASE", fallback=t.deployment_base
            )
            t.deployment_security = parser.getint(
                "timeouts", "DEPLOYMENT_SECURITY", fallback=t.deployment_security
            )
            t.verification = parser.getint("timeouts", "VERIFICATION", fallback=t.verification)
            t.probe_interval = parser.getint(
                "timeouts", "PROBE_INTERVAL", fallback=t.probe_interval
            )
            t.probe_count = parser.getint("timeouts", "PROBE_COUNT", fallback=t.probe_count)

        # Network
        if parser.has_section("network"):
            n = config.network
            n.default_dns = parser.get("network", "DEFAULT_DNS", fallback=n.default_dns)
            n.default_gateway = parser.get("network", "DEFAULT_GATEWAY", fallback=n.default_gateway)

        # Services
        if parser.has_section("services"):
            s = config.services
            s.verify_tailscale = parser.getboolean(
                "services", "VERIFY_TAILSCALE", fallback=s.verify_tailscale
            )
            s.verify_homeassistant = parser.getboolean(
                "services", "VERIFY_HOMEASSISTANT", fallback=s.verify_homeassistant
            )
            s.verify_mqtt = parser.getboolean("services", "VERIFY_MQTT", fallback=s.verify_mqtt)
            s.verify_frigate = parser.getboolean(
                "services", "VERIFY_FRIGATE", fallback=s.verify_frigate
            )
            s.ha_port = parser.getint("services", "HA_PORT", fallback=s.ha_port)
            s.mqtt_port = parser.getint("services", "MQTT_PORT", fallback=s.mqtt_port)
            s.frigate_port = parser.getint("services", "FRIGATE_PORT", fallback=s.frigate_port)

        # Rollback
        if parser.has_section("rollback"):
            r = config.rollback
            r.enabled = parser.getboolean("rollback", "ENABLED", fallback=r.enabled)
            r.snapshot_before_deploy = parser.getboolean(
                "rollback", "SNAPSHOT_BEFORE_DEPLOY", fallback=r.snapshot_before_deploy
            )
            r.restore_on_failure = parser.getboolean(
                "rollback", "RESTORE_ON_FAILURE", fallback=r.restore_on_failure
            )
            r.snapshot_dir = parser.get("rollback", "SNAPSHOT_DIR", fallback=r.snapshot_dir)

        # SSH/Target
        if parser.has_section("target"):
            config.ssh.timeout = parser.getint("target", "SSH_TIMEOUT", fallback=config.ssh.timeout)
            config.ssh.retries = parser.getint("target", "SSH_RETRIES", fallback=config.ssh.retries)

        return config

    def save(self, config_path: Path) -> None:
        """Save configuration to file."""
        parser = configparser.ConfigParser()

        # Timeouts
        parser["timeouts"] = {
            "DISCOVERY": str(self.timeouts.discovery),
            "PREREQUISITES": str(self.timeouts.prerequisites),
            "INITIAL_SETUP": str(self.timeouts.initial_setup),
            "REBOOT_WAIT": str(self.timeouts.reboot_wait),
            "SECRETS": str(self.timeouts.secrets),
            "DEPLOYMENT_BASE": str(self.timeouts.deployment_base),
            "DEPLOYMENT_SECURITY": str(self.timeouts.deployment_security),
            "VERIFICATION": str(self.timeouts.verification),
            "PROBE_INTERVAL": str(self.timeouts.probe_interval),
            "PROBE_COUNT": str(self.timeouts.probe_count),
        }

        # Network
        parser["network"] = {
            "DEFAULT_DNS": self.network.default_dns,
            "DEFAULT_GATEWAY": self.network.default_gateway,
        }

        # Services
        parser["services"] = {
            "VERIFY_TAILSCALE": str(self.services.verify_tailscale).lower(),
            "VERIFY_HOMEASSISTANT": str(self.services.verify_homeassistant).lower(),
            "VERIFY_MQTT": str(self.services.verify_mqtt).lower(),
            "VERIFY_FRIGATE": str(self.services.verify_frigate).lower(),
            "HA_PORT": str(self.services.ha_port),
            "MQTT_PORT": str(self.services.mqtt_port),
            "FRIGATE_PORT": str(self.services.frigate_port),
        }

        # Rollback
        parser["rollback"] = {
            "ENABLED": str(self.rollback.enabled).lower(),
            "SNAPSHOT_BEFORE_DEPLOY": str(self.rollback.snapshot_before_deploy).lower(),
            "RESTORE_ON_FAILURE": str(self.rollback.restore_on_failure).lower(),
            "SNAPSHOT_DIR": self.rollback.snapshot_dir,
        }

        # Target/SSH
        parser["target"] = {
            "SSH_TIMEOUT": str(self.ssh.timeout),
            "SSH_RETRIES": str(self.ssh.retries),
        }

        with open(config_path, "w") as f:
            parser.write(f)
