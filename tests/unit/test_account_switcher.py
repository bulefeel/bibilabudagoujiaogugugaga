"""账户/站点切换页的选站锚点。

⚠️ DOM 里**没有逐行的 marketplace id**（2026-09-18 实地量的，见模块 docstring），
所以选国家只能用按钮文案。既然如此就必须 fail-closed，这里钉的就是那几道闸。

⚠️ 这一页是**两步**：点国家只是选中，页面不导航；要再点一颗 ``<kat-button>``
确认按钮才真的切站。第一版漏了第二步，表现为「点了、选中态也变了、页面就是不动」。
假页面必须照着两步建模，否则这块测试是假绿。

⚠️ 2026-09-23 自审：原来「整串精确 + 唯一」写在 JS 里，替身直接返回写好的
``{"ok": True, ...}``，那道闸**一次都没被执行过**。现在 JS 只回按钮文字，判断在
``pick_marketplace``；替身照着真实页面建模一份按钮列表，让判断真的跑起来。
"""

from __future__ import annotations

import asyncio
import pytest
from html import escape
from tests.workflows.fixture_dom import FixturePage

from ziniao_automation.workflows.amazon_account_switcher import (
    CONFIRM_BUTTON_SELECTOR,
    MARKETPLACE_BUTTON_SELECTOR,
    MARKETPLACE_NAMES,
    AmazonAccountSwitcher,
    pick_marketplace,
)


DE = "sellercentral.amazon.de"
POLICIES_PATH = "/performance/account/health/product-policies"
SWITCHER = (
    "https://sellercentral.amazon.de/account-switcher/default/merchantMarketplace"
    "?returnTo=%2Fperformance%2Faccount%2Fhealth%2Fproduct-policies"
)
POLICIES = f"https://sellercentral.amazon.de{POLICIES_PATH}?t=regulatory-compliance"
SIGNIN = "https://sellercentral.amazon.de/ap/signin?openid.assoc_handle=x"

# 2026-09-18 正香那一页上实地出现过的写法（节选）。
LISTING = ["加拿大", "英國", "德國", "澳洲", "日本 (待註冊)"]
CONFIRM_XY = (500.0, 760.0)


def _xy(index: int) -> tuple[float, float]:
    return (10.0, 20.0 + 40.0 * index)


