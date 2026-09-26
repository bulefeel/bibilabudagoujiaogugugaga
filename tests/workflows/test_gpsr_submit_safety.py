"""提交安全证明:整条流程唯一不可逆的动作。

勾那个框是向亚马逊断言「本商品不需要警告和安全信息」,没有撤销、没有逐条人工闸门
(运营决定 1)。所以这里钉的不是「功能能跑」,而是**什么情况下绝不许点下去**。

⚠️ 假页面故意"会反抗":``save()`` 自己重新核对面板归属与勾选状态,不满足就抛。
这样测试变绿才真的意味着不变量成立,而不是我们自己声明它成立。
"""

from __future__ import annotations

import pytest

from ziniao_automation.workflows.amazon_gpsr.config import (
    STATUS_ACTIONABLE,
    STATUS_SUBMITTED,
)
from ziniao_automation.workflows.amazon_gpsr.page import (
    ListOutcome,
    PanelState,
    SaveOutcome,
)
from ziniao_automation.workflows.amazon_gpsr.workflow import AmazonGpsrWorkflow
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.types import RunMode, SiteStatus, WorkflowPlan

from .test_gpsr_plan import DE, RecordingRepository, make_run
from .test_gpsr_row_selection import live_page


class Adapter:
    """会反抗的假页面适配器。

    它记录每一次动作,并且在 ``save_attestation`` 里**自己再核对一遍**:面板必须是
    为这个 ASIN 打开的,而且必须真的勾过。生产代码若跳过任一步,这里直接抛
    AssertionError,而不是悄悄返回成功。
    """

    def __init__(
        self,
        *,
        rows=None,
        outcome: ListOutcome | None = None,
        statuses: dict[str, str] | None = None,
        panel_refuses: set[str] | None = None,
        tick_fails: set[str] | None = None,
        save_fails: set[str] | None = None,
    ) -> None:
        self.rows = rows if rows is not None else live_page()
        self.outcome = outcome or ListOutcome(ok=True, rows_loaded=True)
        self.statuses = statuses or {}
        self.panel_refuses = panel_refuses or set()
        self.tick_fails = tick_fails or set()
        self.save_fails = save_fails or set()

        self.opened: list[str] = []
        self.ticked: list[str] = []
        self.saved: list[str] = []
        self.closed = 0
        self.evidence: list[str] = []
        # 保存成功但卡片状态不翻的 ASIN——亚马逊在保存时返回校验错误/5xx 时就是
        # 这样：click() 不抛异常，页面上什么都没变。
        self.confirm_fails: set[str] = set()
        self._open_asin: str | None = None
        self._ticked_for: str | None = None

    async def open_policies(self, page, domain):
        return self.outcome

    async def read_rows(self, page):
        return self.rows

    async def open_attestation_panel(self, page, asin) -> PanelState:
        assert self._open_asin is None, "上一条的侧栏没关就开了下一条"
        self.opened.append(asin)
        if asin in self.panel_refuses:
            return PanelState(ok=False, reason="panel_asin_mismatch")
        self._open_asin = asin
        return PanelState(
            ok=True, asin=asin, status=self.statuses.get(asin, STATUS_ACTIONABLE)
        )

    async def tick_attestation(self, page) -> bool:
        assert self._open_asin is not None, "没开面板就去勾选"
        if self._open_asin in self.tick_fails:
            return False
        self.ticked.append(self._open_asin)
        self._ticked_for = self._open_asin
        return True

    async def capture_evidence(self, page, run, marketplace_code, *, label):
        self.evidence.append(label)
        return None

    async def save_attestation(self, page, asin) -> SaveOutcome:
        # ⚠️ 反抗:保存前自己复核,不信调用方。
        assert self._open_asin == asin, (
            f"要保存 {asin},但打开的面板属于 {self._open_asin}"
        )
        assert self._ticked_for == asin, f"{asin} 还没勾就要保存"
        if asin in self.save_fails:
            return SaveOutcome(ok=False, reason="save_disabled")
        self.saved.append(asin)
        # 亚马逊的真实行为（2026-09-17 实测）：保存成功后卡片状态翻成「审核中」。
        # 替身必须照着来，否则「回读确认」那条锚点在测试里永远读到旧状态。
        if asin not in self.confirm_fails:
            self.statuses[asin] = STATUS_SUBMITTED
        return SaveOutcome(ok=True)

    async def return_to_list(self, page, domain) -> bool:
        self.closed += 1
        self._open_asin = None
        self._ticked_for = None
        return True


