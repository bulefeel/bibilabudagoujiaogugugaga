"""提现流程落到账户/站点切换页时的处理（用户 2026-09-23 拍板：提现也自动选站）。

挂在 ``_raise_if_auth`` —— 每次导航之后的唯一收口点，挂一处就全覆盖。三件事要钉住：

1. 选得定 → 落回 ``returnTo`` 那一页，接着往下走；
2. 选不定 → ``HumanAuthRequired``（转人工、选完点继续），**绝不**变成「跳过该站点」；
3. 两处**一下都不点**：ARMED 之后的 ``submit_once``，以及故意打开切换页读卖家身份的
   ``_detect_identity_from_account_switcher``。
"""

from __future__ import annotations

import pytest

from ziniao_automation.workflows.amazon_account_switcher import (
    AmazonAccountSwitcher,
    SwitchOutcome,
)
from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.types import MarketplaceRef

from .fixture_dom import FixturePage


CA = MarketplaceRef(
    id="market-ca", code="CA", domain="sellercentral.amazon.ca", currency="CAD"
)
DASHBOARD_PATH = "/payments/dashboard/index.html"
SWITCHER = (
    "https://sellercentral.amazon.ca/account-switcher/default/merchantMarketplace"
    "?returnTo=%2Fpayments%2Fdashboard%2Findex.html"
)


SIGNIN = "https://sellercentral.amazon.ca/ap/signin?openid.assoc_handle=sc_ca"


class ScriptedSwitcher(AmazonAccountSwitcher):
    def __init__(self, reason: str = "selected") -> None:
        super().__init__()
        self._reason = reason
        self.calls: list[tuple[str, str]] = []

    async def select_marketplace(self, page, domain, *, expected_path):
        self.calls.append((domain, expected_path))
        if self._reason == "selected":
            page.url = f"https://{domain}{expected_path}"
            return SwitchOutcome(ok=True, reason="selected", landed_url=page.url)
        if self._reason == "auth_required":
            page.url = SIGNIN
        return SwitchOutcome(ok=False, reason=self._reason, landed_url=page.url)


class LoginAdvancer:
    """登录助手替身:把登录页推进到 ``lands_on``(与真助手同签名)。"""

    def __init__(self, lands_on: str) -> None:
        self.lands_on = lands_on
        self.calls = 0

    async def advance_or_raise(
        self, page, *, expected_host=None, allow_payment_details_handoff=False
    ):
        self.calls += 1
        page.url = self.lands_on
        return page


def _adapter(switcher: ScriptedSwitcher) -> AmazonPaymentsPage:
    return AmazonPaymentsPage(account_switcher=switcher, pacing_range_ms=(0, 0))


def _page(url: str = SWITCHER) -> FixturePage:
    return FixturePage("<html><body>Seller Central</body></html>", url)


async def test_a_switcher_landing_is_selected_and_the_flow_continues() -> None:
    switcher = ScriptedSwitcher("selected")
    page = _page()

    resolved = await _adapter(switcher)._raise_if_auth(page, CA)

    assert switcher.calls == [("sellercentral.amazon.ca", DASHBOARD_PATH)], (
        "目标路径取 URL 里的 returnTo，不猜"
    )
    assert resolved is page
    assert resolved.url == f"https://sellercentral.amazon.ca{DASHBOARD_PATH}"


@pytest.mark.parametrize(
    "reason", ["not_offered", "ambiguous", "selection_not_registered", "timeout"]
)
async def test_a_switch_that_does_not_complete_goes_to_a_human(reason) -> None:
    """⚠️ 不能变成「跳过」：那会把只差人工选一次站的有余额站点报成「本次没有发起提现」。"""

    switcher = ScriptedSwitcher(reason)

    with pytest.raises(HumanAuthRequired) as caught:
        await _adapter(switcher)._raise_if_auth(_page(), CA)

    # 不叫 sign_in:这不是登录问题,运行详情和卡片要说对是哪一步卡住了。
    assert caught.value.kind == "marketplace_switch"
    assert caught.value.diagnostic.reason_code == {
        "not_offered": "MARKETPLACE_NOT_OFFERED",
        "ambiguous": "MARKETPLACE_AMBIGUOUS",
        "selection_not_registered": "MARKETPLACE_ACTION_UNAVAILABLE",
        "timeout": "NAVIGATION_TIMEOUT",
    }[reason]


