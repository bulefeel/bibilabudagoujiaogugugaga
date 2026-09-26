"""打开合规页时的四路落地判据。

这一条不是防御性编程，是 2026-09-16 实地踩到的：秦登友的德国站会话一小时就掉了，
页面被重定向到 ``/ap/signin``，而那个页面上 ``div.ahd-product-policy-table-row``
匹配到 **0 个**——于是纯筛选函数报「0 候选、0 漂移、无异常」，和「这家店真的没有
待办」**一模一样**。

所以落地判据必须排在筛选之前，而且要能把四种情况分开。这里钉的就是那道分界。
"""

from __future__ import annotations

import asyncio
import pytest

from ziniao_automation.workflows.amazon_account_switcher import (
    AmazonAccountSwitcher,
    SwitchOutcome,
)
from ziniao_automation.workflows.amazon_gpsr.page import AmazonGpsrPage
from ziniao_automation.workflows.errors import DomContractError


DE = "sellercentral.amazon.de"
POLICIES = (
    "https://sellercentral.amazon.de/performance/account/health/"
    "product-policies?t=regulatory-compliance"
)
# 实测那条重定向（截断），德国站有自己的 assoc_handle。
SIGNIN = (
    "https://sellercentral.amazon.de/ap/signin?clientContext=259-9888473"
    "&openid.assoc_handle=sc_de_amazon_v2&openid.return_to=https%3A%2F%2F"
    "sellercentral.amazon.de%2Fperformance%2Faccount%2Fhealth%2Fproduct-policies"
)
SWITCHER = (
    "https://sellercentral.amazon.de/account-switcher"
    "?returnTo=/performance/account/health/product-policies"
)


class FakePage:
    """够用的假页面：能导航、能报 URL、能数行、能等选择器。

    切换页上的点击不在这里建模——那是 ``AmazonAccountSwitcher`` 自己的事，
    有它自己的测试（``tests/unit/test_account_switcher.py``）。这里注入
    ``ScriptedSwitcher``，只测合规页**怎么对待**每一种选站结果。
    """

    def __init__(
        self,
        *,
        lands_on: str = POLICIES,
        row_counts: list[int] | None = None,
        selector_appears: bool = True,
        after_switch: str = POLICIES,
    ) -> None:
        self.url = ""
        self._lands_on = lands_on
        self._row_counts = list(row_counts if row_counts is not None else [3])
        self._selector_appears = selector_appears
        # 选站成功之后再 goto 一次时落到哪里。
        self._after_switch = after_switch
        self.switched = False
        self.gotos: list[str] = []
        self.evaluations = 0

    async def goto(self, url: str, **kwargs) -> None:
        self.gotos.append(url)
        self.url = self._after_switch if self.switched else self._lands_on

    async def wait_for_timeout(self, ms: int) -> None:
        # ⚠️ 两件事都不能省(2026-09-26 自审):
        # ``await asyncio.sleep(0)`` 让出控制权 —— 不让的话协程永不回到事件循环,
        #   外层的 ``asyncio.wait_for`` 连取消都做不到,「防挂死」的测试自己挂死;
        # 预算断言把挂死变成**响亮的失败** —— 撤掉轮询的 ``step`` 下限那条修复时,
        #   我们要的是红色,不是把内存吃满(见 memory: mutation-check-hygiene)。
        self.waits = getattr(self, "waits", 0) + 1
        assert self.waits < 10_000, "轮询循环没有推进:step 下限可能被删了"
        await asyncio.sleep(0)

    async def wait_for_selector(self, selector: str, **kwargs):
        if not self._selector_appears:
            raise TimeoutError("never attached")
        return object()

    async def evaluate(self, script: str, arg=None):
        self.evaluations += 1
        if self._row_counts:
            return self._row_counts.pop(0)
        return 0


class ScriptedSwitcher(AmazonAccountSwitcher):
    """按剧本给出选站结果。成功时模拟亚马逊沿 ``returnTo`` 落回**没筛选**的合规页。"""

    def __init__(self, reason: str = "not_offered") -> None:
        super().__init__()
        self._reason = reason
        self.calls: list[tuple[str, str]] = []

    async def select_marketplace(self, page, domain, *, expected_path):
        self.calls.append((domain, expected_path))
        if self._reason == "selected":
            page.switched = True
            page.url = f"https://{domain}{expected_path}"
            return SwitchOutcome(ok=True, reason="selected", landed_url=page.url)
        if self._reason == "auth_required":
            page.url = SIGNIN
        return SwitchOutcome(ok=False, reason=self._reason, landed_url=page.url)


