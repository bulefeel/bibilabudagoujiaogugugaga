"""Cancellation reaches the durable notice builder through the real engine."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from ziniao_automation.models import NotificationDelivery
from ziniao_automation.notifications import (
    DatabaseNotificationAdapter, NotificationDeliveryService, NotificationKind,
)
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.types import RunStatus
from ziniao_automation.ziniao.errors import AuthWaitCancelled

from .test_inventory_gpsr_workflow import Adapter, _real_runtime, harness, items


class Collector:
    def __init__(self):
        self.notices = []

    async def send(self, notice):
        self.notices.append(notice)


@pytest.mark.parametrize("after_intent", [False, True])
async def test_auth_wait_cancellation_emits_unsettled_notice_only_with_intent(harness, after_intent):
    adapter = Adapter(items(2))
    run = harness.new_run()
    _, engine, _ = _real_runtime(harness, adapter)
    sender = Collector()
    engine.notifier = DatabaseNotificationAdapter(NotificationDeliveryService(harness.factory, sender))

    async def request_login(item):
        raise HumanAuthRequired("fixture sign-in", kind="signin")

    async def cancel_wait(handle, auth_key, *, timeout_seconds, on_waiting):
        await on_waiting()
        assert await harness.repo.get_run_status(run.id) is RunStatus.WAITING_AUTH
        # The real controller raises this when the operator cancels its auth lease.
        raise AuthWaitCancelled("fixture operator cancelled")

    engine.browser_sessions.wait_for_auth = cancel_wait
    if after_intent:
        adapter.on_save = request_login
    else:
        adapter.on_inspect = request_login
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)

    result = await engine.start(run)

    assert result.status is RunStatus.CANCELLED
    assert await harness.repo.get_run_status(run.id) is RunStatus.CANCELLED
    assert not adapter.saved
    rows = await harness.ledger.list_items(run.id)
    assert any(row.intent_at for row in rows) is after_intent
    kinds = [notice.kind for notice in sender.notices]
    assert kinds[0] is NotificationKind.WAITING_AUTH
    if after_intent:
        assert kinds == [NotificationKind.WAITING_AUTH, NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN]
        assert sender.notices[-1].inventory_gpsr_counts.uncertain == 1
        assert sender.notices[-1].inventory_gpsr_counts.scanned == 2
    else:
        assert kinds == [NotificationKind.WAITING_AUTH]
    with harness.factory() as session:
        deliveries = session.scalars(select(NotificationDelivery)).all()
        assert len(deliveries) == len(kinds)
        assert all(row.status == "SENT" for row in deliveries)