class _Page:
    """照着实测行为建模的切换页：一列带文字的按钮 + 选中态 + 一颗确认按钮。

    * 点国家 → 那颗进入选中态（``radio=False`` 时之前选中的不会被取消），页面**不**导航；
    * 点确认 → 落到 ``lands_on``。
    """

    def __init__(
        self,
        buttons: list[str] | None = None,
        *,
        confirm: dict | None = None,
        selects: bool = True,
        radio: bool = True,
        preselected: set[int] | None = None,
        obstructed: bool = False,
        reorder_before_click: bool = False,
        empty_reads: int = 0,
        partial_first_read: bool = False,
        lands_on: str | None = POLICIES,
    ) -> None:
        self.url = SWITCHER
        self.buttons = list(LISTING if buttons is None else buttons)
        self._confirm = confirm if confirm is not None else {
            "ok": True, "x": CONFIRM_XY[0], "y": CONFIRM_XY[1]
        }
        self._selects = selects
        self._radio = radio
        self.selected: set[int] = set(preselected or ())
        self._obstructed = obstructed
        self._reorder = reorder_before_click
        # 登录之后账户列表是异步到达的:前几次读到空表。
        self._empty_reads = empty_reads
        # 列表渲染到一半:第一次只读到前两个国家(还没有德國)。
        self._partial_first_read = partial_first_read
        self._lands_on = lands_on
        self.clicks: list[tuple[float, float]] = []
        self.mouse = self

    def locator(self, selector):
        if selector == ".full-page-account-switcher":
            texts = self.buttons
            if self._empty_reads > 0:
                self._empty_reads -= 1
                texts = []
            elif self._partial_first_read:
                self._partial_first_read = False
                texts = self.buttons[:2]
            countries = ''.join(
                '<div class="full-page-account-switcher-account"><button class="full-page-account-switcher-account-details">'
                + escape(text) + '</button></div>' for text in texts)
            html = ('<body><div class="full-page-account-switcher"><div class="full-page-account-switcher-inner">'
                    '<div class="full-page-account-switcher-accounts-wrapper"><div class="full-page-account-switcher-accounts">'
                    '<div class="full-page-account-switcher-account"><button class="full-page-account-switcher-account-details">'
                    'Fixture (current)</button><div class="full-page-account-switcher-accounts">' + countries
                    + '</div></div></div></div></div></div></body>')
            self._dom = FixturePage(html, self.url)
        return self._dom.locator(selector)

    async def evaluate(self, script: str, arg=None):
        # 按**调用约定**分派，不按脚本字面：参数的形状就是契约。
        if arg == MARKETPLACE_BUTTON_SELECTOR:
            if self._empty_reads > 0:
                self._empty_reads -= 1
                return []
            if self._partial_first_read:
                self._partial_first_read = False
                return list(self.buttons[:2])
            return list(self.buttons)
        if arg == CONFIRM_BUTTON_SELECTOR:
            return self._confirm
        if isinstance(arg, dict) and "index" in arg:
            # 照 _RECT_AT 的契约:只报事实(那一格此刻的文字、坐标、是否在最上层),
            # 不替生产代码做判断。
            if self._reorder:
                self.buttons.append(self.buttons.pop(0))
            index = arg["index"]
            if index >= len(self.buttons):
                return {"text": None}
            x, y = _xy(index)
            return {
                "text": self.buttons[index],
                "x": x,
                "y": y,
                "onTop": not self._obstructed,
            }
        if isinstance(arg, dict) and "selectedClass" in arg:
            return [self.buttons[i] for i in sorted(self.selected)]
        raise AssertionError(f"未建模的 evaluate 调用: {arg!r}")

    async def click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == CONFIRM_XY:
            if self._lands_on is not None:
                self.url = self._lands_on
            return
        for index in range(len(self.buttons)):
            if (x, y) == _xy(index) and self._selects:
                if self._radio:
                    self.selected = {index}
                else:
                    self.selected.add(index)

    async def wait_for_timeout(self, ms: int) -> None:
        # ⚠️ 两件事都不能省(2026-09-26 自审):
        # ``await asyncio.sleep(0)`` 让出控制权 —— 不让的话协程永不回到事件循环,
        #   外层的 ``asyncio.wait_for`` 连取消都做不到,「防挂死」的测试自己挂死;
        # 预算断言把挂死变成**响亮的失败** —— 撤掉轮询的 ``step`` 下限那条修复时,
        #   我们要的是红色,不是把内存吃满(见 memory: mutation-check-hygiene)。
        self.waits = getattr(self, "waits", 0) + 1
        assert self.waits < 10_000, "轮询循环没有推进:step 下限可能被删了"
        await asyncio.sleep(0)


def _switcher() -> AmazonAccountSwitcher:
    return AmazonAccountSwitcher(settle_seconds=0.0, timeout_seconds=0.2)


# ---------------------------------------------------------------------------
# 纯函数：fail-closed 的那道闸
# ---------------------------------------------------------------------------


def test_pick_marketplace_takes_only_one_exact_match() -> None:
    names = MARKETPLACE_NAMES[DE]

    assert pick_marketplace(LISTING, names) == (2, "")
    # 带后缀的不算：那是「账号没开这个站点」
    assert pick_marketplace(["德國 (待註冊)", "加拿大"], names) == (None, "registration_required")
    # 部分匹配不算
    assert pick_marketplace(["德國站", "新德國"], names) == (None, "not_offered")
    # 多账号卖家同一国家出现两次：点哪个都可能错
    assert pick_marketplace(["德國", "加拿大", "德国"], names) == (None, "ambiguous")
    assert pick_marketplace([], names) == (None, "not_offered")


def test_the_switcher_url_is_recognised_by_whole_path_segments() -> None:
    assert AmazonAccountSwitcher.is_switcher_url(SWITCHER)
    assert AmazonAccountSwitcher.is_switcher_url("https://x/account-switcher")
    # 前缀不能退化成子串
    assert not AmazonAccountSwitcher.is_switcher_url("https://x/account-switcherX")
    assert not AmazonAccountSwitcher.is_switcher_url("https://x/evil/account-switcher")
    assert not AmazonAccountSwitcher.is_switcher_url(POLICIES)