def build(**kwargs):
    repo = RecordingRepository()
    return repo, AmazonGpsrWorkflow(repository=repo, page_adapter=Adapter(**kwargs))


def auto_run():
    from dataclasses import replace

    return replace(make_run(), mode=RunMode.AUTO)


EMPTY_PLAN = WorkflowPlan("amazon_gpsr_attestation", "gpsr-run", "3", ())


# --------------------------------------------------------------------------
async def test_a_dry_run_never_reaches_the_irreversible_click() -> None:
    """引擎本来就会短路 DRY_RUN,但这条流程自己也要拦一道。

    模式判断写在 execute 最前面,是为了「万一将来有人给它加了别的模式」。
    """

    repo, workflow = build()
    assert await workflow.execute(make_run(), object(), EMPTY_PLAN) == ()
    assert workflow.page_adapter.saved == []
    assert workflow.page_adapter.opened == []


async def test_execute_never_returns_an_operation_record() -> None:
    """写一条 operation_guards 会把任务拖进 UNCERTAIN_FINANCIAL,
    然后在资金审核台上渲染成一笔提现。这条流程一分钱都不碰。
    """

    repo, workflow = build()
    assert await workflow.execute(auto_run(), object(), EMPTY_PLAN) == ()


async def test_each_candidate_is_submitted_exactly_once() -> None:
    repo, workflow = build()
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert adapter.saved == ["B0HJG2CJ5F", "B0GXZ1ZZLT"]
    # 已下架的那条连面板都不该打开。
    assert "B0GWCRRDZS" not in adapter.opened
    # 每开一次面板都要关一次,页面必须回到安全态。
    assert adapter.closed == len(adapter.opened)
    assert ("DE", SiteStatus.CONFIRMED) in repo.statuses


async def test_evidence_is_captured_before_every_save() -> None:
    """运营 2026-09-16 要求留证:点下去之前拍一张现场。

    事后补不了——那一刻页面长什么样,只有那一刻能记下来。
    """

    repo, workflow = build()
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert len(adapter.evidence) == len(adapter.saved)
    for asin in adapter.saved:
        assert f"attest-{asin}" in adapter.evidence


# --------------------------------------------------------------------------
# 幂等:状态写在亚马逊页面上,所以这条流程不需要自己的台账表
# --------------------------------------------------------------------------
async def test_an_item_already_under_review_is_not_submitted_again() -> None:
    repo, workflow = build(statuses={"B0HJG2CJ5F": STATUS_SUBMITTED})
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert "B0HJG2CJ5F" not in adapter.saved
    assert "B0HJG2CJ5F" not in adapter.ticked
    assert repo.of("gpsr_already_submitted")
    # 但它不该拖累同站点的其它条目。
    assert "B0GXZ1ZZLT" in adapter.saved


async def test_an_unrecognised_card_status_is_never_treated_as_done() -> None:
    """既不是「未开始」也不是「审核中」——不认识的状态不许猜。

    当成已完成会让这一条永远没人提交;当成可提交会在一个未知状态上按下不可逆的按钮。
    两个都不行,所以它既不提交、也不算完成。
    """

    repo, workflow = build(statuses={"B0HJG2CJ5F": "某个没见过的状态"})
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert "B0HJG2CJ5F" not in adapter.saved
    assert repo.of("gpsr_status_unknown")


