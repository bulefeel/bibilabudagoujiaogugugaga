"""Closed CDP cleanup must not cut off the real inventory notification path."""
from playwright._impl._errors import TargetClosedError
from sqlalchemy import select

from ziniao_automation.models import NotificationDelivery, Run
from ziniao_automation.notifications.adapter import DatabaseNotificationAdapter
from ziniao_automation.notifications.database import NotificationDeliveryService
from ziniao_automation.workflows.amazon_inventory_gpsr.page import AmazonInventoryGpsrPage
from ziniao_automation.workflows.amazon_inventory_gpsr.store import BLOCKED
from ziniao_automation.workflows.types import RunStatus, SiteStatus

from .test_inventory_gpsr_workflow import Adapter, harness, items


async def test_closed_page_report_reaches_persistent_notification_without_changing_run_or_ledger(harness):
    run = harness.new_run()
    await harness.workflow(Adapter(items(13))).plan(run, object())
    first = (await harness.ledger.list_items(run.id))[0]
    await harness.ledger.mark(first.id, BLOCKED, reason="historical_submission_unreadable")
    await harness.repo.set_site_status(run.id, "DE", SiteStatus.FAILED)
    await harness.repo.set_run_status(run.id, RunStatus.FAILED)
    before = await harness.ledger.list_items(run.id)

    class ClosedPage:
        def __init__(self):
            self.attached = []
            self.detached = []

        def on(self, event, handler):
            self.attached.append(event)

        def remove_listener(self, event, handler):
            self.detached.append(event)
            raise TargetClosedError("PRIVATE_CDP_PAYLOAD")

    class Sender:
        def __init__(self):
            self.notices = []

        async def send(self, notice):
            self.notices.append(notice)

    page = ClosedPage()
    page_adapter = AmazonInventoryGpsrPage()
    page_adapter._watch_inventory_responses(page, run)
    flow = harness.workflow(page_adapter)
    # report() uses the real cleanup in its finally. Previously TargetClosedError
    # escaped here, so DatabaseNotificationAdapter was never called at all.
    report = await flow.report(run, RunStatus.FAILED)
    assert page.attached == page.detached == ["response", "close"]
    assert not page_adapter._response_observers
    assert report.fields["pending"] == 12 and report.fields["blocked"] == 1
    sender = Sender()
    notifier = DatabaseNotificationAdapter(NotificationDeliveryService(harness.factory, sender))
    await notifier.send(report)
    assert len(sender.notices) == 1
    assert sender.notices[0].inventory_gpsr_counts.pending == 12
    assert sender.notices[0].inventory_gpsr_counts.blocked == 1
    assert "PRIVATE_CDP_PAYLOAD" not in repr(sender.notices[0])
    with harness.factory() as session:
        delivery = session.scalar(select(NotificationDelivery).where(NotificationDelivery.run_id == run.id))
        assert delivery.status == "SENT" and delivery.attempts == 1
        assert session.get(Run, run.id).status == "FAILED"
    assert await harness.ledger.list_items(run.id) == before
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.FAILED