class GivesUpAdvancer:
    """默认的登录助手替身：走不通。

    多数用例钉的是**落地分类**，不是登录本身；用真助手会把它们变成登录测试，
    还要求假页面实现 locator()。专门测登录接线的用例自己注入别的替身。
    """

    async def advance_or_raise(self, page, *, expected_host=None, **kwargs):
        return None


def subject(switcher: AmazonAccountSwitcher | None = None) -> AmazonGpsrPage:
    # 把等待压到 0，否则每个用例要跑几十秒。
    return AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.03,
        login_advancer=GivesUpAdvancer(),
        account_switcher=switcher or ScriptedSwitcher("not_offered"),
    )


# --------------------------------------------------------------------------
async def test_a_login_redirect_is_reported_as_auth_not_as_an_empty_list() -> None:
    """⚠️ 本文件存在的理由。

    登录页上行选择器匹配 0 个。若这里只报「没有行」，调用方会把它当成
    「这家店没有待办」，任务报成功——每家店可以永远这样绿下去。
    """

    page = FakePage(lands_on=SIGNIN, row_counts=[0])
    outcome = await subject().open_policies(page, DE)

    assert outcome.ok is False
    assert outcome.reason == "auth_required"
    assert outcome.landed_url == SIGNIN


async def test_a_bounce_to_the_account_switcher_is_not_an_unopened_marketplace() -> None:
    """那是关于会话的事实，不是故障，**也不是**「这家店没开这个站点」。

    2026-09-18 正香-张刚CA 实测：登录德国站成功后落在账户切换页，而那一页上
    「德國」在列且**不是**「待註冊」——账号是有这个站点的，只是本次会话还没选定。
    这两种状态之前共用一个 reason，操作员对两者拿到同一句「未开通此站点」，
    第二种情况会被指去查一个根本不存在的开店问题。
    """

    outcome = await subject().open_policies(FakePage(lands_on=SWITCHER), DE)

    assert outcome.ok is False
    assert outcome.reason == "marketplace_not_selected"
    assert outcome.reason != "marketplace_unavailable"


async def test_the_right_page_with_rows_is_the_only_green_path() -> None:
    page = FakePage(lands_on=POLICIES, row_counts=[51])
    outcome = await subject().open_policies(page, DE)

    assert (outcome.ok, outcome.rows_loaded, outcome.reason) == (True, True, "")


async def test_the_right_page_with_no_rows_is_ok_but_not_loaded() -> None:
    """页面对、表格结构也在，只是一行都没有——这才是「真的没有待办」。

    它和登录页必须给出**不同**的结果，否则上面那条断言就白写了。
    """

    outcome = await subject().open_policies(FakePage(row_counts=[0, 0, 0]), DE)

    assert outcome.ok is True
    assert outcome.rows_loaded is False


async def test_the_right_page_whose_table_never_renders_is_a_contract_error() -> None:
    """只有这一种算契约破裂：我们确实在对的页面上，但它不长我们认识的样子。"""

    page = FakePage(lands_on=POLICIES, selector_appears=False)
    with pytest.raises(DomContractError):
        await subject().open_policies(page, DE)


async def test_auth_is_checked_before_the_path(monkeypatch) -> None:
    """URL 会**先于**内容变化，所以两道判据的先后是承重的。

    ``/ap/signin`` 也不含合规页路径，如果先判路径，登录会被误报成
    「这家店没开德国站」——操作员会去查一个根本不存在的开店问题。
    """

    outcome = await subject().open_policies(FakePage(lands_on=SIGNIN), DE)
    assert outcome.reason == "auth_required"
    assert outcome.reason != "marketplace_unavailable"


async def test_the_scraper_makes_no_judgements() -> None:
    """``read_rows`` 必须原样返回页面给的东西。

    多写一个 if，就等于把一条判断放进永远测不到的代码里（夹具没有页面级
    evaluate）。全部判断都归 ``select_actionable``。
    """

    payload = [{"cells": ["x"], "buttonId": None, "inAccordionHeader": False}]

    class Scraping(FakePage):
        async def evaluate(self, script: str, arg=None):
            return payload

    assert await subject().read_rows(Scraping()) == payload