# --------------------------------------------------------------------------
# 爆炸半径:运营不设条数上限,金丝雀是唯一剩下的闸
# --------------------------------------------------------------------------
async def test_the_first_failure_stops_the_whole_site() -> None:
    """⚠️ 第一条没确认落地就停整站。

    运营决定不设单次提交上限,所以如果判据本身出了错,没有任何东西能阻止它把同一个
    错误连做十遍。第一条必须确认,才允许做第二条。
    """

    repo, workflow = build(save_fails={"B0HJG2CJ5F"})
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert adapter.saved == []
    assert adapter.opened == ["B0HJG2CJ5F"], "第一条失败后不该再碰第二条"
    assert repo.of("gpsr_canary_failed")
    assert ("DE", SiteStatus.FAILED) in repo.statuses
    assert ("DE", SiteStatus.CONFIRMED) not in repo.statuses


async def test_a_later_failure_does_not_stop_the_rest() -> None:
    """金丝雀只管第一条。第一条确认之后,后面某条失败只跳过它自己。"""

    repo, workflow = build(save_fails={"B0GXZ1ZZLT"})
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert adapter.saved == ["B0HJG2CJ5F"]
    # 第一条开了两次：一次提交、一次回读确认（「发起动作」和「确认到达」是
    # 两个锚点）。第二条只开了一次，因为它在保存那一步就被拒了。
    assert adapter.opened == ["B0HJG2CJ5F", "B0HJG2CJ5F", "B0GXZ1ZZLT"]
    assert repo.of("gpsr_canary_failed") == []
    # ⚠️ 但站点不能再记成功:一条没做成就报「安全证明已完成 / 无需操作」是假话
    # (09-23 第二轮复核)。FAILED → 运行 PARTIAL,原因写在站点级事件里。
    assert repo.statuses[-1] == ("DE", SiteStatus.FAILED)
    (incomplete,) = repo.of("gpsr_site_incomplete")
    assert incomplete["details"]["confirmed"] == 1
    assert incomplete["details"]["unresolved"] == 1


@pytest.mark.parametrize(
    "kwargs, event",
    [
        ({"panel_refuses": {"B0HJG2CJ5F"}}, "gpsr_panel_refused"),
        ({"tick_fails": {"B0HJG2CJ5F"}}, "gpsr_tick_failed"),
        ({"save_fails": {"B0HJG2CJ5F"}}, "gpsr_save_refused"),
    ],
    ids=["panel-mismatch", "tick-not-confirmed", "save-refused"],
)
async def test_every_refusal_is_recorded_and_submits_nothing(kwargs, event) -> None:
    repo, workflow = build(**kwargs)
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert workflow.page_adapter.saved == []
    assert repo.of(event)


async def test_the_panel_is_closed_even_when_an_item_fails() -> None:
    """中途失败也要把页面收回安全态。

    留着半开的侧栏,下一条就会在一个不属于它的面板上开始——那正是「点开 A 的面板、
    提交到 B 头上」的温床。假适配器在 ``open_attestation_panel`` 里直接断言这件事。
    """

    repo, workflow = build(tick_fails={"B0HJG2CJ5F"})
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)
    assert workflow.page_adapter.closed >= 1


# --------------------------------------------------------------------------
# 人工验证与防重放
# --------------------------------------------------------------------------
async def test_human_auth_is_reraised_not_swallowed_as_a_site_failure() -> None:
    """⚠️ ``HumanAuthRequired`` 是 ``RuntimeError`` 的子类。

    被站点隔离的 ``except Exception`` 吞掉的话,「需要人工验证」会被记成「站点失败」,
    引擎的人工接管路径就再也不会触发——操作员永远等不到那张卡片。
    """

    repo, workflow = build(
        outcome=ListOutcome(ok=False, reason="auth_required", landed_url="https://x/ap/signin")
    )
    with pytest.raises(HumanAuthRequired):
        await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert repo.of("gpsr_site_failed") == []
    assert ("DE", SiteStatus.FAILED) not in repo.statuses


