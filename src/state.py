"""State machine for bootstrap phases."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class PhaseStatus(Enum):
    """Status of a phase."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class Phase(Enum):
    """Bootstrap phases."""

    INIT = "init"
    DISCOVERY = "discovery"
    PREREQUISITES = "prerequisites"
    INITIAL_SETUP = "initial_setup"
    REBOOT = "reboot"
    SECRETS = "secrets"
    DEPLOY_BASE = "deploy_base"
    DEPLOY_SECURITY = "deploy_security"
    VERIFY = "verify"
    DONE = "done"


@dataclass
class PhaseResult:
    """Result of a phase execution."""

    phase: Phase
    status: PhaseStatus
    message: str
    started_at: float
    completed_at: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def elapsed(self) -> float:
        return self.completed_at - self.started_at


@dataclass
class ValidationRule:
    """A validation rule for phase transitions."""

    name: str
    check: Callable[[], bool]
    error_message: str


@dataclass
class PhaseDefinition:
    """Definition of a phase including validations and rollback."""

    phase: Phase
    pre_validations: list[ValidationRule] = field(default_factory=list)
    post_validations: list[ValidationRule] = field(default_factory=list)
    rollback: Callable[[], bool] | None = None
    timeout: int = 60


class StateMachine:
    """State machine for managing bootstrap phases."""

    PHASE_ORDER = [
        Phase.INIT,
        Phase.DISCOVERY,
        Phase.PREREQUISITES,
        Phase.INITIAL_SETUP,
        Phase.REBOOT,
        Phase.SECRETS,
        Phase.DEPLOY_BASE,
        Phase.DEPLOY_SECURITY,
        Phase.VERIFY,
        Phase.DONE,
    ]

    def __init__(self, state_file: Path | None = None):
        self.state_file = state_file
        self.current_phase = Phase.INIT
        self.results: dict[Phase, PhaseResult] = {}
        self.phase_definitions: dict[Phase, PhaseDefinition] = {}
        self._started_at = time.monotonic()

        if state_file and state_file.exists():
            self._load_state()

    def register_phase(self, definition: PhaseDefinition) -> None:
        """Register a phase definition."""
        self.phase_definitions[definition.phase] = definition

    def _load_state(self) -> None:
        """Load state from file."""
        if not self.state_file or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            self.current_phase = Phase(data.get("current_phase", "init"))
            for phase_name, result_data in data.get("results", {}).items():
                phase = Phase(phase_name)
                self.results[phase] = PhaseResult(
                    phase=phase,
                    status=PhaseStatus(result_data["status"]),
                    message=result_data["message"],
                    started_at=result_data["started_at"],
                    completed_at=result_data["completed_at"],
                    details=result_data.get("details", {}),
                    error=result_data.get("error"),
                )
        except Exception:
            pass

    def _save_state(self) -> None:
        """Save state to file."""
        if not self.state_file:
            return
        data = {
            "current_phase": self.current_phase.value,
            "timestamp": datetime.now().isoformat(),
            "results": {
                phase.value: {
                    "status": result.status.value,
                    "message": result.message,
                    "started_at": result.started_at,
                    "completed_at": result.completed_at,
                    "details": result.details,
                    "error": result.error,
                }
                for phase, result in self.results.items()
            },
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, indent=2))

    def validate_pre(self, phase: Phase) -> tuple:
        """Validate pre-conditions for a phase. Returns (valid, failed_rules)."""
        definition = self.phase_definitions.get(phase)
        if not definition:
            return True, []

        failed = []
        for rule in definition.pre_validations:
            try:
                if not rule.check():
                    failed.append(rule)
            except Exception as e:
                failed.append(
                    ValidationRule(
                        name=rule.name,
                        check=rule.check,
                        error_message=f"{rule.error_message}: {e}",
                    )
                )
        return len(failed) == 0, failed

    def validate_post(self, phase: Phase) -> tuple:
        """Validate post-conditions for a phase. Returns (valid, failed_rules)."""
        definition = self.phase_definitions.get(phase)
        if not definition:
            return True, []

        failed = []
        for rule in definition.post_validations:
            try:
                if not rule.check():
                    failed.append(rule)
            except Exception as e:
                failed.append(
                    ValidationRule(
                        name=rule.name,
                        check=rule.check,
                        error_message=f"{rule.error_message}: {e}",
                    )
                )
        return len(failed) == 0, failed

    def start_phase(self, phase: Phase) -> float:
        """Mark a phase as started. Returns start time."""
        self.current_phase = phase
        start_time = time.monotonic()
        self._save_state()
        return start_time

    def complete_phase(
        self,
        phase: Phase,
        status: PhaseStatus,
        message: str,
        started_at: float,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> PhaseResult:
        """Mark a phase as completed."""
        result = PhaseResult(
            phase=phase,
            status=status,
            message=message,
            started_at=started_at,
            completed_at=time.monotonic(),
            details=details or {},
            error=error,
        )
        self.results[phase] = result
        self._save_state()
        return result

    def can_proceed(self, from_phase: Phase, to_phase: Phase) -> bool:
        """Check if we can proceed from one phase to another."""
        from_idx = self.PHASE_ORDER.index(from_phase)
        to_idx = self.PHASE_ORDER.index(to_phase)

        # Can only proceed to next phase or skip
        if to_idx <= from_idx:
            return False

        # Previous phase must be success or skipped
        result = self.results.get(from_phase)
        if result and result.status not in [PhaseStatus.SUCCESS, PhaseStatus.SKIPPED]:
            return False

        return True

    def next_phase(self) -> Phase | None:
        """Get the next phase to execute."""
        current_idx = self.PHASE_ORDER.index(self.current_phase)
        if current_idx + 1 < len(self.PHASE_ORDER):
            return self.PHASE_ORDER[current_idx + 1]
        return None

    def rollback_phase(self, phase: Phase) -> bool:
        """Execute rollback for a phase."""
        definition = self.phase_definitions.get(phase)
        if not definition or not definition.rollback:
            return True

        try:
            success = definition.rollback()
            if success:
                result = self.results.get(phase)
                if result:
                    result.status = PhaseStatus.ROLLED_BACK
                    self._save_state()
            return success
        except Exception:
            return False

    def rollback_to(self, target_phase: Phase) -> list[Phase]:
        """Rollback all phases back to target. Returns rolled back phases."""
        target_idx = self.PHASE_ORDER.index(target_phase)
        current_idx = self.PHASE_ORDER.index(self.current_phase)

        rolled_back = []
        for idx in range(current_idx, target_idx, -1):
            phase = self.PHASE_ORDER[idx]
            if self.rollback_phase(phase):
                rolled_back.append(phase)

        self.current_phase = target_phase
        self._save_state()
        return rolled_back

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all phase results."""
        return {
            "current_phase": self.current_phase.value,
            "total_elapsed": time.monotonic() - self._started_at,
            "phases": {
                phase.value: {
                    "status": result.status.value,
                    "elapsed": result.elapsed,
                    "message": result.message,
                }
                for phase, result in self.results.items()
            },
        }

    def is_complete(self) -> bool:
        """Check if all phases are complete."""
        return self.current_phase == Phase.DONE

    def has_failures(self) -> bool:
        """Check if any phase failed."""
        return any(r.status == PhaseStatus.FAILED for r in self.results.values())