def test_return_path_trusts_only_a_single_same_site_absolute_path() -> None:
    """``returnTo`` 是亚马逊写的「选完回哪」。拿不准就返回 None，调用方不猜。"""

    assert AmazonAccountSwitcher.return_path(SWITCHER) == POLICIES_PATH
    base = "https://sellercentral.amazon.ca/account-switcher/default/merchantMarketplace"
    assert AmazonAccountSwitcher.return_path(base) is None
    assert AmazonAccountSwitcher.return_path(f"{base}?returnTo=https%3A%2F%2Fevil.example%2Fx") is None
    assert AmazonAccountSwitcher.return_path(f"{base}?returnTo=%2F%2Fevil.example%2Fx") is None
    assert AmazonAccountSwitcher.return_path(f"{base}?returnTo=home") is None
    assert AmazonAccountSwitcher.return_path(f"{base}?returnTo=%2Fa&returnTo=%2Fb") is None


# ---------------------------------------------------------------------------
# 两步点击
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_steps_run_on_the_right_country_and_landing_is_what_counts() -> None:
    """点**德國那一颗** → 确认 → 落地。三件事都要发生，而且判据是最后那一件。"""

    page = _Page()

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == [_xy(LISTING.index("德國")), CONFIRM_XY], "必须两步都点，且点对国家"
    assert outcome.ok is True
    assert outcome.reason == "selected"
    assert outcome.landed_url == POLICIES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("buttons", "reason"),
    [
        (["加拿大", "德國 (待註冊)"], "registration_required"),
        (["德國", "加拿大", "德國"], "ambiguous"),
    ],
)
async def test_anything_less_than_one_exact_match_clicks_nothing(buttons, reason) -> None:
    """⚠️ 点错国家比不点严重得多。拿不准就一下都不点。"""

    page = _Page(buttons)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == []
    assert outcome.ok is False
    assert outcome.reason == reason
    assert outcome.offered == len(buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"obstructed": True}, "obstructed"),
        # 两次 evaluate 之间列表重排：拿旧下标去点会点到别的国家
        ({"reorder_before_click": True}, "list_changed"),
    ],
)
async def test_a_target_that_cannot_be_clicked_safely_clicks_nothing(kwargs, reason) -> None:
    page = _Page(**kwargs)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == []
    assert outcome.reason == reason


@pytest.mark.asyncio
async def test_a_list_that_arrives_late_is_waited_for() -> None:
    """登录之后落到切换页时,账户列表异步到达。只读一次会读到空表,把「列表还没
    加载」说成「这个国家不在列表里」(09-23 自审复核)。"""

    page = _Page(empty_reads=3)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert outcome.ok is True
    assert page.clicks == [_xy(LISTING.index("德國")), CONFIRM_XY]


@pytest.mark.asyncio
async def test_a_list_that_never_arrives_is_not_reported_as_not_offered() -> None:
    page = _Page(empty_reads=10_000)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == []
    assert outcome.reason == "list_not_loaded"


def test_the_last_gate_before_clicking_is_decided_in_python() -> None:
    """JS 只报事实;「那一格还是不是目标」「有没有被盖住」在这里判,测得到。"""

    from ziniao_automation.workflows.amazon_account_switcher import _click_target

    assert _click_target({"text": "德國", "onTop": True}, "德國") == (True, "")
    assert _click_target({"text": "加拿大", "onTop": True}, "德國") == (False, "list_changed")
    assert _click_target({"text": None}, "德國") == (False, "list_changed")
    assert _click_target({"text": "德國", "onTop": False}, "德國") == (False, "obstructed")


@pytest.mark.asyncio
async def test_clicking_the_country_alone_is_not_success() -> None:
    """⚠️ 第一版就死在这：选中态变了、页面不动，却当成切站成功。

    确认按钮是 ``<kat-button>``，普通 ``button`` 选择器看不见它。
    """

    page = _Page(confirm={"ok": False, "reason": "confirm_not_unique", "count": 0})

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == [_xy(2)], "找不到确认按钮就不该再点别的"
    assert outcome.ok is False
    assert outcome.reason == "confirm_not_unique"