async def test_a_replayed_run_does_not_submit_the_same_asin_twice() -> None:
    """⚠️ 引擎在人工验证完成后会**从头重跑** plan + execute。

    此时亚马逊的卡片状态未必已经翻成「审核中」,所以页面那层的幂等在这段窗口里
    是靠不住的。这个集合是那段窗口里唯一挡住重复不可逆提交的东西。

    构造时**故意不让状态翻成已提交**,否则这条测试会因为别的原因变绿,把集合删掉
    也照样通过——那就是假绿。
    """

    repo, workflow = build()
    page = object()
    await workflow.execute(auto_run(), page, EMPTY_PLAN)
    first = list(workflow.page_adapter.saved)
    assert first

    # 同一个 Page 上重放一次;卡片状态仍然是「未开始」。
    # ⚠️ 替身在保存时会把状态翻成「审核中」(照亚马逊的真实行为),不清掉的话第二次
    # 是**页面那层**挡住的,把防重放集合删掉这条照样绿(2026-09-23 自审实测)。
    workflow.page_adapter.statuses.clear()
    await workflow.execute(auto_run(), page, EMPTY_PLAN)
    assert workflow.page_adapter.saved == first, "同一个 ASIN 被提交了两次"
    # 续跑时这些条目是「本次提交过」,不是「没有新提交」。
    assert repo.statuses[-1] == ("DE", SiteStatus.CONFIRMED)


async def test_a_new_run_starts_a_fresh_ledger() -> None:
    """换了任务就是新的一轮,不能因为上一次记过就再也不提交了。

    ⚠️ 作用域必须是 run,不能按 Page 弱引用键控:不可弱引用的对象会退化成 id(),
    而 CPython 会**复用 id**——新页面拿到上一个页面的集合,该提交的条目被静默跳过。
    """

    from dataclasses import replace

    repo, workflow = build()
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)
    count = len(workflow.page_adapter.saved)
    assert count

    # 亚马逊此刻还没把卡片翻成「审核中」——这正是防重放集合存在的那段窗口
    # （引擎在人工验证完成后会从头重跑 plan+execute）。清掉页面侧的幂等，
    # 让这条测试只钉「台账是不是按 run 作用域」这一件事。
    workflow.page_adapter.statuses.clear()

    later = replace(auto_run(), id="gpsr-run-2")
    await workflow.execute(later, object(), EMPTY_PLAN)
    assert len(workflow.page_adapter.saved) == count * 2


# --------------------------------------------------------------------------
async def test_a_site_error_is_isolated_and_does_not_fail_the_run() -> None:
    """一个站点炸了,别的站点还要继续跑。"""

    class Boom(Adapter):
        async def open_policies(self, page, domain):
            raise RuntimeError("页面炸了")

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(repository=repo, page_adapter=Boom())
    # 不抛出,而是把这个站点记成失败。
    assert await workflow.execute(auto_run(), object(), EMPTY_PLAN) == ()
    assert repo.of("gpsr_site_failed")
    assert ("DE", SiteStatus.FAILED) in repo.statuses


# --------------------------------------------------------------------------
# 侧栏关不掉 —— 2026-09-17 实机测试栽的地方
# --------------------------------------------------------------------------
class StickyAdapter(Adapter):
    """第一条提交成功，但侧栏关不掉。

    这就是实机上真实发生的事：面板留在屏幕上，下一条去点被它盖住的行按钮，
    30 秒超时，整站判失败——而且没有任何事件说明为什么。
    """

    def __init__(self, *, stuck_after: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stuck_after = stuck_after

    async def return_to_list(self, page, domain) -> bool:
        self.closed += 1
        closed = self._open_asin != self.stuck_after
        self._open_asin = None
        self._ticked_for = None
        return closed


async def test_a_panel_that_will_not_close_stops_the_site() -> None:
    """关不掉侧栏 = 页面没回到安全态，后面每一条都注定失败。

    继续跑只会把一条真实原因淹没在一串「面板打不开」里。
    """

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=StickyAdapter(stuck_after="B0HJG2CJ5F")
    )
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    # 第一条确实提交成功了——问题出在它之后。
    assert adapter.saved == ["B0HJG2CJ5F"]
    assert adapter.opened == ["B0HJG2CJ5F"], "侧栏没关就不该再开下一条"
    assert repo.of("gpsr_panel_stuck")
    assert ("DE", SiteStatus.FAILED) in repo.statuses
    assert ("DE", SiteStatus.CONFIRMED) not in repo.statuses


