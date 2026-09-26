"""反馈管理页的落地判据（用户 2026-09-24 拍板「也自动选站」，09-26 实现）。

这条流程原来只有一句 ``if FEEDBACK_MANAGER_PATH not in landed``，两处都错：

* 拿**整个 URL** 做子串匹配 —— 而切换页长成
  ``/account-switcher?returnTo=/feedback-manager/index.html``，目标路径就在查询
  参数里，于是切换页被当成「已经到了」，接着去一张空表上找活儿；
* 落不到就写死「该店铺未开通此站点」—— 一句没读过就下的断言。2026-09-18 正香
  实测推翻过同一句话（切换页上那个国家在列，只是本次会话没选定）。

现在落地那几步走共享的 ``AmazonLanding``，和 GPSR 同一份实现。
"""

from __future__ import annotations

import asyncio

import pytest

from ziniao_automation.workflows.amazon_account_switcher import (
    AmazonAccountSwitcher,
    SwitchOutcome,
)
from ziniao_automation.workflows.amazon_feedback.config import FEEDBACK_MANAGER_PATH
from ziniao_automation.workflows.amazon_feedback.page import AmazonFeedbackPage


CA = "sellercentral.amazon.ca"
LIST_URL = f"https://{CA}{FEEDBACK_MANAGER_PATH}"
SIGNIN = f"https://{CA}/ap/signin?openid.assoc_handle=sc_ca"
# ⚠️ 目标路径就明晃晃地写在 returnTo 里 —— 子串匹配必然把这一页当成「已经到了」。
SWITCHER = (
    f"https://{CA}/account-switcher/default/merchantMarketplace"
    f"?returnTo={FEEDBACK_MANAGER_PATH}"
)
ELSEWHERE = f"https://{CA}/home"


class FakePage:
    def __init__(self, *, lands_on: str, after_switch: str | None = None, rows: int = 2):
        self.url = ""
        self._lands_on = lands_on
        self._after_switch = after_switch or lands_on
        self.switched = False
        self.rows = rows
        self.waits = 0
        self.gotos: list[str] = []

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = self._after_switch if self.switched else self._lands_on

    async def wait_for_timeout(self, ms: int) -> None:
        # 预算 + 让出控制权:撤掉轮询下限时要**响亮失败**而不是挂死。
        self.waits += 1
        assert self.waits < 1_000, "轮询循环没有推进"
        await asyncio.sleep(0)

    async def wait_for_selector(self, selector, **kwargs):
        return object()

    async def evaluate(self, script, arg=None):
        return self.rows


class ScriptedSwitcher(AmazonAccountSwitcher):
    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason
        self.calls: list[str] = []

    async def select_marketplace(self, page, domain, *, expected_path):
        self.calls.append(domain)
        if self._reason == "selected":
            page.switched = True
            page.url = f"https://{domain}{expected_path}"
            return SwitchOutcome(ok=True, reason="selected", landed_url=page.url)
        if self._reason == "auth_required":
            page.url = SIGNIN
        return SwitchOutcome(ok=False, reason=self._reason, landed_url=page.url)


class Advancer:
    def __init__(self, lands_on: str | None) -> None:
        self.lands_on = lands_on
        self.calls = 0

    async def advance_or_raise(self, page, *, expected_host=None, **kwargs):
        self.calls += 1
        if self.lands_on is None:
            return None
        page.url = self.lands_on
        return page


def subject(switcher, advancer=None) -> AmazonFeedbackPage:
    from ziniao_automation.workflows.amazon_landing import AmazonLanding

    return AmazonFeedbackPage(
        settle_seconds=0.0,
        list_timeout_seconds=0.03,
        landing=AmazonLanding(
            settle_seconds=0.0,
            login_advancer=advancer or Advancer(None),
            account_switcher=switcher,
        ),
    )


