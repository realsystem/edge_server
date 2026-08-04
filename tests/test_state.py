"""Tests for state module."""

import tempfile
from pathlib import Path

import pytest

from state import (
    Phase,
    PhaseStatus,
    PhaseResult,
    StateMachine,
    PhaseDefinition,
    ValidationRule,
)


class TestPhase:
    """Tests for Phase enum."""

    def test_phase_values(self):
        """Test phase values."""
        assert Phase.INIT.value == "init"
        assert Phase.DISCOVERY.value == "discovery"
        assert Phase.DONE.value == "done"


class TestPhaseStatus:
    """Tests for PhaseStatus enum."""

    def test_status_values(self):
        """Test status values."""
        assert PhaseStatus.PENDING.value == "pending"
        assert PhaseStatus.SUCCESS.value == "success"
        assert PhaseStatus.FAILED.value == "failed"


class TestPhaseResult:
    """Tests for PhaseResult dataclass."""

    def test_elapsed_time(self):
        """Test elapsed time calculation."""
        result = PhaseResult(
            phase=Phase.DISCOVERY,
            status=PhaseStatus.SUCCESS,
            message="OK",
            started_at=100.0,
            completed_at=105.5,
        )
        assert result.elapsed == 5.5


class TestStateMachine:
    """Tests for StateMachine."""

    def test_phase_order(self):
        """Test phase order is correct."""
        sm = StateMachine()
        assert sm.PHASE_ORDER[0] == Phase.INIT
        assert sm.PHASE_ORDER[-1] == Phase.DONE
        assert Phase.DISCOVERY in sm.PHASE_ORDER

    def test_initial_state(self):
        """Test initial state."""
        sm = StateMachine()
        assert sm.current_phase == Phase.INIT
        assert len(sm.results) == 0

    def test_start_phase(self):
        """Test starting a phase."""
        sm = StateMachine()
        start = sm.start_phase(Phase.DISCOVERY)
        assert sm.current_phase == Phase.DISCOVERY
        assert start > 0

    def test_complete_phase(self):
        """Test completing a phase."""
        sm = StateMachine()
        start = sm.start_phase(Phase.DISCOVERY)
        result = sm.complete_phase(
            Phase.DISCOVERY,
            PhaseStatus.SUCCESS,
            "Completed",
            start,
            details={"os": "Ubuntu"},
        )

        assert result.phase == Phase.DISCOVERY
        assert result.status == PhaseStatus.SUCCESS
        assert result.details["os"] == "Ubuntu"
        assert sm.results[Phase.DISCOVERY] == result

    def test_next_phase(self):
        """Test getting next phase."""
        sm = StateMachine()
        assert sm.next_phase() == Phase.DISCOVERY

        sm.current_phase = Phase.VERIFY
        assert sm.next_phase() == Phase.DONE

        sm.current_phase = Phase.DONE
        assert sm.next_phase() is None

    def test_can_proceed(self):
        """Test phase transition validation."""
        sm = StateMachine()

        # Complete current phase first
        start = sm.start_phase(Phase.INIT)
        sm.complete_phase(Phase.INIT, PhaseStatus.SUCCESS, "OK", start)

        # Can proceed after success
        assert sm.can_proceed(Phase.INIT, Phase.DISCOVERY)

        # Can't go backwards
        assert not sm.can_proceed(Phase.DISCOVERY, Phase.INIT)

        # Can't proceed if failed
        sm2 = StateMachine()
        start = sm2.start_phase(Phase.INIT)
        sm2.complete_phase(Phase.INIT, PhaseStatus.FAILED, "Failed", start)
        assert not sm2.can_proceed(Phase.INIT, Phase.DISCOVERY)

    def test_is_complete(self):
        """Test completion check."""
        sm = StateMachine()
        assert not sm.is_complete()

        sm.current_phase = Phase.DONE
        assert sm.is_complete()

    def test_has_failures(self):
        """Test failure check."""
        sm = StateMachine()
        assert not sm.has_failures()

        start = sm.start_phase(Phase.DISCOVERY)
        sm.complete_phase(Phase.DISCOVERY, PhaseStatus.FAILED, "Failed", start)
        assert sm.has_failures()

    def test_save_and_load_state(self):
        """Test state persistence."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_file = Path(f.name)

        # Create and save state
        sm1 = StateMachine(state_file)
        start = sm1.start_phase(Phase.DISCOVERY)
        sm1.complete_phase(Phase.DISCOVERY, PhaseStatus.SUCCESS, "OK", start)

        # Load in new instance
        sm2 = StateMachine(state_file)
        assert sm2.current_phase == Phase.DISCOVERY
        assert Phase.DISCOVERY in sm2.results
        assert sm2.results[Phase.DISCOVERY].status == PhaseStatus.SUCCESS

    def test_validation_rules(self):
        """Test pre/post validation rules."""
        sm = StateMachine()

        # Register phase with validations
        sm.register_phase(PhaseDefinition(
            phase=Phase.DISCOVERY,
            pre_validations=[
                ValidationRule("check_true", lambda: True, "Should pass"),
            ],
            post_validations=[
                ValidationRule("check_false", lambda: False, "Should fail"),
            ],
        ))

        # Pre-validation passes
        valid, failed = sm.validate_pre(Phase.DISCOVERY)
        assert valid is True
        assert len(failed) == 0

        # Post-validation fails
        valid, failed = sm.validate_post(Phase.DISCOVERY)
        assert valid is False
        assert len(failed) == 1
        assert failed[0].name == "check_false"

    def test_rollback(self):
        """Test rollback functionality."""
        rollback_called = []

        sm = StateMachine()
        sm.register_phase(PhaseDefinition(
            phase=Phase.DISCOVERY,
            rollback=lambda: rollback_called.append(Phase.DISCOVERY) or True,
        ))
        sm.register_phase(PhaseDefinition(
            phase=Phase.PREREQUISITES,
            rollback=lambda: rollback_called.append(Phase.PREREQUISITES) or True,
        ))

        # Set up state
        sm.current_phase = Phase.PREREQUISITES
        start = sm.start_phase(Phase.DISCOVERY)
        sm.complete_phase(Phase.DISCOVERY, PhaseStatus.SUCCESS, "OK", start)
        start = sm.start_phase(Phase.PREREQUISITES)
        sm.complete_phase(Phase.PREREQUISITES, PhaseStatus.SUCCESS, "OK", start)

        # Rollback to INIT
        rolled = sm.rollback_to(Phase.INIT)

        assert Phase.PREREQUISITES in rolled
        assert Phase.DISCOVERY in rolled
        assert sm.current_phase == Phase.INIT

    def test_get_summary(self):
        """Test getting summary."""
        sm = StateMachine()
        start = sm.start_phase(Phase.DISCOVERY)
        sm.complete_phase(Phase.DISCOVERY, PhaseStatus.SUCCESS, "OK", start)

        summary = sm.get_summary()
        assert "current_phase" in summary
        assert "total_elapsed" in summary
        assert "phases" in summary
        assert "discovery" in summary["phases"]
