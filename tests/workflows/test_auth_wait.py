from __future__ import annotations

from contextlib import asynccontextmanager
import unittest

from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
from ziniao_automation.workflows.types import (
    RunMode,
    RunStatus,
    WorkflowPlan,
    WorkflowReport,
)

from .test_disbursement_recovery import make_run


class AuthWorkflow:
    name = "amazon_disbursement"

    def __init__(self) -> None:
        self.calls = 0
        self.pages = []

    async def preflight(self, run, page):
        self.calls += 1
        self.pages.append(page)
        if self.calls == 1:
            raise HumanAuthRequired("challenge", kind="captcha")

    async def plan(self, run, page):
        return WorkflowPlan(self.name, run.id, run.store.id, ())

    async def execute(self, *args):
        return ()

    async def reconcile(self, *args, **kwargs):
        return ()

    async def report(self, run, status):
        return WorkflowReport(run.id, status, "test", "test")


class RepeatedAuthWorkflow(AuthWorkflow):
    async def preflight(self, run, page):
        self.calls += 1
        self.pages.append(page)
        if self.calls <= 2:
            raise HumanAuthRequired("challenge", kind="captcha")


class _Page:
    def __getattr__(self, name):
        raise AssertionError(f"wait_for_auth must not operate the page: {name}")


class _Handle:
    def __init__(self):
        self.page = _Page()


class AuthSessions:
    def __init__(self) -> None:
        self.inside = False
        self.waited_inside = False
        self.handle = _Handle()
        self.wait_handle = None
        self.page_before_wait = None
        self.wait_calls = 0

    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        self.inside = True
        try:
            yield self.handle
        finally:
            self.inside = False

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800):
        self.wait_calls += 1
        self.waited_inside = self.inside
        self.wait_handle = handle
        self.page_before_wait = handle.page
        # Simulates the user typing directly into the visible Ziniao store
        # window. The backend performs no Playwright page operation here.
        return handle

    async def continue_auth(self, auth_key):
        return False

    async def cancel_auth(self, auth_key):
        return False


class AuthWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_keeps_financial_session_and_reruns_preflight(self) -> None:
        repo = InMemoryWorkflowRepository()
        run = make_run(RunMode.DRY_RUN, "auth-run")
        await repo.add_run(run)
        workflow = AuthWorkflow()
        sessions = AuthSessions()
        engine = WorkflowEngine(
            registry=WorkflowRegistry((workflow,)),
            repository=repo,
            browser_sessions=sessions,
        )
        result = await engine.start(run)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertTrue(sessions.waited_inside)
        self.assertEqual(workflow.calls, 2)
        self.assertIs(sessions.wait_handle, sessions.handle)
        self.assertIs(sessions.page_before_wait, sessions.handle.page)
        self.assertTrue(all(page is sessions.handle.page for page in workflow.pages))
        statuses = [event["event_type"] for event in repo.events]
        self.assertIn("human_auth_required", statuses)
        self.assertIn("human_auth_resumed", statuses)

    async def test_multiple_challenges_reuse_same_visible_ziniao_page(self) -> None:
        repo = InMemoryWorkflowRepository()
        run = make_run(RunMode.DRY_RUN, "repeated-auth-run")
        await repo.add_run(run)
        workflow = RepeatedAuthWorkflow()
        sessions = AuthSessions()
        engine = WorkflowEngine(
            registry=WorkflowRegistry((workflow,)),
            repository=repo,
            browser_sessions=sessions,
        )

        result = await engine.start(run)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(sessions.wait_calls, 2)
        self.assertTrue(all(page is sessions.handle.page for page in workflow.pages))


class _ReplacedPageSessions(AuthSessions):
    """紫鸟在人工验证期间把自动化标签页换掉了——恢复时必须拿到新的那个。"""

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800):
        await super().wait_for_auth(handle, auth_key, timeout_seconds=timeout_seconds)
        handle.page = _Page()
        return handle


class AuthReasonAndHandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_reason_the_login_stopped_is_persisted(self) -> None:
        """否则事后没人答得出「为什么又要人工登录」。

        2026-09-18 正香的德国站停在 /ap/signin，运行记录里只剩 kind=sign_in；
        真正的原因（登录助手判定按钮缺失）只存在于一个没有落盘的异常对象里，
        现场早就没了。
        """

        repo = InMemoryWorkflowRepository()
        run = make_run(RunMode.DRY_RUN, "auth-reason-run")
        await repo.add_run(run)
        engine = WorkflowEngine(
            registry=WorkflowRegistry((AuthWorkflow(),)),
            repository=repo,
            browser_sessions=AuthSessions(),
        )

        await engine.start(run)

        paused = [e for e in repo.events if e["event_type"] == "human_auth_required"]
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["details"]["kind"], "captcha")
        self.assertEqual(paused[0]["details"]["reason"], "challenge")
        self.assertIn("challenge", paused[0]["message"])

    async def test_resume_uses_the_page_the_controller_handed_back(self) -> None:
        """人工验证后紫鸟可能已经换了标签页，续跑必须用新的那个。

        拿着旧引用重放的结果是 ``TargetClosedError: Page.goto``——人刚做完的验证
        被整个丢掉，运行直接判失败。
        """

        repo = InMemoryWorkflowRepository()
        run = make_run(RunMode.DRY_RUN, "auth-replaced-page-run")
        await repo.add_run(run)
        workflow = AuthWorkflow()
        sessions = _ReplacedPageSessions()
        engine = WorkflowEngine(
            registry=WorkflowRegistry((workflow,)),
            repository=repo,
            browser_sessions=sessions,
        )

        result = await engine.start(run)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(workflow.calls, 2)
        self.assertIsNot(workflow.pages[0], workflow.pages[1])
        self.assertIs(workflow.pages[1], sessions.handle.page)


if __name__ == "__main__":
    unittest.main()