async def test_a_closable_panel_lets_the_run_continue() -> None:
    """反向验证：能正常关闭时不许误停。"""

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=StickyAdapter(stuck_after="(从不卡住)")
    )
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert workflow.page_adapter.saved == ["B0HJG2CJ5F", "B0GXZ1ZZLT"]
    assert repo.of("gpsr_panel_stuck") == []
    assert ("DE", SiteStatus.CONFIRMED) in repo.statuses


async def test_the_stuck_flag_does_not_leak_between_items() -> None:
    """卡住标记必须每条重置，否则一次卡住会永久停掉之后所有运行。"""

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=StickyAdapter(stuck_after="(从不卡住)")
    )
    workflow._panel_stuck = True  # 上一轮留下的脏状态
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)
    assert workflow.page_adapter.saved == ["B0HJG2CJ5F", "B0GXZ1ZZLT"]


# --------------------------------------------------------------------------
async def test_a_save_that_never_lands_is_not_reported_as_submitted() -> None:
    """⚠️ 整条流程唯一不可逆的动作，成功判据不能是「我点了保存」。

    亚马逊在保存时返回校验错误或 5xx 时，``save.click()`` 照样不抛异常、页面上
    什么都没变（卡片仍停在「未开始」）。旧版在这里无条件记「已提交安全证明」、
    返回成功，于是金丝雀判通过，剩下的全部沿同一条坏路径做完，站点 CONFIRMED、
    飞书绿卡「GPSR 安全证明已完成」。

    判据必须盯**结果态**：卡片翻成「审核中」才算落地。读不到确认时给的是
    ``unconfirmed`` 而不是失败——请求可能真的发出去了，说成「没提交」和说成
    「已提交」一样是假话；但金丝雀把它当未通过，对不可逆动作拿不准就该停。
    """

    repo, workflow = build()
    workflow.page_adapter.confirm_fails = {"B0HJG2CJ5F"}
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    adapter = workflow.page_adapter
    assert adapter.saved == ["B0HJG2CJ5F"]          # 点是点了
    assert repo.of("gpsr_submitted") == []          # 但绝不许说「已提交」
    assert repo.of("gpsr_save_unconfirmed")         # 必须留痕
    assert repo.of("gpsr_canary_failed")            # 金丝雀必须停住整站
    assert adapter.saved == ["B0HJG2CJ5F"], "第一条没确认，不许继续做第二条"


async def test_an_already_submitted_first_item_cannot_satisfy_the_canary() -> None:
    """金丝雀问的是「保存这条路走通了吗」，不是「第一条有没有返回成功」。

    亚马逊把已提交的条目仍留在未解决列表里最多 7 天，所以上一轮提交过的 ASIN
    照样排在候选第一位。把它计进 submitted，等于在保存路径**一次都没跑过**的
    情况下就放行剩下的全部——爆炸半径控制当场失效。
    """

    repo, workflow = build(statuses={"B0HJG2CJ5F": STATUS_SUBMITTED})
    workflow.page_adapter.confirm_fails = {"B0GXZ1ZZLT"}
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert repo.of("gpsr_already_submitted")
    # 第二条是真正走保存路径的第一条，它没确认 —— 金丝雀必须响。
    assert repo.of("gpsr_canary_failed")


async def test_a_site_where_nothing_new_was_submitted_is_skipped_not_confirmed() -> None:
    """候选全是「审核中」→ 本次一条都没提交 → 站点 SKIPPED,并写明原因。

    记成 CONFIRMED 的话飞书卡会说「已提交安全证明」—— 亚马逊把已提交的条目留在
    未解决列表里最多 7 天,这句假话会每天出现、连着一周(2026-09-23 自审)。
    """

    rows = live_page()
    repo, workflow = build(
        rows=rows,
        statuses={"B0HJG2CJ5F": STATUS_SUBMITTED, "B0GXZ1ZZLT": STATUS_SUBMITTED},
    )

    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert workflow.page_adapter.saved == []
    assert ("DE", SiteStatus.CONFIRMED) not in repo.statuses
    assert repo.statuses[-1] == ("DE", SiteStatus.SKIPPED)
    skipped = repo.of("site_skipped")
    assert skipped and skipped[-1]["details"]["reason_code"] == "nothing_new_to_submit"