async def test_without_return_to_nothing_is_clicked_and_a_human_decides() -> None:
    switcher = ScriptedSwitcher("selected")
    page = _page(
        "https://sellercentral.amazon.ca/account-switcher/default/merchantMarketplace"
    )

    with pytest.raises(HumanAuthRequired):
        await _adapter(switcher)._raise_if_auth(page, CA)

    assert switcher.calls == []


async def test_the_opt_out_never_touches_the_switcher() -> None:
    """``submit_once`` 就是这么调的：ARMED 之后不在那个窗口里多做一次导航。

    （``submit_once`` 确实传了 False，由 ``test_details_and_submit_safety`` 里的
    ``switcher_flags`` 断言钉住。）
    """

    switcher = ScriptedSwitcher("selected")
    page = _page()

    resolved = await _adapter(switcher)._raise_if_auth(
        page, CA, select_marketplace_on_switcher=False
    )

    assert switcher.calls == []
    assert resolved.url == SWITCHER


async def test_a_sign_in_bounce_after_confirming_goes_back_through_the_login_helper() -> None:
    """确认选站之后亚马逊要求重新登录:站点其实已经选上了。登录助手就是干这个的 ——
    走它一次,而不是报「未能自动选定站点」、叫人去选一个屏幕上根本没有的站
    (09-23 自审复核)。"""

    switcher = ScriptedSwitcher("auth_required")
    advancer = LoginAdvancer(f"https://sellercentral.amazon.ca{DASHBOARD_PATH}")
    adapter = AmazonPaymentsPage(
        account_switcher=switcher, login_advancer=advancer, pacing_range_ms=(0, 0)
    )
    page = _page()

    resolved = await adapter._raise_if_auth(page, CA)

    assert len(switcher.calls) == 1
    assert advancer.calls == 1
    assert resolved.url.endswith(DASHBOARD_PATH)


async def test_a_second_bounce_goes_to_a_human_instead_of_looping() -> None:
    switcher = ScriptedSwitcher("auth_required")
    advancer = LoginAdvancer(SWITCHER)  # 登录完又落回切换页,再选又被弹
    adapter = AmazonPaymentsPage(
        account_switcher=switcher, login_advancer=advancer, pacing_range_ms=(0, 0)
    )

    with pytest.raises(HumanAuthRequired) as caught:
        await adapter._raise_if_auth(_page(), CA)

    assert "再次要求登录" in str(caught.value)
    assert len(switcher.calls) == 2 and advancer.calls == 1


class _IdentityPage(FixturePage):
    """故意打开的切换页：上面列着当前卖家账号。"""

    async def goto(self, url, **kwargs):
        self.url = SWITCHER

    async def wait_for_function(self, expression, **kwargs):
        return None


async def test_identity_detection_reads_the_switcher_without_selecting_anything() -> None:
    switcher = ScriptedSwitcher("selected")
    page = _IdentityPage(
        "<html><body><button>Acme Trading (当前)</button></body></html>", ""
    )

    identity, source = await _adapter(switcher)._detect_identity_from_account_switcher(
        page, CA
    )

    assert switcher.calls == [], "这一页是故意打开来读身份的，绝不能在这里点国家"
    assert (identity, source) == ("Acme Trading", "account_switcher:current")


class _DashboardRedirectsToSwitcher(FixturePage):
    """付款面板被重定向到切换页,切换页上列着当前卖家账号。"""

    async def goto(self, url, **kwargs):
        self.url = SWITCHER

    async def wait_for_function(self, expression, **kwargs):
        return None


async def test_the_store_setup_identity_probe_never_selects_a_marketplace() -> None:
    """建档探针是只读的:付款面板被弹到切换页时,它从那一页读当前账号,绝不点国家。

    ⚠️ 默认选站让它在选不掉时抛「需要人工验证」,探针就卡进「等待登录」的提示里,
    永远轮不到下一个站点(09-23 第二轮复核)。
    """

    switcher = ScriptedSwitcher("not_offered")
    page = _DashboardRedirectsToSwitcher(
        "<html><body><button>Acme Trading (当前)</button></body></html>", ""
    )

    identity, _ = await _adapter(switcher).detect_identity(page, CA)

    assert switcher.calls == []
    assert identity == "Acme Trading"