async def test_read_rows_tolerates_an_empty_page() -> None:
    class Empty(FakePage):
        async def evaluate(self, script: str, arg=None):
            return None

    assert await subject().read_rows(Empty()) == []


async def test_a_zero_settle_interval_cannot_spin_forever() -> None:
    """等待步长必须有下限。

    ``settle_seconds=0`` 时 ``waited += step`` 永远加 0，轮询循环再也出不来——
    服务会挂在一个「只读」的等待里，没有超时、没有日志、没有任何征兆。第一次
    给这个文件跑测试时就是这样卡住的。
    """

    import asyncio

    page = FakePage(row_counts=[0, 0, 0, 0])
    outcome = await asyncio.wait_for(
        AmazonGpsrPage(settle_seconds=0.0, list_timeout_seconds=0.2).open_policies(
            page, DE
        ),
        timeout=10,
    )
    assert outcome.ok is True


async def test_the_target_path_inside_a_query_parameter_does_not_count() -> None:
    """账户切换页把目标路径原样放在 returnTo 里。

    拿整个 URL 做子串匹配，就会把账户切换页当成「页面正常」，然后跑去
    一张空表上找候选——又一个静默失败。判据必须只比 path 部分。
    """

    assert "product-policies" in SWITCHER  # 前提：目标路径确实出现在 URL 里
    outcome = await subject().open_policies(FakePage(lands_on=SWITCHER), DE)
    assert outcome.ok is False
    assert outcome.reason == "marketplace_not_selected"


async def test_a_trailing_slash_is_still_the_right_page() -> None:
    """尾斜杠不该被当成换了页面。"""

    landed = POLICIES.replace("?t=", "/?t=")
    outcome = await subject().open_policies(
        FakePage(lands_on=landed, row_counts=[7]), DE
    )
    assert (outcome.ok, outcome.rows_loaded) == (True, True)


async def test_the_login_advancer_is_used_before_giving_up() -> None:
    """落在登录页时先让现成的登录助手去点按钮，而不是直接转人工。

    这套点击逻辑（邮箱 Continue、紫鸟托管 Passkey、已填密码、已填 OTP、两步验证
    方式选择页）在提现流程里已经实现并验证过很久了。不接上它，德国站每次会话过期
    都要人工介入——而德国站会话掉得特别快。
    """

    calls: list[str] = []

    class Advancer:
        async def advance_or_raise(self, page, *, expected_host=None, **kw):
            calls.append(expected_host)
            page._lands_on = POLICIES  # 助手点完之后页面回到业务页
            page.url = POLICIES
            return page

    page = FakePage(lands_on=SIGNIN, row_counts=[5])
    adapter = AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.2, login_advancer=Advancer()
    )
    outcome = await adapter.open_policies(page, DE)

    assert calls == [DE], "必须把站点域名传给助手，它要据此校验落地主机"
    assert (outcome.ok, outcome.rows_loaded) == (True, True)


async def test_a_handed_off_tab_is_carried_back_to_the_caller() -> None:
    """⚠️ 亚马逊会把登录收在**新标签**里，并故意把原标签留在 /ap/signin。

    ``ListOutcome`` 不把新 Page 带回去的话，抓取会在那个被遗弃的登录页上进行——
    一行都匹配不到，于是报成「这家店没有待办」。又一个静默失败。
    """

    resolved = FakePage(lands_on=POLICIES, row_counts=[9])
    resolved.url = POLICIES

    class HandingOff:
        async def advance_or_raise(self, page, *, expected_host=None, **kw):
            return resolved  # 换了一个 Page 回来

    original = FakePage(lands_on=SIGNIN)
    outcome = await AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.2, login_advancer=HandingOff()
    ).open_policies(original, DE)

    assert outcome.ok is True
    assert outcome.page is resolved, "带回去的必须是助手解析出的那个标签"
    assert outcome.page is not original


