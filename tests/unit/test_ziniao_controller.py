import asyncio
from types import SimpleNamespace

import pytest

from ziniao_automation.ziniao.controller import ZiniaoController, ZiniaoControllerConfig
from ziniao_automation.ziniao.errors import (
    AuthWaitExpired,
    CdpHealthError,
    ZiniaoCredentialError,
    ZiniaoLaunchError,
)
from ziniao_automation.ziniao.locks import ExecutionLocks
from ziniao_automation.ziniao.models import CdpHealth, ProfileSelector, ZiniaoBrowserHandle


class FakeClient:
    def __init__(self, ports: list[int]):
        self.config = SimpleNamespace(host="127.0.0.1", port=16851)
        self.ports = iter(ports)
        self.actions: list[tuple[str, str]] = []

    async def probe(self):
        return SimpleNamespace(status_code="0")

    async def update_core(self):
        return SimpleNamespace(status_code="0")

    async def start_browser(self, selector):
        self.actions.append(("start", selector.value))
        return {"debuggingPort": next(self.ports)}

    async def stop_browser(self, selector, *, ignore_errors=False):
        self.actions.append(("stop", selector.value))
        return True

    async def close(self):
        return None


class SequencedHealth:
    def __init__(self, reachable: list[bool]):
        self.reachable = iter(reachable)

    async def probe(self, host, port):
        ok = next(self.reachable)
        return CdpHealth(reachable=ok, instrumentation_live=True, issues=() if ok else ("down",))


class FakeSessions:
    def __init__(self):
        self.proofs = {}

    def register_ziniao_launch(self, proof):
        self.proofs[proof.nonce] = proof

    def discard_ziniao_launch(self, proof):
        if self.proofs.get(proof.nonce) is proof:
            self.proofs.pop(proof.nonce, None)

    async def connect(self, *, selector, host, port, base_health, launch_proof):
        assert self.proofs.pop(launch_proof.nonce) is launch_proof
        async def close(_):
            return None

        return ZiniaoBrowserHandle(
            selector=selector,
            debugging_host=host,
            debugging_port=port,
            browser=object(),
            context=object(),
            page=object(),
            health=base_health,
            launch_proof=launch_proof,
            _close_callback=close,
        )


class StartupClosingSessions(FakeSessions):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures
        self.connect_calls = 0

    async def connect(self, **kwargs):
        self.connect_calls += 1
        if self.connect_calls <= self.failures:
            proof = kwargs["launch_proof"]
            assert self.proofs.pop(proof.nonce) is proof
            raise CdpHealthError(
                "startup target closed",
                details={"phase": "startup_target", "retry_safe": True},
            )
        return await super().connect(**kwargs)


class HangingCleanupSessions(FakeSessions):
    def __init__(self):
        super().__init__()
        self.disconnect_started = asyncio.Event()
        self.never_disconnects = asyncio.Event()

    async def connect(self, **kwargs):
        handle = await super().connect(**kwargs)

        async def close(_):
            self.disconnect_started.set()
            await self.never_disconnects.wait()

        handle._close_callback = close
        return handle


class HangingStopClient(FakeClient):
    def __init__(self, ports: list[int]):
        super().__init__(ports)
        self.stop_started = asyncio.Event()
        self.never_stops = asyncio.Event()

    async def stop_browser(self, selector, *, ignore_errors=False):
        del ignore_errors
        self.actions.append(("stop", selector.value))
        self.stop_started.set()
        await self.never_stops.wait()
        return True


@pytest.mark.asyncio
async def test_per_store_retry_stops_only_failed_store() -> None:
    client = FakeClient([9001, 9002])
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(max_start_attempts=4),
        health_checker=SequencedHealth([False, True]),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )
    selector = ProfileSelector("oauth", "store-a")
    handle = await controller.open_store(selector)
    assert handle.debugging_port == 9002
    await handle.close()
    assert client.actions == [
        ("start", "store-a"),
        ("stop", "store-a"),
        ("start", "store-a"),
        ("stop", "store-a"),
    ]


@pytest.mark.asyncio
async def test_launch_is_bounded_at_four_attempts() -> None:
    client = FakeClient([1, 2, 3, 4])
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(max_start_attempts=4),
        health_checker=SequencedHealth([False] * 4),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )
    with pytest.raises(ZiniaoLaunchError):
        await controller.open_store(ProfileSelector("oauth", "bad"))
    assert [a for a in client.actions if a[0] == "start"] == [("start", "bad")] * 4
    # Exactly one per-store cleanup stop for each failed start.
    assert len([a for a in client.actions if a[0] == "stop"]) == 4