@pytest.mark.asyncio
async def test_a_country_click_that_does_not_register_never_reaches_confirm() -> None:
    """点了国家但没进入选中态 —— 绝不能接着去点确认。

    那一下确认会把**当前选中的那个国家**切过去，而那可能是别的国家。
    """

    page = _Page(selects=False)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == [_xy(2)]
    assert outcome.reason == "selection_not_registered"


@pytest.mark.asyncio
async def test_a_stale_selection_of_another_country_blocks_confirm() -> None:
    """目标选中了、但上次选中的加拿大没被取消 —— 确认会切到哪一个说不准，不点。"""

    page = _Page(radio=False, preselected={LISTING.index("加拿大")})

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == [_xy(2)]
    assert outcome.reason == "selection_not_registered"


@pytest.mark.asyncio
async def test_a_confirm_without_a_landing_is_a_timeout_not_a_success() -> None:
    """判据盯**结果态**：点了不算数，落到目标路径才算。"""

    page = _Page(lands_on=None)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert page.clicks == [_xy(2), CONFIRM_XY]
    assert outcome.ok is False
    assert outcome.reason == "timeout"


@pytest.mark.asyncio
async def test_a_bounce_to_sign_in_after_confirm_is_reported_as_auth() -> None:
    """确认之后被弹去登录页：是会话问题，要报成 ``auth_required``，不是「停在选择页」。"""

    page = _Page(lands_on=SIGNIN)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert outcome.ok is False
    assert outcome.reason == "auth_required"
    assert outcome.landed_url == SIGNIN


@pytest.mark.asyncio
async def test_an_unknown_domain_refuses_instead_of_guessing() -> None:
    """没登记过文案的站点：显式拒绝，不许拿别的名字去凑。"""

    page = _Page()

    outcome = await _switcher().select_marketplace(
        page, "sellercentral.amazon.co.jp", expected_path=POLICIES_PATH
    )

    assert page.clicks == []
    assert outcome.reason == "unknown_marketplace"


def test_no_registered_name_carries_a_pending_registration_suffix() -> None:
    """名字必须是**干净的国家名**。

    切换页把未注册的站点写成「日本 (待註冊)」。整串精确匹配因此天然排除它们 ——
    前提是登记的名字里不带任何后缀。写进带括号的变体就会把这道分界拆掉。
    """

    for domain, names in MARKETPLACE_NAMES.items():
        for name in names:
            assert "(" not in name and "（" not in name, (domain, name)
            assert name == name.strip(), (domain, name)


@pytest.mark.asyncio
async def test_a_half_rendered_list_is_not_judged() -> None:
    """第一次只读到一半(还没有德國),若第一次非空就判,会报「列表里没有这个国家」。
    判据是**连续两次同一份**非空列表。"""

    page = _Page(partial_first_read=True)

    outcome = await _switcher().select_marketplace(
        page, DE, expected_path=POLICIES_PATH
    )

    assert outcome.ok is True, outcome
    assert page.clicks[0] == _xy(LISTING.index("德國"))


@pytest.mark.asyncio
async def test_inactive_account_finishes_as_known_blocker_not_timeout(caplog):
    page = _Page(lands_on=f"https://{DE}/inactiveAccount?token=PRIVATE_TOKEN")
    outcome = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert not outcome.ok
    assert outcome.reason == "inactive_account"
    assert outcome.page_category == "INACTIVE_ACCOUNT"
    assert page.clicks == [_xy(2), CONFIRM_XY]
    assert page.waits <= 2, "known landing must not consume the navigation timeout"
    assert "PRIVATE_TOKEN" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    f"https://{DE}.evil.invalid{POLICIES_PATH}",
    f"http://{DE}{POLICIES_PATH}",
    f"https://user:secret@{DE}{POLICIES_PATH}",
    f"https://{DE}:8765{POLICIES_PATH}",
    "https://[bad/url",
])
async def test_matching_path_on_wrong_origin_never_counts_as_landing(url):
    page = _Page(lands_on=url)
    outcome = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert not outcome.ok
    assert outcome.reason == "domain_mismatch"
    assert outcome.page_category == "DOMAIN_MISMATCH"


