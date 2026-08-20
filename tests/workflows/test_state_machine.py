from __future__ import annotations

import unittest

from ziniao_automation.workflows.errors import InvalidTransition
from ziniao_automation.workflows.state_machine import (
    require_guard_transition,
    require_run_transition,
)
from ziniao_automation.workflows.types import GuardState, RunStatus


class StateMachineTests(unittest.TestCase):
    def test_financial_guard_never_returns_to_armed(self) -> None:
        require_guard_transition(GuardState.ARMED, GuardState.SUBMITTED)
        require_guard_transition(GuardState.SUBMITTED, GuardState.CONFIRMED)
        with self.assertRaises(InvalidTransition):
            require_guard_transition(GuardState.SUBMITTED, GuardState.ARMED)
        with self.assertRaises(InvalidTransition):
            require_guard_transition(GuardState.CONFIRMED, GuardState.SUBMITTED)

    def test_uncertain_run_only_enters_reconciliation(self) -> None:
        require_run_transition(
            RunStatus.UNCERTAIN_FINANCIAL, RunStatus.RECONCILING
        )
        with self.assertRaises(InvalidTransition):
            require_run_transition(
                RunStatus.UNCERTAIN_FINANCIAL, RunStatus.RUNNING
            )


if __name__ == "__main__":
    unittest.main()

