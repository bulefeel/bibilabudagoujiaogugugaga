"""Bounded, pre-submit read progress; a valid skip is progress, not just a save."""
import time

from ..errors import PageRecoveryRequired
from .config import InventoryGpsrPolicy


class ReadProgress:
    def __init__(self, policy: InventoryGpsrPolicy) -> None:
        self.policy = policy
        self.last_progress = time.monotonic()
        self.failures = 0

    def completed(self) -> None:
        self.last_progress = time.monotonic()
        self.failures = 0

    def failed(self) -> None:
        self.failures += 1

    def check(self) -> None:
        if (self.failures >= self.policy.consecutive_read_failure_limit
                or time.monotonic() - self.last_progress >= self.policy.no_progress_timeout_seconds):
            raise PageRecoveryRequired("库存读取持续无进展，暂停并保留剩余清单",
                                       reason_code="inventory_no_progress")

    def diagnostic(self) -> dict[str, int]:
        return {"consecutive_failures": self.failures,
                "seconds_without_progress": int(time.monotonic() - self.last_progress)}