async def test_an_unresolvable_login_still_goes_to_a_human() -> None:
    """助手用尽预算也走不通（非托管 Passkey、CAPTCHA、候选不唯一）时不许猜。

    猜错会给卖家真实手机发消息或打电话。
    """

    class GivesUp:
        async def advance_or_raise(self, page, *, expected_host=None, **kw):
            return None

    outcome = await AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.2, login_advancer=GivesUp()
    ).open_policies(FakePage(lands_on=SIGNIN), DE)

    assert outcome.ok is False
    assert outcome.reason == "auth_required"


async def test_a_business_page_never_invokes_the_advancer() -> None:
    """已经在业务页上就别去碰登录助手——那是多余的点击风险。"""

    class MustNotRun:
        async def advance_or_raise(self, page, *, expected_host=None, **kw):
            raise AssertionError("业务页不该调用登录助手")

    outcome = await AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.2, login_advancer=MustNotRun()
    ).open_policies(FakePage(lands_on=POLICIES, row_counts=[3]), DE)
    assert outcome.rows_loaded is True


def test_the_german_site_is_in_the_login_allow_lists() -> None:
    """⚠️ 同一个坑第三次。

    业务页不在白名单里，登录其实成功了也会被判成「需要人工」——结算页漏过一次、
    反馈管理页漏过一次。德国站还要额外加主机，因为它有自己的 assoc_handle。
    """

    from ziniao_automation.workflows.amazon_login import (
        AmazonLoginAdvancer,
        _BUSINESS_EXACT_PATHS,
        _SELLER_CENTRAL_HOSTS,
    )
    from ziniao_automation.workflows.amazon_gpsr.config import PRODUCT_POLICIES_PATH

    assert "sellercentral.amazon.de" in _SELLER_CENTRAL_HOSTS
    assert PRODUCT_POLICIES_PATH in _BUSINESS_EXACT_PATHS
    # 端到端：带着查询串的真实 URL 必须被认成业务页。
    assert AmazonLoginAdvancer.is_supported_business_url(
        POLICIES, expected_host=DE
    )
    # 反向：别的站点的同一路径不该因此被放行到德国站。
    assert not AmazonLoginAdvancer.is_supported_business_url(
        POLICIES, expected_host="sellercentral.amazon.ca"
    )


# --------------------------------------------------------------------------
# 打开安全证明面板
# --------------------------------------------------------------------------
class PanelPage(FakePage):
    """会分两拍渲染的假侧栏：先出骨架，状态后到。"""

    def __init__(self, *, states: list[dict], cards_before: int = 0) -> None:
        super().__init__()
        self._states = list(states)
        self._cards_before = cards_before
        self.clicked: list[str] = []

    async def click(self, selector: str, **kwargs) -> None:
        self.clicked.append(selector)

    async def evaluate(self, script: str, arg=None):
        # _PANEL_STATE 带 panelFrom 辅助函数；_CARD_COUNT 没有。
        if "panelFrom" not in script:
            return self._cards_before if not self.clicked else 1
        if self._states:
            return self._states.pop(0)
        return {"ok": False, "reason": "exhausted"}


def panel_adapter() -> AmazonGpsrPage:
    return AmazonGpsrPage(
        settle_seconds=0.0, panel_timeout_seconds=0.5,
        login_advancer=GivesUpAdvancer(),
    )


async def test_the_panel_status_is_awaited_not_read_too_early() -> None:
    """⚠️ 首次实战就栽在这：侧栏骨架先渲染、``kat-label[text]`` 后到。

    只等「面板出现」的话会读到一个空状态，然后按「状态不认识」拒绝提交——看起来
    像判据出错，其实是退出轮询太早。状态也必须等出来。
    """

    page = PanelPage(states=[
        {"ok": True, "asin": "B0GX12HL64", "status": ""},      # 骨架
        {"ok": True, "asin": "B0GX12HL64", "status": ""},
        {"ok": True, "asin": "B0GX12HL64", "status": "未开始"},  # 状态到了
    ])
    state = await panel_adapter().open_attestation_panel(page, "B0GX12HL64")

    assert state.ok is True
    assert state.status == "未开始"


async def test_a_panel_for_another_product_is_refused() -> None:
    """⚠️ 防错位的核心：面板自报的 ASIN 必须等于目标。

    实测面板头部会写出自己的 ASIN 且只有一个，所以这是真实依据，不是靠点击顺序推断。
    """

    page = PanelPage(states=[{"ok": True, "asin": "B0OTHER123", "status": "未开始"}])
    state = await panel_adapter().open_attestation_panel(page, "B0GX12HL64")

    assert state.ok is False
    assert state.reason == "panel_asin_mismatch"


