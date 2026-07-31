"""Expose controlled UTC, monotonic, and wait primitives."""

from __future__ import annotations

import time
from datetime import UTC, datetime


class Clock:
    """Expose UTC time, monotonic time, and controlled waiting."""

    def monotonic(self) -> float:
        """Return the current monotonic clock value."""

        return time.monotonic()

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant."""

        return datetime.now(UTC)

    def sleep(self, delay_seconds: float) -> None:
        """Wait for a non-negative duration."""

        time.sleep(delay_seconds)