@pytest.mark.asyncio
async def test_wrong_origin_before_selection_clicks_nothing():
    page = _Page()
    page.url = "https://evil.invalid/account-switcher/default/merchantMarketplace"
    outcome = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert outcome.reason == "domain_mismatch"
    assert page.clicks == []


@pytest.mark.asyncio
async def test_unknown_landing_timeout_logs_no_raw_path_or_query(caplog):
    page = _Page(lands_on=f"https://{DE}/PRIVATE_SELLER?token=PRIVATE_TOKEN")
    outcome = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert outcome.reason == "timeout"
    assert outcome.page_category == "UNKNOWN_PAGE"
    assert "PRIVATE_SELLER" not in caplog.text
    assert "PRIVATE_TOKEN" not in caplog.text
    assert "UNKNOWN_PAGE" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("text,reason", [
    ("德国（待注册）", "registration_required"),
    ("Germany (Pending registration)", "registration_required"),
    ("德国（不可用）", "unavailable"),
    ("Germany (unknown status)", "layout_unknown"),
])
async def test_explicit_country_states_never_click(text, reason):
    page = _Page([text])
    result = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert not result.ok and result.reason == reason
    assert page.clicks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["德国（当前）", "Germany (current)", "德國"])
async def test_current_and_available_country_use_existing_two_step_navigation(text):
    page = _Page([text])
    result = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert result.ok
    assert page.clicks == [_xy(0), CONFIRM_XY]


@pytest.mark.asyncio
async def test_unknown_tree_contract_does_not_fall_back_to_global_buttons():
    page = _Page()
    original = page.locator
    def unknown(selector):
        if selector == '.full-page-account-switcher':
            page._dom = FixturePage('<body><button>Germany</button></body>', page.url)
            return page._dom.locator(selector)
        if not hasattr(page, '_dom'):
            return original(selector)
        return page._dom.locator(selector)
    page.locator = unknown
    result = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert result.reason == 'layout_unknown'
    assert page.clicks == []


@pytest.mark.asyncio
async def test_snapshot_read_hang_is_bounded_before_any_click(monkeypatch):
    import ziniao_automation.workflows.amazon_account_switcher as module
    page = _Page()
    cancelled = asyncio.Event()
    async def hung(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(module, 'read_switcher_snapshot', hung)
    result = await asyncio.wait_for(_switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH), timeout=1)
    assert not result.ok
    assert cancelled.is_set()
    assert result.reason == 'list_not_loaded'
    assert page.clicks == []


@pytest.mark.asyncio
async def test_changed_owner_before_confirmation_is_not_confirmed(monkeypatch):
    import ziniao_automation.workflows.amazon_account_switcher as module
    from dataclasses import replace
    from ziniao_automation.workflows.amazon_switcher_contract import AccountCandidate
    original = module.read_switcher_snapshot
    page = _Page()
    async def changed(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        if page.clicks:
            snapshot = replace(snapshot, accounts=(AccountCandidate('Changed (current)'),))
        return snapshot
    monkeypatch.setattr(module, 'read_switcher_snapshot', changed)
    result = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert result.reason == 'list_changed'
    assert page.clicks == [_xy(2)]


@pytest.mark.asyncio
async def test_login_during_list_load_is_handed_back_without_waiting_for_structure(monkeypatch):
    import ziniao_automation.workflows.amazon_account_switcher as module
    original = module.read_switcher_snapshot
    page = _Page()
    async def redirects(*args, **kwargs):
        result = await original(*args, **kwargs)
        page.url = SIGNIN
        return result
    monkeypatch.setattr(module, 'read_switcher_snapshot', redirects)
    result = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert result.reason == 'auth_required'
    assert page.clicks == []
    assert page.waits <= 1


@pytest.mark.asyncio
async def test_domain_change_after_hit_testing_still_prevents_the_first_click():
    page = _Page()
    original = page.evaluate
    async def external(script, arg=None):
        result = await original(script,arg)
        if isinstance(arg,dict) and 'index' in arg:
            page.url = 'https://other.invalid/'
        return result
    page.evaluate = external
    result = await _switcher().select_marketplace(page, DE, expected_path=POLICIES_PATH)
    assert result.reason == 'domain_mismatch'
    assert page.clicks == []
