"""Progress display utilities with elapsed time tracking."""

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional


class Color:
    """ANSI color codes."""

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[0;33m"
    BLUE = "\033[0;34m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        """Disable colors (for non-TTY output)."""
        cls.RED = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.BOLD = ""
        cls.RESET = ""


class Status(Enum):
    """Task status indicators."""

    PENDING = "○"
    RUNNING = "→"
    SUCCESS = "✓"
    FAILURE = "✗"
    SKIPPED = "○"
    WARNING = "⚠"


@dataclass
class TaskResult:
    """Result of a task execution."""

    status: Status
    message: str
    elapsed: float
    details: Optional[str] = None


class Progress:
    """Progress display manager with timing."""

    SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, verbose: bool = True, no_color: bool = False):
        self.verbose = verbose
        self.start_time = time.monotonic()
        self.phase_start: Optional[float] = None
        self.task_start: Optional[float] = None
        self._spinner_idx = 0

        if no_color or not sys.stdout.isatty():
            Color.disable()

    def header(self, title: str, mode: str = "Manual", dry_run: bool = False) -> None:
        """Print the main header."""
        print()
        print("═" * 67)
        print(f" {Color.BOLD}{title}{Color.RESET} - {mode} Mode")
        if dry_run:
            print(f" {Color.YELLOW}(DRY RUN - no changes will be made){Color.RESET}")
        print("═" * 67)

    def phase(self, number: int, name: str, timeout: Optional[int] = None) -> None:
        """Start a new phase."""
        self.phase_start = time.monotonic()
        timeout_str = f"[timeout: {timeout}s]" if timeout else ""
        print()
        print(f"{Color.BOLD}Phase {number}: {name}{Color.RESET}".ljust(50) + timeout_str)
        print("━" * 67)

    def phase_summary(self) -> None:
        """Print phase timing summary."""
        if self.phase_start:
            elapsed = time.monotonic() - self.phase_start
            print(" " * 54 + "──────────────")
            print(" " * 54 + f" Total: {elapsed:.1f}s")

    @contextmanager
    def task(self, description: str) -> Iterator[None]:
        """Context manager for a timed task."""
        self.task_start = time.monotonic()
        self._print_task_status(description, Status.RUNNING)
        try:
            yield
            elapsed = time.monotonic() - self.task_start
            self._print_task_status(description, Status.SUCCESS, elapsed)
        except Exception:
            elapsed = time.monotonic() - self.task_start
            self._print_task_status(description, Status.FAILURE, elapsed)
            raise

    def task_status(
        self,
        description: str,
        status: Status,
        elapsed: Optional[float] = None,
        details: Optional[str] = None,
    ) -> None:
        """Print a task status line."""
        self._print_task_status(description, status, elapsed, details)

    def _print_task_status(
        self,
        description: str,
        status: Status,
        elapsed: Optional[float] = None,
        details: Optional[str] = None,
    ) -> None:
        """Internal: print formatted task status."""
        color = {
            Status.PENDING: Color.YELLOW,
            Status.RUNNING: Color.BLUE,
            Status.SUCCESS: Color.GREEN,
            Status.FAILURE: Color.RED,
            Status.SKIPPED: Color.YELLOW,
            Status.WARNING: Color.YELLOW,
        }.get(status, Color.RESET)

        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else ""
        detail_str = f" ({details})" if details else ""

        # Clear line and print
        sys.stdout.write("\r" + " " * 80 + "\r")
        print(
            f"  {color}{status.value}{Color.RESET} {description}{detail_str}".ljust(55)
            + elapsed_str
        )

    def waiting(self, message: str, elapsed: float, timeout: int) -> None:
        """Update waiting status with spinner."""
        spinner = self.SPINNERS[self._spinner_idx % len(self.SPINNERS)]
        self._spinner_idx += 1
        percent = min(100, int((elapsed / timeout) * 100)) if timeout > 0 else 0
        sys.stdout.write(
            f"\r  {Color.BLUE}{spinner}{Color.RESET} {message}: {int(elapsed)}s / {timeout}s [{percent}%]"
        )
        sys.stdout.flush()

    def clear_line(self) -> None:
        """Clear the current line."""
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()

    def info(self, message: str) -> None:
        """Print an info message."""
        print(f"  {Color.BLUE}→{Color.RESET} {message}")

    def ok(self, message: str) -> None:
        """Print a success message."""
        print(f"  {Color.GREEN}✓{Color.RESET} {message}")

    def warn(self, message: str) -> None:
        """Print a warning message."""
        print(f"  {Color.YELLOW}⚠{Color.RESET} {message}")

    def fail(self, message: str, details: Optional[str] = None) -> None:
        """Print a failure message."""
        print(f"  {Color.RED}✗{Color.RESET} {message}")
        if details:
            print(f"         {details}")

    def skip(self, message: str) -> None:
        """Print a skipped message."""
        print(f"  {Color.YELLOW}○{Color.RESET} {message}")

    def dry_run(self, message: str) -> None:
        """Print a dry-run message."""
        print(f"  {Color.YELLOW}[DRY-RUN]{Color.RESET} {message}")

    def countdown(self, message: str, seconds: int) -> None:
        """Display a countdown."""
        for remaining in range(seconds, 0, -1):
            sys.stdout.write(f"\r  {Color.BLUE}◷{Color.RESET} {message} {remaining}...")
            sys.stdout.flush()
            time.sleep(1)
        self.clear_line()

    def progress_bar(self, current: int, total: int, width: int = 20) -> str:
        """Generate a progress bar string."""
        if total == 0:
            return "[" + " " * width + "]"
        filled = int((current / total) * width)
        return "[" + "█" * filled + "░" * (width - filled) + "]"

    def summary(self, passed: int, failed: int, skipped: int = 0) -> None:
        """Print a results summary."""
        print()
        print("━" * 40)
        print(f"  {Color.GREEN}Passed{Color.RESET}:  {passed}")
        print(f"  {Color.RED}Failed{Color.RESET}:  {failed}")
        if skipped:
            print(f"  {Color.YELLOW}Skipped{Color.RESET}: {skipped}")
        print("━" * 40)
        print()

    def footer(self, success: bool, urls: Optional[dict] = None) -> None:
        """Print the final footer."""
        print()
        print("═" * 67)
        if success:
            print(f" {Color.GREEN}{Color.BOLD}Deployment complete!{Color.RESET}")
            if urls:
                print()
                print(" Access your server:")
                for name, url in urls.items():
                    print(f"   {name}: {url}")
        else:
            print(f" {Color.RED}{Color.BOLD}Deployment failed{Color.RESET}")
        print("═" * 67)
        print()

    def total_elapsed(self) -> float:
        """Get total elapsed time since start."""
        return time.monotonic() - self.start_time