async def test_the_switcher_is_selected_and_the_list_is_read() -> None:
    """落到选择页时自己选定站点，再回来读列表。"""

    switcher = ScriptedSwitcher("selected")
    page = FakePage(lands_on=SWITCHER, after_switch=LIST_URL)

    outcome = await subject(switcher).open_list(page, CA)

    assert switcher.calls == [CA]
    assert outcome.ok is True and outcome.rows_loaded is True


@pytest.mark.parametrize(
    "reason", ["not_offered", "ambiguous", "list_not_loaded", "timeout"]
)
async def test_a_site_that_cannot_be_selected_says_only_what_it_saw(reason) -> None:
    """⚠️ 不许再说「该店铺未开通此站点」—— 那是没读过就下的断言。"""

    page = FakePage(lands_on=SWITCHER)

    outcome = await subject(ScriptedSwitcher(reason)).open_list(page, CA)

    assert outcome.ok is False
    assert outcome.reason == "marketplace_not_selected"
    assert outcome.switch_reason == reason


async def test_the_switcher_is_not_mistaken_for_the_list_page() -> None:
    """切换页的 returnTo 里带着目标路径。子串匹配会把它当成「已经到了」，
    然后去一张空表上找活儿 —— 判据必须只比 path。"""

    page = FakePage(lands_on=SWITCHER)
    assert FEEDBACK_MANAGER_PATH in SWITCHER  # 前提

    outcome = await subject(ScriptedSwitcher("not_offered")).open_list(page, CA)

    assert outcome.ok is False and outcome.reason != "marketplace_unavailable"


async def test_an_expired_session_is_reported_as_auth_not_as_an_unopened_site() -> None:
    """会话掉了是会话的事。报成「没开这个站点」会把人指去查一个不存在的开店问题。"""

    page = FakePage(lands_on=SIGNIN)

    outcome = await subject(ScriptedSwitcher("selected"), Advancer(None)).open_list(page, CA)

    assert outcome.ok is False and outcome.reason == "auth_required"


async def test_a_login_the_helper_can_finish_is_not_handed_to_a_human() -> None:
    """反向：助手走得通就别打扰人。凭据全程不经过我们，它只点按钮。"""

    advancer = Advancer(LIST_URL)
    page = FakePage(lands_on=SIGNIN)

    outcome = await subject(ScriptedSwitcher("selected"), advancer).open_list(page, CA)

    assert advancer.calls == 1
    assert outcome.ok is True


async def test_landing_anywhere_else_is_its_own_reason() -> None:
    page = FakePage(lands_on=ELSEWHERE)

    outcome = await subject(ScriptedSwitcher("selected")).open_list(page, CA)

    assert outcome.ok is False and outcome.reason == "marketplace_unavailable"


async def test_a_page_handed_off_to_another_tab_is_returned_to_the_caller() -> None:
    """登录助手可能把登录收在另一个标签里。抓取必须用它返回的那个 Page，
    否则会去读被遗弃的登录页 —— 那上面一行都匹配不到，结果就是「没有待办」。"""

    landed_tab = FakePage(lands_on=LIST_URL)
    landed_tab.url = LIST_URL

    class HandsOff(Advancer):
        async def advance_or_raise(self, page, *, expected_host=None, **kwargs):
            self.calls += 1
            return landed_tab

    page = FakePage(lands_on=SIGNIN)
    outcome = await subject(ScriptedSwitcher("selected"), HandsOff(None)).open_list(page, CA)

    assert outcome.ok is True
    assert outcome.page is landed_tab


async def test_a_target_path_inside_someone_else_s_query_is_not_a_landing() -> None:
    """⚠️ 亚马逊到处把回跳路径塞进查询串。判据必须**只比 path**。

    这里单独钉它：切换页和登录页那两条路各有自己的判据挡在前面，所以只有这种
    「别的页面 + 查询串里带着目标路径」才暴露得出子串匹配的错。
    """

    page = FakePage(lands_on=f"https://{CA}/home?ref={FEEDBACK_MANAGER_PATH}")

    outcome = await subject(ScriptedSwitcher("selected")).open_list(page, CA)

    assert outcome.ok is False and outcome.reason == "marketplace_unavailable"