@pytest.mark.asyncio
async def test_global_credential_error_is_not_retried_or_stopped_per_store() -> None:
    class CredentialFailClient(FakeClient):
        async def start_browser(self, selector):
            self.actions.append(("start", selector.value))
            raise ZiniaoCredentialError("紫鸟凭据引用已失效")

    client = CredentialFailClient([9001, 9002, 9003, 9004])
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(max_start_attempts=4),
        health_checker=SequencedHealth([True] * 4),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )

    with pytest.raises(ZiniaoCredentialError, match="凭据引用已失效"):
        await controller.open_store(ProfileSelector("oauth", "store-a"))

    assert client.actions == [("start", "store-a")]


@pytest.mark.asyncio
async def test_startup_target_loss_recovers_once_for_same_store_only() -> None:
    client = FakeClient([9001, 9002])
    sessions = StartupClosingSessions(failures=1)
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(
            max_start_attempts=4,
            startup_poll_seconds=0.001,
        ),
        health_checker=SequencedHealth([True, True]),
        sessions=sessions,
        process_running=lambda: True,
    )

    handle = await controller.open_store(ProfileSelector("oauth", "store-a"))
    assert handle.debugging_port == 9002
    await handle.close()
    assert client.actions == [
        ("start", "store-a"),
        ("stop", "store-a"),
        ("start", "store-a"),
        ("stop", "store-a"),
    ]


@pytest.mark.asyncio
async def test_repeated_startup_target_loss_does_not_consume_four_launches() -> None:
    client = FakeClient([9001, 9002, 9003, 9004])
    sessions = StartupClosingSessions(failures=4)
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(
            max_start_attempts=4,
            startup_poll_seconds=0.001,
        ),
        health_checker=SequencedHealth([True, True, True, True]),
        sessions=sessions,
        process_running=lambda: True,
    )

    with pytest.raises(ZiniaoLaunchError):
        await controller.open_store(ProfileSelector("oauth", "store-a"))
    assert [action for action in client.actions if action[0] == "start"] == [
        ("start", "store-a"),
        ("start", "store-a"),
    ]
    assert [action for action in client.actions if action[0] == "stop"] == [
        ("stop", "store-a"),
        ("stop", "store-a"),
    ]


@pytest.mark.asyncio
async def test_unclassified_connect_error_keeps_existing_launch_policy() -> None:
    class OtherFailingSessions(FakeSessions):
        async def connect(self, **kwargs):
            proof = kwargs["launch_proof"]
            assert self.proofs.pop(proof.nonce) is proof
            raise RuntimeError(
                "Locator.count: Target page, context or browser has been closed"
            )

    client = FakeClient([9001, 9002, 9003, 9004])
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(
            max_start_attempts=4,
            startup_poll_seconds=0.001,
        ),
        health_checker=SequencedHealth([True, True, True, True]),
        sessions=OtherFailingSessions(),
        process_running=lambda: True,
    )

    with pytest.raises(ZiniaoLaunchError):
        await controller.open_store(ProfileSelector("oauth", "store-a"))
    # Matching exception text alone is insufficient: only the session
    # manager's typed pre-business proof receives the special one-retry bound.
    assert len([action for action in client.actions if action[0] == "start"]) == 4


@pytest.mark.asyncio
async def test_target_close_after_handle_delivery_is_never_auto_replayed() -> None:
    client = FakeClient([9001, 9002])
    controller = ZiniaoController(
        client,
        health_checker=SequencedHealth([True, True]),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )
    selector = ProfileSelector("oauth", "store-a")

    with pytest.raises(RuntimeError, match="Target page"):
        async with controller.session(selector):
            # This represents a target loss after the workflow owns the Page.
            # Retrying here could replay a business transition, so cleanup is
            # allowed but another startBrowser is not.
            raise RuntimeError(
                "Locator.count: Target page, context or browser has been closed"
            )

    assert client.actions == [
        ("start", "store-a"),
        ("stop", "store-a"),
    ]


@pytest.mark.asyncio
async def test_store_lock_excludes_same_store_but_not_other_store() -> None:
    locks = ExecutionLocks()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    other_entered = asyncio.Event()

    async def first():
        async with locks.store("A"):
            first_entered.set()
            await release_first.wait()

    async def second():
        await first_entered.wait()
        async with locks.store("A"):
            second_entered.set()

    async def other():
        await first_entered.wait()
        async with locks.store("B"):
            other_entered.set()

    tasks = [asyncio.create_task(fn()) for fn in (first, second, other)]
    await first_entered.wait()
    await asyncio.wait_for(other_entered.wait(), 1)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(*tasks)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_human_auth_pause_resumes_same_handle() -> None:
    client = FakeClient([9001])
    controller = ZiniaoController(
        client,
        health_checker=SequencedHealth([True]),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )
    selector = ProfileSelector("oauth", "auth-store")
    async with controller.financial_session(selector) as handle:
        waiter = asyncio.create_task(controller.wait_for_auth(handle, "run-1", timeout_seconds=1))
        await asyncio.sleep(0)
        snapshot = await controller.auth_snapshot()
        assert snapshot["run-1"]["debugging_port"] == 9001
        assert await controller.continue_auth("run-1") is True
        resumed = await waiter
        assert resumed is handle
    assert client.actions[-1] == ("stop", "auth-store")