@pytest.mark.parametrize(
    ("outcome", "event_type"),
    [
        (
            ListOutcome(
                ok=False,
                reason="marketplace_not_selected",
                landed_url="https://x/account-switcher",
                switch_reason="timeout",
            ),
            "gpsr_site_unavailable",
        ),
        (ListOutcome(ok=True, rows_loaded=False), "gpsr_list_empty"),
    ],
)
async def test_an_execute_time_landing_failure_says_why_the_site_was_skipped(
    outcome, event_type
) -> None:
    """执行阶段重开合规页也可能落到切换页、选不掉站,或表格是空的。

    ⚠️ 原来这里一条事件都不写就记 SKIPPED:卡片上只剩计划阶段那句「待提交 N 个」,
    没人知道为什么没提交,选站失败的原因也丢了(09-23 自审复核)。
    """

    repo, workflow = build(outcome=outcome)

    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert workflow.page_adapter.saved == []
    # 走到执行阶段说明计划已经读到了待提交的条目:这时打不开是**没做成**,
    # 不是「这站没活儿」—— FAILED,运行落 PARTIAL(09-23 第二轮复核)。
    assert repo.statuses[-1] == ("DE", SiteStatus.FAILED)
    events = repo.of(event_type)
    assert len(events) == 1
    if event_type == "gpsr_site_unavailable":
        assert events[0]["details"]["switch_reason"] == "timeout"
        assert "timeout" in events[0]["message"]


async def test_the_panel_stuck_reason_on_the_card_carries_no_asin() -> None:
    """这句话会上飞书卡的原因栏;ASIN 只放 details。"""

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=StickyAdapter(stuck_after="B0HJG2CJ5F")
    )
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    (event,) = repo.of("gpsr_panel_stuck")
    assert "B0HJG2CJ5F" not in event["message"]
    assert event["details"]["asin"] == "B0HJG2CJ5F"


async def test_the_unresolved_count_survives_onto_the_card() -> None:
    """卡片原因栏只截到第一个「；」。没做成的条数必须在那之前,而且写进站点错误
    (汇总行读的是它)—— 原来截下来只剩「已提交并确认 1 条」(09-23 第三轮复核)。"""

    from ziniao_automation.notifications.feishu import _short_reason

    repo, workflow = build(save_fails={"B0GXZ1ZZLT"})
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    (event,) = repo.of("gpsr_site_incomplete")
    assert "1 条点击后未能确认落地" in _short_reason(event["message"])
    # 同一句也要写进站点错误:卡片的**汇总行**读的是 SiteRun.error,不是事件。
    failed = [row for row in repo.site_errors if row[1] is SiteStatus.FAILED]
    assert failed and failed[-1][2] and failed[-1][2].startswith("1 条点击后未能确认落地")


class SessionDropsAdapter(StickyAdapter):
    """收尾回列表时落到了登录页 —— 德国站会话掉得很快，这是实机上的常态。"""

    def unsafe_reason(self, page) -> str:
        return "auth_required"


async def test_a_session_lost_while_submitting_is_reported_as_a_login_problem() -> None:
    """⚠️ 一律报「侧栏未能关闭」的话，操作员拿到的原因和真实原因毫无关系。

    ``return_to_list`` 的 docstring 早就点名了这种错位：德国站会话一小时就掉，
    重新加载列表页经常直接落到登录页（2026-09-26 自审）。
    """

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=SessionDropsAdapter(stuck_after="B0HJG2CJ5F")
    )

    with pytest.raises(HumanAuthRequired):
        await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    assert repo.of("gpsr_panel_stuck") == [], "这不是侧栏的问题"


async def test_a_panel_that_really_is_stuck_still_says_so() -> None:
    """反向：真的是侧栏关不掉时，照旧报 panel_stuck、站点 FAILED。"""

    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=StickyAdapter(stuck_after="B0HJG2CJ5F")
    )
    await workflow.execute(auto_run(), object(), EMPTY_PLAN)

    (event,) = repo.of("gpsr_panel_stuck")
    assert event["details"]["reason_code"] == "panel_stuck"