async def test_an_already_open_panel_blocks_opening_another() -> None:
    """上一条的侧栏没关就开下一条，正是「点开 A 的面板、提交到 B 头上」的温床。"""

    page = PanelPage(states=[], cards_before=1)
    state = await panel_adapter().open_attestation_panel(page, "B0GX12HL64")

    assert state.ok is False
    assert state.reason == "panel_already_open"
    assert page.clicked == [], "面板已开时连点都不该点"


async def test_a_panel_that_never_settles_is_refused() -> None:
    """状态一直读不出来就不做。未知 ≠ 可以提交。"""

    page = PanelPage(states=[{"ok": True, "asin": "B0GX12HL64", "status": ""}] * 20)
    state = await panel_adapter().open_attestation_panel(page, "B0GX12HL64")
    # ok 但状态为空 —— 调用方会据此拒绝（gpsr_status_unknown）。
    assert state.status == ""


# --------------------------------------------------------------------------
# 收回安全态：先点关闭，再退回刷新
# --------------------------------------------------------------------------
class SafeStatePage(FakePage):
    """可编程的侧栏：记录点击与导航，按脚本决定侧栏还开不开。"""

    def __init__(self, *, open_after: list[bool], close_rect=None) -> None:
        super().__init__()
        self._open_after = list(open_after)
        self._close_rect = close_rect
        self.clicks: list[tuple[float, float]] = []
        self.navigations = 0
        self.url = POLICIES

        outer = self

        class Mouse:
            async def click(self, x, y):
                outer.clicks.append((x, y))

        self.mouse = Mouse()

    async def goto(self, url, **kwargs):
        self.navigations += 1
        self.url = POLICIES

    async def evaluate(self, script, arg=None):
        if "closeLabel" in script:
            return self._close_rect
        if self._open_after:
            return self._open_after.pop(0)
        return False


def safe_state_adapter() -> AmazonGpsrPage:
    return AmazonGpsrPage(settle_seconds=0.0, login_advancer=GivesUpAdvancer())


async def test_a_closed_panel_needs_no_action_at_all() -> None:
    """侧栏本来就没开，就别多点也别刷新——每一次多余的动作都是自动化特征。"""

    page = SafeStatePage(open_after=[False])
    assert await safe_state_adapter().return_to_list(page, DE) is True
    assert page.clicks == []
    assert page.navigations == 0


async def test_clicking_close_is_preferred_over_reloading() -> None:
    """⚠️ 运营 2026-09-17 提的：每条都整页刷新，连续九次，自动化特征太明显。

    页面本来就给了那颗「关闭」按钮，能点就点。
    """

    page = SafeStatePage(
        open_after=[True, False],  # 开着 → 点完就没了
        close_rect={"x": 1670.0, "y": 1010.0},
    )
    assert await safe_state_adapter().return_to_list(page, DE) is True
    assert page.clicks == [(1670.0, 1010.0)]
    assert page.navigations == 0, "点关闭成功了就不该再刷新"


async def test_a_failed_close_falls_back_to_reloading() -> None:
    """点了没用就退回刷新。

    「关闭」那个文案是从截图读的、还没实地验证过——所以这条退路必须在，
    而且判据说了算：点完不算数，判据说侧栏没了才算。
    """

    page = SafeStatePage(
        open_after=[True, True, False],  # 开 → 点完还开 → 刷完没了
        close_rect={"x": 5.0, "y": 5.0},
    )
    assert await safe_state_adapter().return_to_list(page, DE) is True
    assert page.clicks, "该先试着点一下"
    assert page.navigations == 1


async def test_no_close_button_goes_straight_to_reloading() -> None:
    """找不到唯一的「关闭」按钮就别乱点，直接刷新。"""

    page = SafeStatePage(open_after=[True, False], close_rect=None)
    assert await safe_state_adapter().return_to_list(page, DE) is True
    assert page.clicks == []
    assert page.navigations == 1


