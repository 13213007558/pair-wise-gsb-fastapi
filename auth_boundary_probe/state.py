"""Process-wide probes for resource cleanup and pending tasks.

The auth dependency opens a real AnyIO task group + background task inside its
``yield`` section.  Every scenario asserts via these counters that switching the
exception style never leaks the entered resources.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TeardownStats:
    enter_count: int = 0
    exit_count: int = 0
    bg_started: int = 0
    bg_cancelled: int = 0
    seen_exceptions: list[str] = field(default_factory=list)
    last_exc_repr: str | None = None

    def reset(self) -> None:
        self.enter_count = 0
        self.exit_count = 0
        self.bg_started = 0
        self.bg_cancelled = 0
        self.seen_exceptions.clear()
        self.last_exc_repr = None

    @property
    def balanced(self) -> bool:
        return self.enter_count == self.exit_count and self.bg_started == self.bg_cancelled


STATS = TeardownStats()