async def test_an_auto_run_records_why_a_site_needed_a_human() -> None:
    """自动模式跳过要人处理的站点时,**原因**要留在站点错误和事件里:
    卡在登录还是卡在选站,处理方式完全不同(09-23 第二轮复核:只留了 kind)。"""

    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )
    from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
    from ziniao_automation.workflows.types import RunMode, SiteStatus

    from .test_disbursement_recovery import make_run

    class _KeepsSiteErrors(InMemoryWorkflowRepository):
        """⚠️ 内存仓储的 ``set_site_status`` 把 ``error`` 直接 ``del`` 掉,而生产的
        SQLAlchemy 仓储会写进 ``SiteRun.error`` —— 卡片和运行详情页读的就是它。
        用原版替身断言,等于这半个修复根本没被钉住(2026-09-26 自审)。"""

        def __init__(self) -> None:
            super().__init__()
            self.site_errors: dict[str, str | None] = {}

        async def set_site_status(self, run_id, marketplace_code, status, *, error=None):
            self.site_errors[marketplace_code] = error
            await super().set_site_status(run_id, marketplace_code, status)

    repo = _KeepsSiteErrors()
    run = make_run(RunMode.AUTO, "run-auto-drop")
    await repo.add_run(run)
    workflow = AmazonDisbursementWorkflow(repository=repo, page_adapter=object())
    reason = HumanAuthRequired(
        "页面停在亚马逊账户/站点选择页，未能自动选定 CA（not_offered）",
        kind="marketplace_switch",
    )

    dropped = await workflow._auto_drops_site_needing_human(
        run, run.marketplaces[0], reason
    )

    assert dropped is True
    assert await repo.get_site_status(run.id, "CA") is SiteStatus.NEEDS_HUMAN_AUTH
    stored = repo.site_errors["CA"]
    assert stored and stored.startswith("自动模式已跳过该站点")
    assert "not_offered" in stored and "marketplace_switch" in stored
    (event,) = [e for e in repo.events if e["event_type"] == "site_needs_human_auth"]
    assert "not_offered" in event["message"]
    assert event["details"]["reason"] == str(reason)
    # 自动模式不等人:不能留一句「请…后继续」,那里根本没有「继续」可点。
    assert "后继续" not in event["message"]


async def test_switcher_refusals_state_facts_not_a_continue_instruction() -> None:
    """「去紫鸟里处理、回后台继续」由等待卡片来说;写进异常就会在自动模式里被原样
    记成一句无处可「继续」的指示(09-23 第三轮复核)。"""

    for reason in ("not_offered", "timeout"):
        with pytest.raises(HumanAuthRequired) as caught:
            await _adapter(ScriptedSwitcher(reason))._raise_if_auth(_page(), CA)
        assert "后继续" not in str(caught.value)


class _ContractWatchingAdapter(AmazonPaymentsPage):
    """记录「落地之后有没有重新等页面契约」。"""

    def __init__(self, switcher, advancer) -> None:
        super().__init__(
            account_switcher=switcher, login_advancer=advancer, pacing_range_ms=(0, 0)
        )
        self.waited_for: list[str] = []

    async def _wait_for_dashboard_contract(self, page) -> None:
        self.waited_for.append("dashboard")

    async def _wait_for_statements_contract(self, page) -> None:
        self.waited_for.append("statements")

    async def _wait_for_assisted_login_dom_handoff(self, page) -> None:
        return None

    async def _settle(self, page) -> None:
        return None


async def test_a_page_brought_back_by_the_login_helper_waits_for_its_contract() -> None:
    """⚠️ 落地页的内容是异步画上去的。

    不等的话 ``lookup_existing`` 会在结算表还没渲染时就去找记录，读到 NOT_FOUND，
    「亚马逊那边今天已经有同额记录」这道点击前的平台侧防重直接失效，面板照样按下去
    （2026-09-26 自审）。直接选站那条路径本来就等，登录这条漏了。
    """

    STATEMENTS = "https://sellercentral.amazon.ca/payments/allstatements/index.html"
    advancer = LoginAdvancer(STATEMENTS)
    adapter = _ContractWatchingAdapter(ScriptedSwitcher("selected"), advancer)
    page = _page("https://sellercentral.amazon.ca/ap/signin?openid.assoc_handle=sc_ca")

    resolved = await adapter._raise_if_auth(page, CA)

    assert advancer.calls == 1
    assert resolved.url == STATEMENTS
    assert adapter.waited_for == ["statements"], "落地到结算页就要等结算页的契约"


async def test_a_page_that_never_moved_is_not_waited_on_again() -> None:
    """反向：这一趟没换过页面就不必重等，别白白多花一次等待窗口。"""

    adapter = _ContractWatchingAdapter(ScriptedSwitcher("selected"), LoginAdvancer(""))
    page = _page("https://sellercentral.amazon.ca/payments/dashboard/index.html")

    await adapter._raise_if_auth(page, CA)

    assert adapter.waited_for == []