async def test_a_panel_that_survives_everything_is_reported() -> None:
    """两条路都没收回安全态 —— 必须如实返回 False，由调用方停掉整站。

    静默放过的话，后面每一条都会去点被侧栏盖住的按钮然后超时。
    """

    page = SafeStatePage(open_after=[True, True, True], close_rect={"x": 1.0, "y": 1.0})
    assert await safe_state_adapter().return_to_list(page, DE) is False


# --------------------------------------------------------------------------
# 零条记录的店铺 —— 2026-09-17 秦登美实测暴露的假故障
# --------------------------------------------------------------------------
class CountingPage(FakePage):
    """能分别回答「骨架在不在」「几行」「页面文字」的假页面。"""

    def __init__(self, *, rows: int, chip: str | None, wrapper: bool = True) -> None:
        super().__init__(lands_on=POLICIES)
        self._rows = rows
        self._chip = chip
        self._wrapper = wrapper

    async def wait_for_selector(self, selector: str, **kwargs):
        if "ahd-product-policy-table" in selector and not self._wrapper:
            raise TimeoutError("骨架没出现")
        return object()

    async def evaluate(self, script: str, arg=None):
        if "innerText" in script and "document.body" in script:
            return f"应用的筛选条件: 监管合规性 ({self._chip})" if self._chip else ""
        return self._rows


def counting_adapter() -> AmazonGpsrPage:
    return AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.2,
        login_advancer=GivesUpAdvancer(),
    )


async def test_a_store_with_zero_compliance_issues_is_not_a_failure() -> None:
    """⚠️ 这是最健康的状态，不是故障。

    2026-09-17 秦登美实测：页面正常、表头在、筛选器写着「监管合规性 (0)」、
    一行都没有。原来等的是「行」，等不到就抛契约错误——把零问题判成了故障。
    德国站要铺到所有账号，这种店只会越来越多。
    """

    outcome = await counting_adapter().open_policies(
        CountingPage(rows=0, chip="0"), DE
    )
    assert outcome.ok is True
    assert outcome.rows_loaded is False


async def test_a_chip_that_disagrees_with_zero_rows_is_drift() -> None:
    """⚠️ 徽章是**亚马逊自己给出的权威条数**。

    它说有 14 条而我们一行都没读到，那就是结构漂移——绝不能报成「没有待办」，
    那会让这家店永远显示成功。
    """

    with pytest.raises(DomContractError, match="14"):
        await counting_adapter().open_policies(CountingPage(rows=0, chip="14"), DE)


async def test_an_unreadable_chip_does_not_invent_a_failure() -> None:
    """读不到徽章时不瞎猜。零行 + 没有反证 = 正常空列表。"""

    outcome = await counting_adapter().open_policies(
        CountingPage(rows=0, chip=None), DE
    )
    assert (outcome.ok, outcome.rows_loaded) == (True, False)


async def test_a_missing_table_skeleton_is_still_a_contract_error() -> None:
    """骨架都没有才算页面变了——这条闸不能因为上面的放宽而丢掉。"""

    with pytest.raises(DomContractError):
        await counting_adapter().open_policies(
            CountingPage(rows=0, chip="0", wrapper=False), DE
        )


async def test_rows_present_never_consults_the_chip() -> None:
    """读到行了就照常干活，徽章对不对得上不影响（它每天都在变）。"""

    outcome = await counting_adapter().open_policies(
        CountingPage(rows=72, chip="14"), DE
    )
    assert (outcome.ok, outcome.rows_loaded) == (True, True)


class _SafeStatePage:
    """收尾判据用的假页面：URL 可控，侧栏开关可控。"""

    def __init__(self, *, url: str, panel_open: bool, reload_lands_on: str | None = None):
        self.url = url
        self.panel_open = panel_open
        self._reload_lands_on = reload_lands_on
        self.reloads = 0
        self.clicks = 0
        self.mouse = self

    async def evaluate(self, expression: str, *args):
        if "为什么您需要提供警告和安全信息" in expression:
            return self.panel_open
        if "closeLabel" in expression or "getBoundingClientRect" in expression:
            return None          # 没有可点的关闭按钮 → 直接走重新加载那条路
        return None

    async def click(self, x, y):
        self.clicks += 1

    async def goto(self, url: str, **kwargs) -> None:
        self.reloads += 1
        if self._reload_lands_on is not None:
            self.url = self._reload_lands_on

    async def wait_for_timeout(self, ms: int) -> None:
        # ⚠️ 两件事都不能省(2026-09-26 自审):
        # ``await asyncio.sleep(0)`` 让出控制权 —— 不让的话协程永不回到事件循环,
        #   外层的 ``asyncio.wait_for`` 连取消都做不到,「防挂死」的测试自己挂死;
        # 预算断言把挂死变成**响亮的失败** —— 撤掉轮询的 ``step`` 下限那条修复时,
        #   我们要的是红色,不是把内存吃满(见 memory: mutation-check-hygiene)。
        self.waits = getattr(self, "waits", 0) + 1
        assert self.waits < 10_000, "轮询循环没有推进:step 下限可能被删了"
        await asyncio.sleep(0)


