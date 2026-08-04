"""Main bootstrap orchestration."""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import Config
from progress import Progress
from services import HealthStatus, ServiceChecker
from snapshot import SnapshotManager
from ssh import SSHClient, SSHResult
from state import Phase, PhaseStatus, StateMachine

DEFAULT_LOG_DIR = Path.home() / ".edge-server" / "logs"
DEFAULT_SECRETS_DIR = "$HOME/.edge-server-secrets"


class Bootstrap:
    """Main bootstrap orchestrator."""

    SCRIPTS = [
        "initial-setup.sh",
        "deploy-edge-server.sh",
        "deploy-security.sh",
        "secrets.sh",
        "env.example",
    ]

    REMOTE_DIR = "/tmp/edge-server-setup"

    def __init__(
        self,
        target: str,
        config: Config,
        mode: str = "manual",
        deploy_type: str = "full",
        dry_run: bool = False,
        skip_init: bool = False,
        no_rollback: bool = False,
        secrets_file: Optional[Path] = None,
        script_dir: Optional[Path] = None,
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
        self.ssh: Optional[SSHClient] = None
        self.state = StateMachine()
        self.checker: Optional[ServiceChecker] = None
        self.snapshots: Optional[SnapshotManager] = None

        self._secrets: dict = {}
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Setup logging to local file."""
        log_dir = (
            Path(self.config.logging.log_dir) if self.config.logging.log_dir else DEFAULT_LOG_DIR
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"bootstrap_{self.target}_{timestamp}.log"

        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[
                logging.FileHandler(log_file),
            ],
        )
        self.logger = logging.getLogger("bootstrap")
        self.log_file = log_file
        self.secrets_dir = self.config.secrets.secrets_dir or DEFAULT_SECRETS_DIR
        self.logger.info(f"Bootstrap started for target: {self.target}")
        self.logger.info(f"Mode: {self.mode}, Deploy type: {self.deploy_type}")
        self.logger.info(f"Secrets dir: {self.secrets_dir}")

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
            self._log(f"Unexpected error: {e}", "error")
            self.progress.fail(f"Unexpected error: {e}")
            self.progress.info(f"Log saved to: {self.log_file}")
            if not self.no_rollback and self.config.rollback.restore_on_failure:
                self._rollback()
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
        self.snapshots = SnapshotManager(self.ssh)

        self.state.complete_phase(Phase.DISCOVERY, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
        return True

    def _phase_prerequisites(self) -> bool:
        """Phase 2: Prerequisites."""
        self.progress.phase(2, "Prerequisites", self.config.timeouts.prerequisites)
        start = self.state.start_phase(Phase.PREREQUISITES)

        # Check local tools
        for tool in ["ssh", "scp"]:
            if os.system(f"which {tool} > /dev/null 2>&1") == 0:
                self.progress.ok(f"Local tool: {tool}")
            else:
                self.progress.fail(f"Missing local tool: {tool}")
                return False

        if self.dry_run:
            self.progress.dry_run(f"Would copy scripts to {self.target}:{self.REMOTE_DIR}")
            self.state.complete_phase(Phase.PREREQUISITES, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        # Copy scripts
        self.progress.info(f"Copying scripts to {self.target}:{self.REMOTE_DIR}...")
        self.ssh.mkdir(self.REMOTE_DIR)
        scripts = [self.script_dir / s for s in self.SCRIPTS if (self.script_dir / s).exists()]
        self.progress.info(f"  Scripts: {', '.join(s.name for s in scripts)}")
        if not self.ssh.copy_files(scripts, self.REMOTE_DIR):
            self.progress.fail("Failed to copy scripts")
            return False
        self.ssh.run(f"chmod +x {self.REMOTE_DIR}/*.sh")
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
        static_ip = self._prompt("Static IP (e.g., 192.168.1.100/24)", "")

        if static_ip:
            gateway = self._prompt("Gateway", self.config.network.default_gateway)
            dns = self._prompt("DNS server", self.config.network.default_dns)
        else:
            gateway = ""
            dns = ""
            self.progress.info("  Skipping network configuration")

        # Create snapshot before changes
        if self.config.rollback.snapshot_before_deploy:
            self.progress.info("Creating pre-setup snapshot...")
            self.snapshots.create(
                "initial_setup",
                "Before initial setup",
                paths=["/etc/netplan"],
            )
            self.progress.ok("Snapshot created")

        # Run initial setup
        env = {}
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

        result = self.ssh.run_script(
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
            self.ssh.reboot()

        # Wait for host
        self.progress.info("Waiting for target to come back online...")
        timeout = self.config.timeouts.reboot_wait
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < timeout:
            elapsed = time.monotonic() - start_wait
            self.progress.waiting("Waiting for host", elapsed, timeout)
            if self.ssh.test_connection():
                self.progress.clear_line()
                self.progress.ok(f"Target back online ({elapsed:.0f}s)")
                break
            time.sleep(self.config.timeouts.probe_interval)
        else:
            self.progress.clear_line()
            self.progress.fail("Target did not come back online")
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
                "Tailscale auth key", "", secret=True
            )
            self._secrets["MQTT_USER"] = self._prompt("MQTT username", "homeassistant")
            self._secrets["MQTT_PASS"] = self._prompt("MQTT password", "", secret=True)
            self._secrets["REOLINK_USER"] = self._prompt("Camera username", "admin")
            self._secrets["REOLINK_PASS"] = self._prompt("Camera password", "", secret=True)

        if self.dry_run:
            self.progress.dry_run("Would configure secrets on target")
            self.state.complete_phase(Phase.SECRETS, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        # Configure secrets on target
        # Check if secrets already initialized
        check_result = self.ssh.run(f"[ -f {self.secrets_dir}/secrets.enc ]")
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

        if secrets_exist:
            self.progress.ok("Secrets storage already initialized")
        else:
            self.progress.info("Initializing secrets storage...")
            result = self.ssh.run(
                f"cd {self.REMOTE_DIR} && SECRETS_DIR=\"{self.secrets_dir}\" SECRETS_PASSWORD='{secrets_pass}' "
                f"./secrets.sh init <<< $'{secrets_pass}\\n{secrets_pass}' 2>&1"
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

        # Store password for subsequent set commands
        os.environ["SECRETS_PASSWORD"] = secrets_pass

        secrets_set = 0
        secrets_failed = 0
        secrets_pass = os.environ.get("SECRETS_PASSWORD", "")
        self.progress.info("Setting secrets...")
        for key, value in self._secrets.items():
            if value:
                self.progress.info(f"  Setting {key}...")
                env_prefix = f'SECRETS_DIR="{self.secrets_dir}"'
                if secrets_pass:
                    env_prefix += f" SECRETS_PASSWORD='{secrets_pass}'"
                cmd = f"cd {self.REMOTE_DIR} && {env_prefix} ./secrets.sh set {key} '{value}' 2>&1"
                result = self.ssh.run(cmd)
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
            if self.deploy_type in ["security", "full"]:
                self.progress.dry_run("Would run deploy-security.sh")
            self.state.complete_phase(Phase.DEPLOY_BASE, PhaseStatus.SUCCESS, "Dry run", start)
            return True

        # Create snapshot before deployment
        if self.config.rollback.snapshot_before_deploy:
            self.progress.info("Creating pre-deployment snapshot...")
            self.snapshots.create(
                "deploy",
                "Before deployment",
                paths=["/opt/edge-server"],
                include_docker=True,
            )
            self.progress.ok("Snapshot created")

        # Deploy base stack
        if self.deploy_type in ["base", "full"]:
            self._log("Starting base stack deployment")
            self.progress.info("Deploying base stack (Tailscale, MQTT, Home Assistant)...")
            timeout = self.config.timeouts.deployment_base

            result = self.ssh.run(
                f"cd {self.REMOTE_DIR} && "
                f"eval $(./secrets.sh export 2>/dev/null) && "
                f"sudo -E ./deploy-edge-server.sh",
                timeout=timeout,
            )

            self._log_result("deploy-edge-server.sh", result)
            if not result.success:
                self._log("Base stack deployment failed", "error")
                self.progress.fail("Base stack deployment failed")
                # Show last output lines on failure
                if result.stdout:
                    self.progress.info("  Output:")
                    for line in result.stdout.split("\n")[-20:]:
                        if line.strip():
                            self.progress.info(f"    {line}")
                self.progress.info(f"  Full log: {self.log_file}")
                return False
            self._log("Base stack deployed successfully")
            self.progress.ok("Base stack deployed")

        # Deploy security stack
        if self.deploy_type in ["security", "full"]:
            self.state.start_phase(Phase.DEPLOY_SECURITY)
            self._log("Starting security stack deployment")
            self.progress.info("Deploying security stack (Frigate NVR)...")
            timeout = self.config.timeouts.deployment_security

            result = self.ssh.run(
                f"cd {self.REMOTE_DIR} && "
                f"eval $(./secrets.sh export 2>/dev/null) && "
                f"sudo -E ./deploy-security.sh",
                timeout=timeout,
            )

            self._log_result("deploy-security.sh", result)
            if not result.success:
                self._log("Security stack deployment failed", "error")
                # Show last output lines on failure
                if result.stdout:
                    self.progress.info("  Output:")
                    for line in result.stdout.split("\n")[-20:]:
                        if line.strip():
                            self.progress.info(f"    {line}")
                self.progress.fail("Security stack deployment failed")
                self.progress.info(f"  Full log: {self.log_file}")
                return False
            self._log("Security stack deployed successfully")
            self.progress.ok("Security stack deployed")

        self.state.complete_phase(Phase.DEPLOY_BASE, PhaseStatus.SUCCESS, "OK", start)
        self.progress.phase_summary()
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
            health = self.checker.check_tailscale()
            if health.status == HealthStatus.HEALTHY:
                self.progress.ok(f"Tailscale: {health.message}")
                passed += 1
            else:
                self.progress.warn(f"Tailscale: {health.message}")
                failed += 1

        # Check Home Assistant
        if self.config.services.verify_homeassistant:
            health = self.checker.check_homeassistant(
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
        if self.config.services.verify_mqtt:
            health = self.checker.check_mqtt()
            if health.status == HealthStatus.HEALTHY:
                self.progress.ok(f"MQTT: {self.target}:{self.config.services.mqtt_port}")
                passed += 1
            else:
                self.progress.warn(f"MQTT: {health.message}")
                failed += 1

        # Check Frigate (if security deployed)
        if self.config.services.verify_frigate and self.deploy_type in ["security", "full"]:
            health = self.checker.check_frigate(
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
        urls = {
            "Home Assistant": f"http://{self.target}:{self.config.services.ha_port}",
            "MQTT": f"{self.target}:{self.config.services.mqtt_port}",
            "SSH": f"ssh {self.config.ssh.user}@{self.target}",
        }
        if self.deploy_type in ["security", "full"]:
            urls["Frigate"] = f"http://{self.target}:{self.config.services.frigate_port}"

        self.progress.footer(True, urls)

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

    def _prompt(self, prompt: str, default: str = "", secret: bool = False) -> str:
        """Prompt for input in manual mode."""
        if self.mode == "auto":
            return self._secrets.get(prompt.upper().replace(" ", "_"), default)

        display_default = f" [{default}]" if default and not secret else ""
        if secret:
            import getpass

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
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                self._secrets[key.strip()] = value.strip()


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
    parser.add_argument("--user", default=os.environ.get("USER", "root"), help="SSH user")
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
    config.ssh.user = args.user
    if args.key:
        config.ssh.key_file = args.key
    if args.no_host_key_check:
        config.ssh.strict_host_key_checking = "no"

    # Apply timeout overrides
    if args.timeout:
        for override in args.timeout:
            if "=" in override:
                phase, _, seconds = override.partition("=")
                phase = phase.lower()
                try:
                    setattr(config.timeouts, phase, int(seconds))
                except (AttributeError, ValueError):
                    pass

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
