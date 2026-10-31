"""Main bootstrap orchestration."""

import argparse
import getpass
import logging
import os
import shlex
import shutil
import sys
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from config import Config
from progress import Progress
from services import HealthStatus, ServiceChecker
from snapshot import SnapshotManager
from ssh import SSHClient, SSHResult
from state import Phase, PhaseStatus, StateMachine

DEFAULT_LOG_DIR = Path.home() / ".edge-server" / "logs"
DEFAULT_SECRETS_DIR = "$HOME/.edge-server-secrets"


def remote_path(path: str) -> str:
    """Quote a path for a remote shell, still expanding a leading $HOME or ~."""
    for prefix in ("$HOME/", "~/"):
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            return '"$HOME"/' + shlex.quote(rest) if rest else '"$HOME"'
    if path in ("$HOME", "~"):
        return '"$HOME"'
    return shlex.quote(path)


class Bootstrap:
    """Main bootstrap orchestrator."""

    SCRIPTS = [
        "initial-setup.sh",
        "deploy-edge-server.sh",
        "deploy-security.sh",
        "secrets.sh",
        "discover-cameras.sh",
        "collect-diagnostics.sh",
        "telegram-bot",  # Telegram bot (bot.py, Dockerfile)
        ".telegram",  # Telegram bot token and allowed user ids, if present
        "plugins",  # deploy-edge-server.sh looks for plugins/ next to itself
        "tools",  # camera discovery
        ".cam",  # camera logins ("IP user:pass" per line), if present
    ]

    # /var/tmp survives the reboot at the end of initial setup; /tmp does not,
    # which used to leave the secrets/deploy phases without their scripts.
    REMOTE_DIR = "/var/tmp/edge-server-setup"

    def __init__(
        self,
        target: str,
        config: Config,
        mode: str = "manual",
        deploy_type: str = "full",
        dry_run: bool = False,
        skip_init: bool = False,
        no_rollback: bool = False,
        secrets_file: Path | None = None,
        script_dir: Path | None = None,
    ):
        self.target = target
        self.config = config
        self.mode = mode
        self.deploy_type = deploy_type
        self.dry_run = dry_run
        self.skip_init = skip_init
        self.no_rollback = no_rollback
        self.secrets_file = secrets_file
        self.script_dir = script_dir or Path(__file__).parent.parent

        self.progress = Progress(no_color=not sys.stdout.isatty())
        self.ssh: SSHClient | None = None
        self.state = StateMachine()
        self.checker: ServiceChecker | None = None
        self.snapshots: SnapshotManager | None = None

        self._secrets: dict[str, str] = {}
        self._secrets_pass = ""
        self._snapshot_taken = False
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Setup logging to local file."""
        log_dir = (
            Path(self.config.logging.log_dir) if self.config.logging.log_dir else DEFAULT_LOG_DIR
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_target = "".join(c if c.isalnum() or c in ".-_" else "_" for c in self.target)
        log_file = log_dir / f"bootstrap_{safe_target}_{timestamp}.log"

        # Logs can contain deploy output (e.g. generated credentials): owner-only.
        log_file.touch(mode=0o600, exist_ok=True)
        handler = logging.FileHandler(log_file)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger = logging.getLogger(f"bootstrap.{id(self)}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(handler)
        self.log_file = log_file
        self.secrets_dir = self.config.secrets.secrets_dir or DEFAULT_SECRETS_DIR
        self.logger.info(f"Bootstrap started for target: {self.target}")
        self.logger.info(f"Mode: {self.mode}, Deploy type: {self.deploy_type}")
        self.logger.info(f"Secrets dir: {self.secrets_dir}")

    @property
    def _remote(self) -> SSHClient:
        """SSH client for phases that run after discovery connected it."""
        if self.ssh is None:
            raise RuntimeError("Not connected to target (discovery phase not run)")
        return self.ssh

    @property
    def _checker(self) -> ServiceChecker:
        if self.checker is None:
            raise RuntimeError("Service checker not initialised (discovery phase not run)")
        return self.checker

    def _log(self, message: str, level: str = "info") -> None:
        """Log a message."""
        getattr(self.logger, level)(message)

    def _log_result(self, phase: str, result: "SSHResult") -> None:
        """Log SSH result."""
        self.logger.info(f"[{phase}] returncode: {result.returncode}")
        if result.stdout:
            self.logger.debug(f"[{phase}] stdout:\n{result.stdout}")
        if result.stderr:
            self.logger.warning(f"[{phase}] stderr:\n{result.stderr}")

    def _ssh_command(self) -> str:
        """Build SSH command string for display."""
        parts = ["ssh"]
        if self.config.ssh.key_file:
            key_path = Path(self.config.ssh.key_file).expanduser().resolve()
            parts.append(f"-i {key_path}")
        if self.config.ssh.port != 22:
            parts.append(f"-p {self.config.ssh.port}")
        parts.append(f"{self.config.ssh.user}@{self.target}")
        return " ".join(parts)

    def run(self) -> bool:
        """Run the bootstrap process."""
        self.progress.header("Edge Server Bootstrap", self.mode.title(), self.dry_run)

        try:
            # Phase 1: Discovery
            if not self._phase_discovery():
                return False

            # Phase 2: Prerequisites
            if not self._phase_prerequisites():
                return False

            # Phase 3: Initial Setup
            if not self._phase_initial_setup():
                return False

            # Phase 4: Secrets
            if not self._phase_secrets():
                return False

            # Phase 5: Deployment
            if not self._phase_deployment():
                return False

            # Phase 6: Verification
            if not self._phase_verification():
                return False

            # Success
            self._log("Bootstrap completed successfully")
            self._print_success()
            self.progress.info(f"Log saved to: {self.log_file}")
            return True

        except KeyboardInterrupt:
            self._log("Interrupted by user", "warning")
            self.progress.fail("Interrupted by user")
            self.progress.info(f"Log saved to: {self.log_file}")
            return False
        except Exception as e:
            self.logger.exception("Unexpected error")
            self.progress.fail(f"Unexpected error: {e}")
            self.progress.info(f"Log saved to: {self.log_file}")
            self._maybe_rollback()
            return False

    def _phase_discovery(self) -> bool:
        """Phase 1: Discovery."""
        self.progress.phase(1, "Discovery", self.config.timeouts.discovery)
        start = self.state.start_phase(Phase.DISCOVERY)

        self.progress.info(f"Target: {self.target}")
        self.progress.info(f"SSH user: {self.config.ssh.user}")
        self.progress.info(f"Deploy type: {self.deploy_type}")

        if self.dry_run:
            self.progress.dry_run("Would test SSH connection")
            self.progress.dry_run("Would check OS version")
            self.progress.dry_run("Would check for existing deployment")
            self.state.complete_phase(Phase.DISCOVERY, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        # Test SSH connection
        self.ssh = SSHClient(
            host=self.target,
            user=self.config.ssh.user,
            port=self.config.ssh.port,
            timeout=self.config.ssh.timeout,
            retries=self.config.ssh.retries,
            key_file=self.config.ssh.key_file or None,
            strict_host_key_checking=self.config.ssh.strict_host_key_checking,
        )

        with self.progress.task("SSH connection"):
            if not self.ssh.test_connection():
                self.progress.fail(
                    f"Cannot connect via SSH to {self.config.ssh.user}@{self.target}",
                    "Check: IP, SSH enabled, authorized key",
                )
                self.state.complete_phase(Phase.DISCOVERY, PhaseStatus.FAILED, "SSH failed", start)
                return False

        # Check OS
        with self.progress.task("OS detection"):
            os_info = self.ssh.get_os_info()
            if os_info and "Ubuntu" in os_info:
                self.progress.ok(f"OS: {os_info}")
            else:
                self.progress.warn(f"OS: {os_info or 'Unknown'} (expected Ubuntu)")
                if self.mode == "manual" and not self._confirm("Continue anyway?"):
                    return False

        # Check sudo
        with self.progress.task("Sudo access"):
            if not self.ssh.test_sudo():
                self.progress.warn("Sudo requires password (may cause issues)")

        # Check existing deployment
        if self.ssh.path_exists("/opt/edge-server"):
            self.progress.warn("Existing deployment detected at /opt/edge-server")
            if self.mode == "manual" and not self._confirm("Continue with upgrade?"):
                return False

        self.checker = ServiceChecker(self.ssh)
        self.snapshots = SnapshotManager(self.ssh, self.config.rollback.snapshot_dir)

        self.state.complete_phase(Phase.DISCOVERY, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
        return True

    def _phase_prerequisites(self) -> bool:
        """Phase 2: Prerequisites."""
        self.progress.phase(2, "Prerequisites", self.config.timeouts.prerequisites)
        start = self.state.start_phase(Phase.PREREQUISITES)

        # Check local tools
        for tool in ["ssh", "scp"]:
            if shutil.which(tool):
                self.progress.ok(f"Local tool: {tool}")
            elif self.dry_run:
                self.progress.warn(f"Missing local tool: {tool} (required for a real run)")
            else:
                self.progress.fail(f"Missing local tool: {tool}")
                self.state.complete_phase(
                    Phase.PREREQUISITES, PhaseStatus.FAILED, f"Missing {tool}", start
                )
                return False

        if self.dry_run:
            self.progress.dry_run(f"Would copy scripts to {self.target}:{self.REMOTE_DIR}")
            self.state.complete_phase(Phase.PREREQUISITES, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        # Copy scripts
        self.progress.info(f"Copying scripts to {self.target}:{self.REMOTE_DIR}...")
        self._remote.mkdir(self.REMOTE_DIR)
        # Private: the copied files can include .cam (camera logins) and .telegram (bot token)
        self._remote.run(f"chmod 700 {self.REMOTE_DIR}")
        scripts = [self.script_dir / s for s in self.SCRIPTS if (self.script_dir / s).exists()]
        self.progress.info(f"  Scripts: {', '.join(s.name for s in scripts)}")
        if not self._remote.copy_files(scripts, self.REMOTE_DIR):
            self.progress.fail("Failed to copy scripts")
            self.state.complete_phase(Phase.PREREQUISITES, PhaseStatus.FAILED, "Copy failed", start)
            return False
        self._remote.run(
            f"chmod +x {self.REMOTE_DIR}/*.sh && "
            f"if [ -f {self.REMOTE_DIR}/.cam ]; then chmod 600 {self.REMOTE_DIR}/.cam; fi && "
            f"if [ -f {self.REMOTE_DIR}/.telegram ]; then chmod 600 {self.REMOTE_DIR}/.telegram; fi"
        )
        self.progress.ok(f"Copied {len(scripts)} scripts")

        self.state.complete_phase(Phase.PREREQUISITES, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
        return True

    def _phase_initial_setup(self) -> bool:
        """Phase 3: Initial Setup."""
        if self.skip_init:
            self.progress.phase(3, "Initial Setup (skipped)")
            self.progress.skip("Skipping initial setup as requested")
            self.state.complete_phase(Phase.INITIAL_SETUP, PhaseStatus.SKIPPED, "Skipped", 0)
            return True

        self.progress.phase(3, "Initial Setup", self.config.timeouts.initial_setup)
        start = self.state.start_phase(Phase.INITIAL_SETUP)

        if self.dry_run:
            self.progress.dry_run("Would configure static IP")
            self.progress.dry_run("Would run initial-setup.sh")
            self.progress.dry_run("Would reboot target")
            self.state.complete_phase(Phase.INITIAL_SETUP, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        # Get configuration (all optional - press Enter to skip)
        self.progress.info("Configure static IP (press Enter to skip if already configured):")
        static_ip = self._prompt(
            "Static IP (e.g., 192.168.1.100/24)",
            self.config.network.static_ip,
            env_key="STATIC_IP",
        )

        if static_ip:
            gateway = self._prompt(
                "Gateway", self.config.network.default_gateway, env_key="GATEWAY"
            )
            dns = self._prompt("DNS server", self.config.network.default_dns, env_key="DNS")
        else:
            gateway = ""
            dns = ""
            self.progress.info("  Skipping network configuration")

        # Create snapshot before changes
        self._create_snapshot("initial_setup", "Before initial setup", paths=["/etc/netplan"])

        # Run initial setup. The script runs over a non-interactive SSH session,
        # so it must never prompt: everything it needs was collected above.
        env = {"BATCH_MODE": "true"}
        if static_ip:
            env["STATIC_IP"] = static_ip
        if gateway:
            env["GATEWAY"] = gateway
        if dns:
            env["DNS"] = dns

        self._log(f"Running initial-setup.sh with env: {env}")
        timeout = self.config.timeouts.initial_setup
        self.progress.info(f"Running initial-setup.sh (timeout: {timeout}s)...")

        def on_setup_progress(elapsed: float) -> None:
            self.progress.waiting("initial-setup.sh", elapsed, timeout)

        result = self._remote.run_script(
            f"{self.REMOTE_DIR}/initial-setup.sh",
            env=env,
            timeout=timeout,
            on_progress=on_setup_progress,
        )
        self.progress.clear_line()
        self._log_result("initial-setup.sh", result)
        if not result.success:
            self._log("Initial setup failed", "error")
            self.progress.fail("Initial setup failed")
            self.state.complete_phase(
                Phase.INITIAL_SETUP, PhaseStatus.FAILED, "Script failed", start
            )
            if result.stderr:
                self.progress.info(f"  {result.stderr[:500]}")
            if result.stdout:
                lines = result.stdout.strip().split("\n")
                for line in lines[-10:]:
                    self.progress.info(f"  {line}")
            self.progress.info(f"  Full log: {self.log_file}")
            return False

        # Reboot
        if self.mode == "manual":
            if not self._confirm("Reboot target now?"):
                self.progress.skip("Reboot skipped")
                self.state.complete_phase(
                    Phase.INITIAL_SETUP, PhaseStatus.SUCCESS, "No reboot", start
                )
                return True

        with self.progress.task("Rebooting"):
            self._remote.reboot()

        # Wait for host
        self.progress.info("Waiting for target to come back online...")
        timeout = self.config.timeouts.reboot_wait
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < timeout:
            elapsed = time.monotonic() - start_wait
            self.progress.waiting("Waiting for host", elapsed, timeout)
            if self._remote.test_connection():
                self.progress.clear_line()
                self.progress.ok(f"Target back online ({elapsed:.0f}s)")
                break
            time.sleep(self.config.timeouts.probe_interval)
        else:
            self.progress.clear_line()
            self.progress.fail("Target did not come back online")
            self.state.complete_phase(
                Phase.INITIAL_SETUP, PhaseStatus.FAILED, "Host offline after reboot", start
            )
            return False

        self.state.complete_phase(Phase.INITIAL_SETUP, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
        return True

    def _phase_secrets(self) -> bool:
        """Phase 4: Secrets Configuration."""
        self.progress.phase(4, "Secrets Configuration", self.config.timeouts.secrets)
        start = self.state.start_phase(Phase.SECRETS)

        # Collect secrets
        if self.secrets_file and self.secrets_file.exists():
            self._load_secrets_file()
        else:
            self._secrets["TAILSCALE_AUTH_KEY"] = self._prompt(
                "TAILSCALE_AUTH_KEY", "", secret=True
            )
            if self.config.deploy.install_mqtt:
                self._secrets["MQTT_USER"] = self._prompt("MQTT_USER", "homeassistant")
                self._secrets["MQTT_PASS"] = self._prompt("MQTT_PASS", "", secret=True)
            self._secrets["REOLINK_USER"] = self._prompt("REOLINK_USER", "admin")
            self._secrets["REOLINK_PASS"] = self._prompt("REOLINK_PASS", "", secret=True)

        if self.dry_run:
            self.progress.dry_run("Would configure secrets on target")
            self.state.complete_phase(Phase.SECRETS, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        secrets_dir = remote_path(self.secrets_dir)

        # Check if secrets already initialized
        check_result = self._remote.run(f"[ -f {secrets_dir}/secrets.enc ]")
        secrets_exist = check_result.success

        # Get password - either from env, or prompt user
        secrets_pass = os.environ.get("SECRETS_PASSWORD", "")
        if not secrets_pass:
            if self.mode == "manual":
                if secrets_exist:
                    secrets_pass = self._prompt("Secrets storage password", "", secret=True)
                else:
                    secrets_pass = self._prompt("Create secrets storage password", "", secret=True)
                if not secrets_pass:
                    self.progress.fail("Secrets password required")
                    self.state.complete_phase(
                        Phase.SECRETS, PhaseStatus.FAILED, "No password", start
                    )
                    return False
            else:
                self.progress.fail("SECRETS_PASSWORD env var required in auto mode")
                self.state.complete_phase(Phase.SECRETS, PhaseStatus.FAILED, "No password", start)
                return False

        if "\n" in secrets_pass:
            self.progress.fail("Secrets password cannot contain newlines")
            self.state.complete_phase(Phase.SECRETS, PhaseStatus.FAILED, "Bad password", start)
            return False

        # Kept in memory only; passwords and secret values travel over the SSH
        # channel's stdin, never on a (world-readable) remote command line.
        self._secrets_pass = secrets_pass
        secrets_cmd = f"cd {self.REMOTE_DIR} && SECRETS_DIR={secrets_dir} ./secrets.sh"

        if secrets_exist:
            self.progress.ok("Secrets storage already initialized")
        else:
            self.progress.info("Initializing secrets storage...")
            result = self._remote.run(
                f"{secrets_cmd} init 2>&1", input=f"{secrets_pass}\n{secrets_pass}\n"
            )
            self._log_result("secrets.sh init", result)
            if not result.success:
                self.progress.fail("Failed to initialize secrets storage")
                if result.stdout:
                    self.progress.info(f"  {result.stdout}")
                if result.stderr:
                    self.progress.info(f"  {result.stderr}")
                self.progress.info(f"  Log: {self.log_file}")
                self.state.complete_phase(Phase.SECRETS, PhaseStatus.FAILED, "Init failed", start)
                return False
            self.progress.ok("Secrets storage initialized")

        secrets_set = 0
        secrets_failed = 0
        self.progress.info("Setting secrets...")
        for key, value in self._secrets.items():
            if not value:
                continue
            self.progress.info(f"  Setting {key}...")
            if "\n" in value:
                secrets_failed += 1
                self.progress.fail(f"    {key}: value cannot contain newlines")
                continue
            result = self._remote.run(
                f"{secrets_cmd} set {shlex.quote(key)} - 2>&1",
                input=f"{secrets_pass}\n{value}\n",
            )
            self._log_result(f"secrets.sh set {key}", result)
            if result.success:
                secrets_set += 1
                self.progress.ok(f"    {key} set")
            else:
                secrets_failed += 1
                self.progress.fail(f"    Failed: {result.stderr or result.stdout}")

        if secrets_failed > 0:
            self.progress.fail(f"Set {secrets_set} secrets, {secrets_failed} failed")
            self.progress.info(f"  Log: {self.log_file}")
            self.state.complete_phase(
                Phase.SECRETS, PhaseStatus.FAILED, f"{secrets_failed} failed", start
            )
            return False

        self.progress.ok(f"Set {secrets_set} secrets")
        self.state.complete_phase(Phase.SECRETS, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
        return True

    def _phase_deployment(self) -> bool:
        """Phase 5: Deployment."""
        self.progress.phase(5, "Deployment", self.config.timeouts.deployment_base)
        start = self.state.start_phase(Phase.DEPLOY_BASE)

        if self.dry_run:
            if self.deploy_type in ["base", "full"]:
                self.progress.dry_run("Would run deploy-edge-server.sh")
            if self.deploy_type == "security":
                self.progress.dry_run("Would run deploy-security.sh")
            self.state.complete_phase(Phase.DEPLOY_BASE, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        self._create_snapshot(
            "deploy", "Before deployment", paths=["/opt/edge-server"], include_docker=True
        )

        deploy = self.config.deploy
        deploy_env = (
            "export BATCH_MODE=true"
            f" INSTALL_HOMEASSISTANT={str(deploy.install_homeassistant).lower()}"
            f" INSTALL_MQTT={str(deploy.install_mqtt).lower()}"
        )
        if self.config.deploy.edge_server_dir:
            deploy_env += f" EDGE_SERVER_DIR={shlex.quote(self.config.deploy.edge_server_dir)}"
        if self.config.deploy.edge_storage_dir:
            deploy_env += f" EDGE_STORAGE_DIR={shlex.quote(self.config.deploy.edge_storage_dir)}"

        # Deploy base stack ("full" = base stack, which already includes Frigate)
        if self.deploy_type in ["base", "full"]:
            if not self._run_deploy_script(
                "deploy-edge-server.sh",
                "base stack",
                f"Deploying base stack ({self._stack_description()})...",
                deploy_env,
                self.config.timeouts.deployment_base,
            ):
                self.state.complete_phase(Phase.DEPLOY_BASE, PhaseStatus.FAILED, "Failed", start)
                self._maybe_rollback()
                return False

        # Security-only stack (alternative layout; overlaps with the base stack)
        if self.deploy_type == "security":
            sec_start = self.state.start_phase(Phase.DEPLOY_SECURITY)
            if not self._run_deploy_script(
                "deploy-security.sh",
                "security stack",
                "Deploying security stack (Frigate NVR)...",
                deploy_env,
                self.config.timeouts.deployment_security,
            ):
                self.state.complete_phase(
                    Phase.DEPLOY_SECURITY, PhaseStatus.FAILED, "Failed", sec_start
                )
                self._maybe_rollback()
                return False
            self.state.complete_phase(Phase.DEPLOY_SECURITY, PhaseStatus.SUCCESS, "OK", sec_start)

        self.state.complete_phase(Phase.DEPLOY_BASE, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
        return True

    def _run_deploy_script(
        self, script: str, label: str, banner: str, deploy_env: str, timeout: int
    ) -> bool:
        """Run a deploy script remotely with secrets loaded into its environment."""
        self._log(f"Starting {label} deployment")
        self.progress.info(banner)
        self.progress.info(
            f"  Tail log: {self._ssh_command()} tail -f /var/log/edge-server-deploy.log"
        )

        secrets_pass = self._secrets_pass or os.environ.get("SECRETS_PASSWORD", "")
        secrets_dir = remote_path(self.secrets_dir)
        # The password is read from stdin, used to decrypt, then dropped before
        # the deploy script runs, so it never shows up in a process listing.
        command = (
            f"cd {self.REMOTE_DIR} && "
            f"IFS= read -r SECRETS_PASSWORD && export SECRETS_PASSWORD && "
            f'eval "$(SECRETS_DIR={secrets_dir} ./secrets.sh export 2>/dev/null)" && '
            f"unset SECRETS_PASSWORD && "
            f"{deploy_env} && sudo -E ./{script}"
        )
        result = self._remote.run(command, timeout=timeout, input=f"{secrets_pass}\n")

        self._log_result(script, result)
        if not result.success:
            self._log(f"{label.capitalize()} deployment failed", "error")
            self.progress.fail(f"{label.capitalize()} deployment failed")
            # Show last output lines on failure
            if result.stdout:
                self.progress.info("  Output:")
                for line in result.stdout.split("\n")[-20:]:
                    if line.strip():
                        self.progress.info(f"    {line}")
            if result.stderr:
                self.progress.info(f"  {result.stderr[:500]}")
            self.progress.info(f"  Full log: {self.log_file}")
            return False
        self._log(f"{label.capitalize()} deployed successfully")
        self.progress.ok(f"{label.capitalize()} deployed")
        return True

    def _phase_verification(self) -> bool:
        """Phase 6: Verification."""
        self.progress.phase(6, "Verification", self.config.timeouts.verification)
        start = self.state.start_phase(Phase.VERIFY)

        if self.dry_run:
            self.progress.dry_run("Would verify services are running")
            self.state.complete_phase(Phase.VERIFY, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        self.progress.info("Waiting 15s for services to initialize...")
        time.sleep(15)

        passed = 0
        failed = 0

        # Check Tailscale
        if self.config.services.verify_tailscale:
            health = self._checker.check_tailscale()
            if health.status == HealthStatus.HEALTHY:
                self.progress.ok(f"Tailscale: {health.message}")
                passed += 1
            else:
                self.progress.warn(f"Tailscale: {health.message}")
                failed += 1

        # Check Home Assistant
        if self.config.services.verify_homeassistant and self.config.deploy.install_homeassistant:
            health = self._checker.check_homeassistant(
                self.target,
                self.config.services.ha_port,
            )
            if health.status == HealthStatus.HEALTHY:
                self.progress.ok(
                    f"Home Assistant: http://{self.target}:{self.config.services.ha_port}"
                )
                passed += 1
            else:
                self.progress.warn(f"Home Assistant: {health.message}")
                failed += 1

        # Check MQTT
        if self.config.services.verify_mqtt and self.config.deploy.install_mqtt:
            health = self._checker.check_mqtt(
                port=self.config.services.mqtt_port,
                user=self._secrets.get("MQTT_USER") or None,
                password=self._secrets.get("MQTT_PASS") or None,
            )
            if health.status == HealthStatus.HEALTHY:
                self.progress.ok(f"MQTT: {self.target}:{self.config.services.mqtt_port}")
                passed += 1
            else:
                self.progress.warn(f"MQTT: {health.message}")
                failed += 1

        # Check Frigate (if security deployed)
        if self.config.services.verify_frigate and self.deploy_type in ["security", "full"]:
            health = self._checker.check_frigate(
                self.target,
                self.config.services.frigate_port,
            )
            if health.status == HealthStatus.HEALTHY:
                self.progress.ok(
                    f"Frigate: http://{self.target}:{self.config.services.frigate_port}"
                )
                passed += 1
            else:
                self.progress.warn(f"Frigate: {health.message}")
                failed += 1

        status = PhaseStatus.SUCCESS if failed == 0 else PhaseStatus.FAILED
        self.state.complete_phase(Phase.VERIFY, status, f"{passed} passed, {failed} failed", start)
        self.progress.phase_summary()

        return failed == 0

    def _print_success(self) -> None:
        """Print success footer."""
        services = self.config.services
        urls = {"Frigate": f"http://{self.target}:{services.frigate_port}"}
        if self.config.deploy.install_homeassistant:
            urls["Home Assistant"] = f"http://{self.target}:{services.ha_port}"
        if self.config.deploy.install_mqtt:
            urls["MQTT"] = f"{self.target}:{services.mqtt_port}"
        urls["SSH"] = self._ssh_command()

        self.progress.footer(True, urls)

    def _stack_description(self) -> str:
        parts = ["Tailscale", "Frigate"]
        if self.config.deploy.install_mqtt:
            parts.append("MQTT")
        if self.config.deploy.install_homeassistant:
            parts.append("Home Assistant")
        return ", ".join(parts)

    def _create_snapshot(self, phase: str, description: str, **kwargs: object) -> None:
        """Create a pre-change snapshot if rollback is configured."""
        rollback = self.config.rollback
        if not (rollback.enabled and rollback.snapshot_before_deploy and self.snapshots):
            return
        self.progress.info(f"Creating {phase.replace('_', ' ')} snapshot...")
        if self.snapshots.create(phase, description, **kwargs):  # type: ignore[arg-type,unused-ignore]
            self._snapshot_taken = True
            self.progress.ok("Snapshot created")
        else:
            self.progress.warn("Snapshot failed - rollback will not be available")

    def _maybe_rollback(self) -> None:
        """Roll back to the latest snapshot if enabled and one was taken this run."""
        rollback = self.config.rollback
        if self.no_rollback or not rollback.enabled or not rollback.restore_on_failure:
            return
        if not self._snapshot_taken:
            return
        self._rollback()

    def _rollback(self) -> None:
        """Perform rollback on failure."""
        self.progress.warn("Attempting rollback...")
        if self.snapshots:
            if self.snapshots.restore("latest"):
                self.progress.ok("Rollback complete")
            else:
                self.progress.fail("Rollback failed")

    def _confirm(self, prompt: str) -> bool:
        """Ask for confirmation in manual mode."""
        if self.mode == "auto":
            return True
        response = input(f"  {prompt} [Y/n] ").strip().lower()
        return response in ["", "y", "yes"]

    def _prompt(
        self,
        prompt: str,
        default: str = "",
        secret: bool = False,
        env_key: str | None = None,
    ) -> str:
        """Prompt for input in manual mode; read env/secrets file in auto mode."""
        if self.mode == "auto":
            key = env_key or prompt.upper().replace(" ", "_")
            # Check env vars first, then stored secrets, then default
            return os.environ.get(key, self._secrets.get(key, default))

        display_default = f" [{default}]" if default and not secret else ""
        if secret:
            value = getpass.getpass(f"  {prompt}{display_default}: ")
        else:
            value = input(f"  {prompt}{display_default}: ")
        return value.strip() or default

    def _load_secrets_file(self) -> None:
        """Load secrets from file."""
        if not self.secrets_file or not self.secrets_file.exists():
            return
        for line in self.secrets_file.read_text().split("\n"):
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                value = value.strip()
                # Accept KEY="value" / KEY='value' as written by most .env tools
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                self._secrets[key.strip()] = value


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Bootstrap edge server deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive manual mode
  %(prog)s 192.168.1.100

  # Automated with secrets file
  %(prog)s --auto --secrets-file ~/.edge-secrets.env 192.168.1.100

  # Dry run
  %(prog)s --dry-run 192.168.1.100
""",
    )

    parser.add_argument("target", help="Target IP address")
    parser.add_argument("--auto", action="store_true", help="Non-interactive mode")
    parser.add_argument("--config", type=Path, help="Config file path")
    parser.add_argument("--secrets-file", type=Path, help="Secrets file path")
    parser.add_argument(
        "--deploy", choices=["base", "security", "full"], default="full", help="Deployment type"
    )
    parser.add_argument("--user", help="SSH user (default: from config or current user)")
    parser.add_argument("--key", "-i", help="SSH private key file")
    parser.add_argument(
        "--no-host-key-check",
        action="store_true",
        help="Disable SSH host key checking (StrictHostKeyChecking=no)",
    )
    parser.add_argument("--skip-init", action="store_true", help="Skip initial setup")
    parser.add_argument("--no-rollback", action="store_true", help="Disable automatic rollback")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--timeout", action="append", help="Override timeout (PHASE=SECONDS)")

    args = parser.parse_args()

    # Load config
    config = Config.load(args.config)
    if args.user:
        config.ssh.user = args.user
    elif not config.ssh.user:
        config.ssh.user = os.environ.get("USER", "root")
    if args.key:
        config.ssh.key_file = args.key
    if args.no_host_key_check:
        config.ssh.strict_host_key_checking = "no"

    # Apply timeout overrides
    valid_phases = {f.name for f in fields(config.timeouts)}
    for override in args.timeout or []:
        phase, sep, seconds = override.partition("=")
        phase = phase.strip().lower()
        if not sep or phase not in valid_phases:
            parser.error(
                f"invalid --timeout {override!r}; expected PHASE=SECONDS with PHASE one of: "
                + ", ".join(sorted(valid_phases))
            )
        try:
            value = int(seconds)
        except ValueError:
            parser.error(f"invalid --timeout {override!r}; SECONDS must be an integer")
        if value <= 0:
            parser.error(f"invalid --timeout {override!r}; SECONDS must be positive")
        setattr(config.timeouts, phase, value)

    # Create and run bootstrap
    bootstrap = Bootstrap(
        target=args.target,
        config=config,
        mode="auto" if args.auto else "manual",
        deploy_type=args.deploy,
        dry_run=args.dry_run,
        skip_init=args.skip_init,
        no_rollback=args.no_rollback,
        secrets_file=args.secrets_file,
    )

    return 0 if bootstrap.run() else 1


if __name__ == "__main__":
    sys.exit(main())