async def test_a_closed_panel_on_the_login_page_is_not_a_safe_state() -> None:
    """⚠️ 安全态是「回到了合规列表页」，不是「没有侧栏」。

    德国站会话掉得很快，而一次 AUTO 要连做十几条、每条都重新导航一次。被弹回登录
    页时侧栏判据同样返回 false（登录页上既没有决议卡、正文也不匹配），于是收尾被
    判成「安全」、循环继续，下一条去点一个根本不存在的行按钮、等满 25 秒超时，
    站点判 FAILED —— 而报出来的原因和真实原因毫无关系。
    """

    page = _SafeStatePage(url=SIGNIN, panel_open=False, reload_lands_on=SIGNIN)
    assert await AmazonGpsrPage().return_to_list(page, DE) is False


async def test_being_back_on_the_policies_page_with_no_panel_is_safe() -> None:
    """反向验证：真的回到列表页且侧栏关掉了，必须判安全。"""

    page = _SafeStatePage(url=POLICIES, panel_open=False)
    assert await AmazonGpsrPage().return_to_list(page, DE) is True
    assert page.reloads == 0, "已经在安全态就不该再整页刷新"


async def test_a_panel_still_open_on_the_policies_page_is_not_safe() -> None:
    """另一半也要守住：还在合规页但侧栏盖着行按钮，同样不能继续。

    2026-09-17 首次 AUTO 实机测试里，第一条提交成功之后整站失败就是这么来的。
    """

    page = _SafeStatePage(url=POLICIES, panel_open=True, reload_lands_on=POLICIES)
    assert await AmazonGpsrPage().return_to_list(page, DE) is False
    assert page.reloads == 1, "关不掉时必须退回重新加载那条路"


# --------------------------------------------------------------------------
# 账户/站点切换页：自己把站点选掉
# --------------------------------------------------------------------------
async def test_the_switcher_is_operated_and_the_run_continues() -> None:
    """落到切换页时自己选定目标站点，别把一个纯粹的视图切换变成人工活。

    ``returnTo`` 只带路径、不带 ``?t=regulatory-compliance``：选完之后必须**再开一次
    带筛选的地址**，否则读的是没筛选的合规页。
    """

    switcher = ScriptedSwitcher("selected")
    page = FakePage(lands_on=SWITCHER, row_counts=[3])
    outcome = await subject(switcher).open_policies(page, DE)

    assert switcher.calls == [(DE, "/performance/account/health/product-policies")]
    assert page.gotos == [POLICIES, POLICIES], "选完要带筛选重开一次"
    assert outcome.ok is True
    assert outcome.rows_loaded is True


@pytest.mark.parametrize(
    "reason",
    ["not_offered", "ambiguous", "obstructed", "selection_not_registered", "timeout"],
)
async def test_a_switch_that_does_not_complete_is_skipped_with_its_reason(reason) -> None:
    """选不掉 → 照旧跳过，而且把**为什么**带出去（进事件 details），不重开页面。"""

    page = FakePage(lands_on=SWITCHER)
    outcome = await subject(ScriptedSwitcher(reason)).open_policies(page, DE)

    assert outcome.ok is False
    assert outcome.reason == "marketplace_not_selected"
    assert outcome.switch_reason == reason
    assert page.gotos == [POLICIES]


async def test_a_bounce_to_sign_in_during_the_switch_is_auth() -> None:
    """确认之后被弹去登录页 —— 会话问题，交给人工验证，不是「停在选择页」。"""

    page = FakePage(lands_on=SWITCHER)
    outcome = await subject(ScriptedSwitcher("auth_required")).open_policies(page, DE)

    assert outcome.ok is False
    assert outcome.reason == "auth_required"


