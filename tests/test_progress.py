"""Tests for progress module."""

import io
import sys

import pytest

from progress import Color, Progress, Status


class TestColor:
    """Tests for Color class."""

    def test_color_codes(self):
        """Test color codes are defined (or empty if disabled)."""
        # Colors may be disabled from previous test, just check they exist
        assert hasattr(Color, "RED")
        assert hasattr(Color, "GREEN")
        assert hasattr(Color, "RESET")

    def test_disable_colors(self):
        """Test disabling colors."""
        # Save original
        orig_red = Color.RED

        Color.disable()
        assert Color.RED == ""
        assert Color.GREEN == ""
        assert Color.RESET == ""

        # Restore
        Color.RED = orig_red
        Color.GREEN = "\033[0;32m"
        Color.RESET = "\033[0m"


class TestStatus:
    """Tests for Status enum."""

    def test_status_values(self):
        """Test status symbols."""
        assert Status.SUCCESS.value == "✓"
        assert Status.FAILURE.value == "✗"
        assert Status.RUNNING.value == "→"
        assert Status.PENDING.value == "○"


class TestProgress:
    """Tests for Progress class."""

    def test_init(self):
        """Test Progress initialization."""
        progress = Progress(verbose=True, no_color=True)
        assert progress.verbose is True

    def test_progress_bar(self):
        """Test progress bar generation."""
        progress = Progress(no_color=True)

        bar = progress.progress_bar(0, 100)
        assert bar == "[" + "░" * 20 + "]"

        bar = progress.progress_bar(50, 100)
        assert bar == "[" + "█" * 10 + "░" * 10 + "]"

        bar = progress.progress_bar(100, 100)
        assert bar == "[" + "█" * 20 + "]"

    def test_progress_bar_zero_total(self):
        """Test progress bar with zero total."""
        progress = Progress(no_color=True)
        bar = progress.progress_bar(0, 0)
        assert bar == "[" + " " * 20 + "]"

    def test_total_elapsed(self):
        """Test elapsed time tracking."""
        import time
        progress = Progress()
        time.sleep(0.1)
        elapsed = progress.total_elapsed()
        assert elapsed >= 0.1

    def test_spinners(self):
        """Test spinner characters."""
        progress = Progress()
        assert len(progress.SPINNERS) > 0
        assert all(len(s) == 1 for s in progress.SPINNERS)