@pytest.mark.asyncio
async def test_human_auth_timeout_expires_browser() -> None:
    client = FakeClient([9001])
    controller = ZiniaoController(
        client,
        health_checker=SequencedHealth([True]),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )
    selector = ProfileSelector("oauth", "auth-store")
    async with controller.session(selector) as handle:
        with pytest.raises(AuthWaitExpired):
            await controller.wait_for_auth(handle, "run-timeout", timeout_seconds=0.01)
    assert ("stop", "auth-store") in client.actions


@pytest.mark.asyncio
async def test_global_funds_lock_serialises_different_stores() -> None:
    client = FakeClient([9001, 9002])
    controller = ZiniaoController(
        client,
        health_checker=SequencedHealth([True, True]),
        sessions=FakeSessions(),
        process_running=lambda: True,
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with controller.financial_session(ProfileSelector("oauth", "first")):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with controller.financial_session(ProfileSelector("oauth", "second")):
            second_entered.set()

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await first_entered.wait()
    await asyncio.sleep(0.01)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(*tasks)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_financial_session_bounds_hung_cleanup_and_releases_locks() -> None:
    client = HangingStopClient([9001])
    sessions = HangingCleanupSessions()
    locks = ExecutionLocks()
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(
            cancel_cleanup_step_timeout_seconds=0.01,
        ),
        locks=locks,
        health_checker=SequencedHealth([True]),
        sessions=sessions,
        process_running=lambda: True,
    )
    entered = asyncio.Event()

    async def run() -> None:
        async with controller.financial_session(
            ProfileSelector("oauth", "sensitive-oauth"),
            store_key="deadline-store",
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    result = await asyncio.wait_for(
        asyncio.gather(task, return_exceptions=True),
        timeout=1.0,
    )

    assert isinstance(result[0], asyncio.CancelledError)
    assert sessions.disconnect_started.is_set()
    assert client.stop_started.is_set()
    assert client.actions[-1] == ("stop", "sensitive-oauth")
    snapshot = locks.snapshot()
    assert snapshot["funds_locked"] is False
    assert snapshot["launch_locked"] is False
    assert snapshot["stores"]["deadline-store"] is False


@pytest.mark.asyncio
async def test_deadline_during_disconnect_switches_remaining_cleanup_to_bounded_mode() -> None:
    client = HangingStopClient([9001])
    sessions = HangingCleanupSessions()
    locks = ExecutionLocks()
    controller = ZiniaoController(
        client,
        config=ZiniaoControllerConfig(
            cancel_cleanup_step_timeout_seconds=0.01,
        ),
        locks=locks,
        health_checker=SequencedHealth([True]),
        sessions=sessions,
        process_running=lambda: True,
    )

    async def run() -> None:
        async with controller.financial_session(
            ProfileSelector("oauth", "sensitive-oauth"),
            store_key="cleanup-race-store",
        ):
            pass

    task = asyncio.create_task(run())
    # Normal __aexit__ has already entered the unbounded disconnect path.
    await asyncio.wait_for(sessions.disconnect_started.wait(), timeout=1.0)
    task.cancel()
    result = await asyncio.wait_for(
        asyncio.gather(task, return_exceptions=True),
        timeout=1.0,
    )

    assert isinstance(result[0], asyncio.CancelledError)
    assert client.stop_started.is_set()
    snapshot = locks.snapshot()
    assert snapshot["funds_locked"] is False
    assert snapshot["launch_locked"] is False
    assert snapshot["stores"]["cleanup-race-store"] is False


@pytest.mark.asyncio
async def test_bounded_cleanup_accepts_completion_at_timeout_boundary(
    monkeypatch,
) -> None:
    controller = ZiniaoController(FakeClient([]))

    async def completed():
        return "acquired"

    task = asyncio.create_task(completed())
    await asyncio.sleep(0)
    assert task.done()

    async def stale_timeout_snapshot(tasks, *, timeout):
        del timeout
        # Simulate asyncio.wait taking its timeout snapshot immediately before
        # the task's completion callback becomes visible to the caller.
        return set(), set(tasks)

    monkeypatch.setattr(asyncio, "wait", stale_timeout_snapshot)
    finished, value = await controller._bounded_cancel_cleanup_step(
        task,
        operation="acquire_launch_lock",
    )

    assert finished is True
    assert value == "acquired"