async def test_a_bounce_to_sign_in_on_the_filtered_reopen_is_auth() -> None:
    page = FakePage(lands_on=SWITCHER, after_switch=SIGNIN)
    outcome = await subject(ScriptedSwitcher("selected")).open_policies(page, DE)

    assert outcome.ok is False
    assert outcome.reason == "auth_required"


async def test_landing_back_on_the_switcher_after_a_switch_is_not_selected() -> None:
    """选完重开又落回切换页：还是「没选定」，不能报成「别的页面」，也不再点第二次。"""

    switcher = ScriptedSwitcher("selected")
    page = FakePage(lands_on=SWITCHER, after_switch=SWITCHER)
    outcome = await subject(switcher).open_policies(page, DE)

    assert outcome.ok is False
    assert outcome.reason == "marketplace_not_selected"
    assert outcome.switch_reason == "bounced_back"
    assert len(switcher.calls) == 1


class BouncingSwitcher(ScriptedSwitcher):
    """选站成功，但亚马逊随即要求该站点自己的登录（每站 assoc_handle 不同）。"""

    def __init__(self, lands_on_after_login: str) -> None:
        super().__init__("selected")
        self.lands_on_after_login = lands_on_after_login

    async def select_marketplace(self, page, domain, *, expected_path):
        self.calls.append((domain, expected_path))
        # 站点其实已经选上了(所以之后重开会落到合规页),只是亚马逊要求该站点登录。
        page.switched = True
        page.url = SIGNIN
        return SwitchOutcome(ok=False, reason="auth_required", landed_url=SIGNIN)


class WorkingAdvancer:
    """登录助手替身：把登录页推进到 ``lands_on``。"""

    def __init__(self, lands_on: str | None) -> None:
        self.lands_on = lands_on
        self.calls = 0

    async def advance_or_raise(self, page, *, expected_host=None, **kwargs):
        self.calls += 1
        if self.lands_on is None:
            return None
        page.url = self.lands_on
        return page


async def test_a_login_demanded_by_the_new_marketplace_is_handled_not_parked() -> None:
    """切过去之后要求登录是**常态**（每个站点各有 assoc_handle），而那正是登录助手
    的活。原来直接返回 auth_required、一次都不试：AUTO 运行停 30 分钟等人，还占着
    唯一的队列 worker（提现也排在后面）（2026-09-26 自审）。"""

    switcher = BouncingSwitcher(POLICIES)
    advancer = WorkingAdvancer(POLICIES)
    page = FakePage(lands_on=SWITCHER, row_counts=[3])
    subject = AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.03,
        login_advancer=advancer, account_switcher=switcher,
    )

    outcome = await subject.open_policies(page, DE)

    assert advancer.calls == 1, "必须让登录助手试一次"
    assert outcome.ok is True and outcome.rows_loaded is True


async def test_a_login_the_helper_cannot_finish_still_goes_to_a_human() -> None:
    """反向：助手走不通才交给人，而且只试一次，不绕圈。"""

    switcher = BouncingSwitcher(POLICIES)
    advancer = WorkingAdvancer(None)
    page = FakePage(lands_on=SWITCHER)
    subject = AmazonGpsrPage(
        settle_seconds=0.0, list_timeout_seconds=0.03,
        login_advancer=advancer, account_switcher=switcher,
    )

    outcome = await subject.open_policies(page, DE)

    assert advancer.calls == 1
    assert outcome.ok is False and outcome.reason == "auth_required"


def test_the_unsafe_reason_says_what_it_saw() -> None:
    """收不回安全态的原因不止一种。德国站会话掉得快，重新加载列表页经常直接落到
    登录页——那时报「侧栏未能关闭」和真实原因毫无关系（2026-09-26 自审）。"""

    subject = AmazonGpsrPage(settle_seconds=0.0)
    cases = {
        SIGNIN: "auth_required",
        SWITCHER: "marketplace_not_selected",
        "https://sellercentral.amazon.de/home": "left_the_page",
        POLICIES: "panel_stuck",
    }
    for url, expected in cases.items():
        page = FakePage(lands_on=url)
        page.url = url  # 收尾判据读的是**当前** URL,不经过 goto
        assert subject.unsafe_reason(page) == expected, url
