"""德国站 GPSR 安全证明流程的编排。

⚠️ **``execute`` 会真的动手**：打开安全证明卡、勾选、点保存。那一下不可撤销。
每一条不变量都在**同一个 JS tick 内**复核（见 ``page.save_attestation``），而且
第一条必须确认落地才做第二条——运营决定不设条数上限，那个「金丝雀」是唯一剩下的
爆炸半径控制。

``plan`` 已经是真的：它打开页面、读行、判定，并把**每一个候选 ASIN 写成一条运行
事件**。那不是装饰——运营去掉了逐条人工闸门，所以「空跑一遍再放量」是这条不可逆
流程唯一的人工关卡，而看到「候选 3 条」就批准等于什么都没批。
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any, Sequence

from ..contracts import WorkflowRepository
from ..errors import HumanAuthRequired
from ..types import (
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationRecord,
    PlanLine,
    RunMode,
    RunStatus,
    SiteStatus,
    WorkflowPlan,
    WorkflowReport,
    WorkflowRun,
)
from .config import (
    STATUS_ACTIONABLE,
    STATUS_SUBMITTED,
    GpsrDomContract,
    PolicyRow,
    Selection,
    select_actionable,
)
from .page import AmazonGpsrPage


logger = logging.getLogger(__name__)


# 逐条处理的四个出口。⚠️ 别再合并回一个布尔：``unconfirmed``（点了但没读到确认）
# 既不是成功也不是失败，把它归进任何一边都是在对不可逆动作说假话。
_ITEM_CONFIRMED = "confirmed"
_ITEM_SKIPPED = "skipped"
_ITEM_UNCONFIRMED = "unconfirmed"
_ITEM_FAILED = "failed"
# ``_dispatch_one`` 的内部出口：保存已点下、结果尚未回读。它**不会**离开
# ``_submit_one``——外面看到的只能是上面那四个。
_ITEM_SAVED = "saved"


def _log_site(marketplace_code: str, outcome: str, reason_code: str, **extra: Any) -> None:
    """只记代码和计数。

    ASIN 和商品标题不进 ``data/logs/*.jsonl``——和金额、收款尾号、卖家名同一个
    口径。逐条明细走 ``run_events``，那是只进 SQLite 的。
    """

    detail = "".join(
        f" {key}={value}" for key, value in extra.items() if value not in (None, "")
    )
    logger.info(
        "gpsr_site: marketplace=%s outcome=%s reason_code=%s%s",
        marketplace_code,
        outcome,
        reason_code,
        detail,
        extra={"marketplace": marketplace_code, "event": "gpsr_site"},
    )


class AmazonGpsrWorkflow:
    """实现 Workflow 协议。一条 ``operation_guards`` 都不写。"""

    name = "amazon_gpsr_attestation"

    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        page_adapter: AmazonGpsrPage | None = None,
    ) -> None:
        self.repository = repository
        self.page_adapter = page_adapter or AmazonGpsrPage()
        self.contract = GpsrDomContract()
        # 防重放：记住**本次任务**里已经处理过的 ASIN。引擎在人工验证完成后会
        # 从头重跑 plan+execute，而那一刻亚马逊的卡片状态未必已经翻成「审核中」，
        # 所以页面那层的幂等在这段窗口里靠不住。
        #
        # ⚠️ 作用域是 run，不是 Page。最初按 Page 弱引用键控，但 Playwright 之外
        # 的对象未必可弱引用，退化到 id() 之后 **CPython 会复用 id**——新页面会拿到
        # 上一个页面的集合，于是该提交的条目被当成已处理而静默跳过。
        # 只留当前任务这一份是安全的：队列一次只跑一个浏览器任务（queue.py），
        # 不存在两个 run 并行。
        self._consumed_run_id: str | None = None
        self._consumed_asins: set[str] = set()
        # 其中**本次任务确认落地**的那些。续跑时它们被防重放集合跳过，
        # 不能因此把站点说成「本次没有新提交」。
        self._confirmed_asins: set[str] = set()
        # 上一条处理完之后侧栏有没有卡住。卡住就没法继续——见 _submit_one。
        self._panel_stuck = False

    # ------------------------------------------------------------------
    # Workflow protocol
    # ------------------------------------------------------------------
    async def preflight(self, run: WorkflowRun, page: Any) -> None:
        if not run.marketplaces:
            raise ValueError("该任务没有可执行的站点")

    async def plan(self, run: WorkflowRun, page: Any) -> WorkflowPlan:
        lines: list[PlanLine] = []
        for marketplace in run.marketplaces:
            actionable = await self._plan_marketplace(run, page, marketplace)
            lines.append(
                PlanLine(
                    marketplace=marketplace,
                    snapshot=gpsr_snapshot(
                        marketplace, actionable, contract_version=self.contract.version
                    ),
                )
            )
        # 一个站点没活儿也要留一行，任务里才看得出它被读过；引擎只在**一行都没有**
        # 时才跳过执行。
        if not any(line.snapshot.can_submit for line in lines):
            return WorkflowPlan(self.name, run.id, run.store.id, ())
        return WorkflowPlan(self.name, run.id, run.store.id, tuple(lines))

    async def _plan_marketplace(
        self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef
    ) -> int:
        # 站点行必须最先建：``append_event`` 靠查这一行来绑定站点，建之前记的
        # 事件会成为孤儿；而且每个被读过的站点都要出现在报告里，只在出错时才建
        # 行会让「读过且没活儿」的站点整个消失。
        await self.repository.set_site_status(
            run.id, marketplace.code, SiteStatus.PREFLIGHT
        )

        outcome = await self.page_adapter.open_policies(page, marketplace.domain)
        if not outcome.ok:
            if outcome.reason == "auth_required":
                # 德国站有自己的 assoc_handle，会话掉得很快。交给引擎的人工接管
                # 路径，**不能**当成「这个站点没活儿」。
                _log_site(marketplace.code, "NEEDS_HUMAN_AUTH", "auth_required")
                raise HumanAuthRequired(
                    f"{marketplace.code} 站点需要重新登录后才能读取监管合规性页",
                    kind="signin",
                )
            await self._record_unavailable(run, marketplace, outcome)
            return 0

        if not outcome.rows_loaded:
            await self._record_list_empty(run, marketplace)
            return 0

        # ⚠️ 必须用 outcome 带回来的 Page：登录助手可能把登录收在另一个标签里，
        # 而亚马逊会故意把原标签留在 /ap/signin。在原标签上抓，一行都匹配不到，
        # 结果就成了「这家店没有待办」。
        rows = await self.page_adapter.read_rows(outcome.page or page)
        selection = select_actionable(rows)
        await self._record_selection(run, marketplace, selection)
        # 读到了待提交的条目就是「待提交」,不是「已跳过」—— 等登录、空跑、重启时
        # 卡片都会显示这一格(09-23 第三轮复核)。没有候选才是跳过。
        await self.repository.set_site_status(
            run.id,
            marketplace.code,
            SiteStatus.PLANNED if selection.candidates else SiteStatus.SKIPPED,
        )
        return len(selection.candidates)

    async def _record_selection(
        self, run: WorkflowRun, marketplace: MarketplaceRef, selection: Selection
    ) -> None:
        """把这次扫描的结果写成运行事件。

        逐条事件是**空跑预览**本身：运营去掉了逐条人工闸门，所以「先空跑再放量」
        是唯一的人工关卡；只给一个总数等于让人盲批一次不可逆的合规声明。
        """

        for item in selection.candidates:
            await self.repository.append_event(
                run.id,
                "gpsr_candidate",
                f"{item.asin} 待提交安全证明 · {item.title[:60]}",
                marketplace_code=marketplace.code,
                details={
                    "reason_code": "attestation_pending",
                    "asin": item.asin,
                    "action_taken": item.action_taken,
                    "dom_copies": item.dom_copies,
                },
            )

        for asin in sorted(selection.removed_asins):
            # 只报一个数字的话，操作员没法核对我们跳过的是不是他在页面上看到的
            # 那一条。空跑预览是这条不可逆流程唯一的人工闸门，必须逐条可核。
            await self.repository.append_event(
                run.id,
                "gpsr_already_removed",
                f"{asin} 亚马逊已下架该商品，按运营要求不提交安全证明",
                marketplace_code=marketplace.code,
                details={"reason_code": "already_removed", "asin": asin},
            )

        if selection.unknown_reasons:
            # 运营 2026-09-16 决定不因未知原因阻断，所以这条事件是这个信号**唯一**
            # 的去处。它同时意味着两件事：亚马逊新增了合规品类（无害），或者亚马逊
            # 改了目标那句文案（候选会静默变 0，每家店永远报绿）。
            await self.repository.append_event(
                run.id,
                "gpsr_unknown_reason",
                "读到白名单之外的原因文案，请核对是否亚马逊改了措辞："
                + "、".join(sorted(selection.unknown_reasons)[:3]),
                marketplace_code=marketplace.code,
                details={
                    "reason_code": "unknown_reason",
                    "unknown_reasons": sorted(selection.unknown_reasons)[:10],
                },
            )
            _log_site(
                marketplace.code,
                "UNKNOWN_REASON",
                "unknown_reason",
                count=len(selection.unknown_reasons),
            )

        # ⚠️ 未知原因必须并进这一句。飞书卡上每个站点只显示**最后一条**站点事件,
        # 而这一条总是在 gpsr_unknown_reason 之后写 —— 分开写的话,运营取消阻断之后
        # 那个唯一的文案漂移信号永远上不了卡片(自审抓到)。
        unknown_note = (
            f"；另有 {len(selection.unknown_reasons)} 种白名单之外的原因文案，"
            "请核对亚马逊是否改了措辞"
            if selection.unknown_reasons
            else ""
        )
        await self.repository.append_event(
            run.id,
            "gpsr_scanned",
            (
                f"读取 {selection.rows_read} 行，"
                f"目标条目 {selection.target_asins} 个，"
                f"待提交 {len(selection.candidates)} 个，"
                f"已下架跳过 {selection.already_removed} 个，"
                f"结构不符 {selection.drift_rows} 行"
                f"{unknown_note}"
            ),
            marketplace_code=marketplace.code,
            details={
                "reason_code": "scanned",
                "rows_read": selection.rows_read,
                "target_asins": selection.target_asins,
                "candidates": len(selection.candidates),
                "already_removed": selection.already_removed,
                "drift_rows": selection.drift_rows,
            },
        )
        _log_site(
            marketplace.code,
            "SCANNED",
            "scanned",
            rows=selection.rows_read,
            candidates=len(selection.candidates),
            removed=selection.already_removed,
            drift=selection.drift_rows,
        )

    async def execute(
        self, run: WorkflowRun, page: Any, plan: WorkflowPlan
    ) -> Sequence[OperationRecord]:
        """⚠️ 这里是整条流程唯一真的动手的地方。

        **永远返回空序列**：它一分钱都不碰，写一条 ``operation_guards`` 就会把任务
        拖进 UNCERTAIN_FINANCIAL，然后在资金审核台上渲染成一笔提现。
        """

        if run.mode is not RunMode.AUTO:
            # DRY_RUN 根本走不到这里（引擎会短路），这道判断是为了「万一」——
            # 万一将来有人给这条流程加了别的模式。
            return ()

        for marketplace in run.marketplaces:
            try:
                await self._submit_marketplace(run, page, marketplace)
            except HumanAuthRequired:
                # ⚠️ 必须排在 except Exception 之前。HumanAuthRequired 是
                # RuntimeError 的子类，被下面那条吞掉的话，「需要人工验证」会被
                # 记成「站点失败」，引擎的人工接管路径就再也不会触发。
                raise
            except Exception as exc:  # noqa: BLE001 —— 站点级隔离
                logger.exception(
                    "gpsr site failed run=%s marketplace=%s", run.id, marketplace.code
                )
                await self.repository.append_event(
                    run.id,
                    "gpsr_site_failed",
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    marketplace_code=marketplace.code,
                    details={"reason_code": "site_execution_failed"},
                )
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.FAILED
                )
        return ()

    def _unsafe_reason(self, page: Any) -> str:
        reader = getattr(self.page_adapter, "unsafe_reason", None)
        return reader(page) if callable(reader) else "panel_stuck"

    async def _record_unavailable(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        outcome: Any,
        *,
        during_execute: bool = False,
    ) -> None:
        """合规页打不开(不是登录问题):写明看见了什么。

        计划阶段:站点记 SKIPPED(可能真没开这个站点)。执行阶段:计划刚读到过
        待提交的条目,这时打不开就是没做成 —— 站点记 FAILED,话也换成对应的。
        """

        # ⚠️ 这两句只陈述**看见了什么**，绝不断言**为什么**。
        #
        # 原来写死的是「该店铺未开通此站点」——2026-09-18 正香-张刚CA 实测推翻：
        # 切换页上「德國」在列且不是「待註冊」，账号是有这个站点的。
        # 但反过来写死「账号有此站点、去选一次」同样是假话：迁移 0010 给
        # **每一家**店铺无条件补了 enabled=1 的 DE 行，所以真没开德国站的店是
        # 多数，它们会被指去选一个不存在的站点。
        #
        # 自动选站（``amazon_account_switcher``）读过那一页，但它只按整串精确
        # 文案匹配：``not_offered`` 的意思是「列表里没有我们认得的那个写法」，
        # 既可能是真没开，也可能是一种没记录过的文案。所以仍然只说看见了什么。
        details: dict[str, Any] = {
            "reason_code": outcome.reason or "marketplace_unavailable"
        }
        if outcome.reason == "marketplace_not_selected":
            if outcome.switch_reason == "not_offered":
                seen = "选择页上没有与此站点完全一致的国家（带「待註冊」等后缀的不算）"
            elif outcome.switch_reason == "list_not_loaded":
                seen = "选择页上的站点列表一直没有加载出来"
            else:
                seen = f"自动选站没有完成（{outcome.switch_reason or '原因未知'}）"
            if during_execute:
                message = (
                    f"执行阶段重新打开合规页时停在账户/站点选择页，{seen}；"
                    "计划阶段读到的待提交条目本次没有提交，请在紫鸟窗口选定该站点后重试"
                )
            else:
                message = (
                    f"页面停在账户/站点选择页，{seen}，本站点跳过；"
                    "请在紫鸟窗口确认该账号是否开通了此站点："
                    "若已开通，在该页选定一次即可；若没有，请在店铺设置里关闭此站点"
                )
            details["switch_reason"] = outcome.switch_reason
        elif during_execute:
            message = (
                "执行阶段重新打开时页面未落在监管合规性页；"
                "计划阶段读到的待提交条目本次没有提交"
            )
        else:
            message = "页面未落在监管合规性页，也不是账户/站点选择页，本站点跳过"
        await self.repository.append_event(
            run.id,
            "gpsr_site_unavailable",
            message,
            marketplace_code=marketplace.code,
            details=details,
        )
        status = SiteStatus.FAILED if during_execute else SiteStatus.SKIPPED
        await self.repository.set_site_status(run.id, marketplace.code, status)
        _log_site(
            marketplace.code,
            status.value,
            outcome.reason or "unavailable",
            switch_reason=outcome.switch_reason,
        )

    async def _record_list_empty(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        *,
        during_execute: bool = False,
    ) -> None:
        await self.repository.append_event(
            run.id,
            "gpsr_list_empty",
            (
                "执行阶段重新打开时监管合规性列表是空的；计划阶段读到的待提交条目本次没有提交"
                if during_execute
                else "该站点的监管合规性列表没有任何记录"
            ),
            marketplace_code=marketplace.code,
            details={"reason_code": "no_rows"},
        )
        await self.repository.set_site_status(
            run.id,
            marketplace.code,
            SiteStatus.FAILED if during_execute else SiteStatus.SKIPPED,
        )
        _log_site(marketplace.code, "EMPTY", "no_rows")

    async def _submit_marketplace(
        self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef
    ) -> None:
        outcome = await self.page_adapter.open_policies(page, marketplace.domain)
        if not outcome.ok:
            if outcome.reason == "auth_required":
                raise HumanAuthRequired(
                    f"{marketplace.code} 站点需要重新登录后才能提交安全证明",
                    kind="signin",
                )
            # 执行阶段重开页面时同样可能落到切换页、或选不掉站。原来这里一条事件都
            # 不写就记 SKIPPED:卡片上只剩计划写的「待提交 N 个」,没人知道为什么
            # 没提交,选站失败的原因也丢了(09-23 自审复核)。
            # ⚠️ 走到执行阶段说明计划已经读到了待提交的条目:这时打不开页面是
            # **没做成**,不是「这站没活儿」—— 记 FAILED(09-23 第二轮复核)。
            await self._record_unavailable(run, marketplace, outcome, during_execute=True)
            return
        page = outcome.page or page
        if not outcome.rows_loaded:
            await self._record_list_empty(run, marketplace, during_execute=True)
            return

        selection = select_actionable(await self.page_adapter.read_rows(page))
        if not selection.candidates:
            # ⚠️ 计划阶段说有活儿（引擎只在有 can_submit 的行时才走到这里），执行
            # 阶段却一条候选都读不到。可能是上一轮刚提交完、卡片已翻「审核中」，
            # 也可能是这次行加载判据只等到了别的类别的行就放行了。
            #
            # 无论哪种，**一条事件都不写就把站点记成 CONFIRMED**，等于凭空宣布
            # 「确认完成」：运行详情里只剩计划写的「待提交 N 个」，站点却是绿的。
            await self.repository.append_event(
                run.id,
                "gpsr_nothing_to_submit",
                "执行阶段重新读页时没有任何候选，未提交任何安全证明",
                marketplace_code=marketplace.code,
                details={
                    "reason_code": "no_candidates_at_execute",
                    "target_asins": selection.target_asins,
                    "drift_rows": selection.drift_rows,
                },
            )
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SKIPPED
            )
            _log_site(
                marketplace.code,
                "NOTHING_TO_SUBMIT",
                "no_candidates_at_execute",
                targets=selection.target_asins,
                drift=selection.drift_rows,
            )
            return

        consumed = self._consumed_for(run)
        submitted = 0
        # 金丝雀之后点了、却没确认落地(或处理失败)的条数。不单独数的话,站点照样记
        # CONFIRMED、卡片写「安全证明已完成 / 无需操作」—— 一下结果不明的不可逆点击
        # 被报成成功(09-23 第二轮复核)。
        unresolved = 0

        for item in selection.candidates:
            # ⚠️ 防重放。引擎的 ``_with_auth_wait`` 在人工完成验证后会**从头重跑**
            # preflight + plan + execute，所以同一个 ASIN 可能第二次走到这里，而
            # 此时亚马逊的卡片状态未必已经翻成「审核中」。这个集合是那段窗口里
            # 唯一挡住重复提交的东西。
            if item.asin in consumed:
                continue

            self._panel_stuck = False
            outcome = await self._submit_one(run, page, marketplace, item)
            consumed.add(item.asin)
            if self._panel_stuck:
                # 收不回安全态的原因不止一种。德国站会话掉得快,重新加载列表页
                # 经常直接落到登录页 —— 那时报「侧栏未能关闭」和真实原因毫无关系
                # (2026-09-26 自审)。
                reason = self._unsafe_reason(page)
                if reason == "auth_required":
                    _log_site(marketplace.code, "NEEDS_HUMAN_AUTH", "auth_required")
                    raise HumanAuthRequired(
                        f"{marketplace.code} 站点在提交过程中被要求重新登录",
                        kind="signin",
                    )
                message = {
                    "marketplace_not_selected": "提交过程中页面回到了账户/站点选择页，已停止该站点后续提交",
                    "left_the_page": "提交过程中页面离开了监管合规性页，已停止该站点后续提交",
                }.get(reason, "有一条处理后侧栏未能关闭，已停止该站点后续提交（ASIN 见运行详情）")
                await self.repository.append_event(
                    run.id,
                    "gpsr_panel_stuck",
                    # ASIN 只放 details:这句话会上飞书卡的原因栏。
                    message,
                    marketplace_code=marketplace.code,
                    details={"reason_code": reason, "asin": item.asin},
                )
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.FAILED
                )
                _log_site(marketplace.code, "UNSAFE_STATE", reason)
                return
            if outcome == _ITEM_SKIPPED:
                # 本来就已是「审核中」，这一条没走过保存路径。
                # ⚠️ **不能**计进 submitted：金丝雀问的是「保存这条路走通了吗」，
                # 而亚马逊把已提交的条目仍留在未解决列表里最多 7 天，所以上一轮
                # 提交过的 ASIN 照样会排在候选第一位。拿它满足金丝雀，等于在保存
                # 路径一次都没跑过的情况下就放行剩下的全部。
                continue
            if outcome != _ITEM_CONFIRMED:
                # ⚠️ 爆炸半径控制。运营决定不设条数上限，所以「第一条必须确认
                # 落地，才做第二条」是**唯一**剩下的闸：第一条没确认就停下整站，
                # 避免把同一个错误连做十遍。
                # ``unconfirmed``（点了但没读到确认）在这里和失败同等对待 ——
                # 对不可逆动作，拿不准就该停。
                if submitted == 0:
                    await self.repository.append_event(
                        run.id,
                        "gpsr_canary_failed",
                        "本站点第一条安全证明未能确认落地，已停止该站点后续提交",
                        marketplace_code=marketplace.code,
                        details={"reason_code": "canary_failed", "asin": item.asin},
                    )
                    # SiteStatus 里没有 PARTIAL。这一站确实一条都没提交成，
                    # FAILED 是诚实且响亮的。
                    await self.repository.set_site_status(
                        run.id, marketplace.code, SiteStatus.FAILED
                    )
                    return
                unresolved += 1
                continue
            submitted += 1
            self._confirmed_asins.add(item.asin)

        submitted_earlier_in_run = any(
            item.asin in self._confirmed_asins for item in selection.candidates
        )
        if submitted == 0 and not submitted_earlier_in_run:
            # 一条都没提交(候选全是「审核中」):记成 CONFIRMED 会让飞书卡写
            # 「已提交安全证明」—— 亚马逊把已提交的条目留在未解决列表里最多 7 天,
            # 于是这句假话会每天出现、连着一周(自审抓到)。
            # ⚠️ 人工验证续跑时,本次任务前半段已经确认落地的条目会被防重放集合
            # 跳过 —— 那是「本次提交过」,不是「没有新提交」,照常记 CONFIRMED。
            await self.repository.append_event(
                run.id,
                "site_skipped",
                "本次没有新提交：候选条目在亚马逊上都已处于审核中",
                marketplace_code=marketplace.code,
                details={"reason_code": "nothing_new_to_submit"},
            )
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SKIPPED
            )
            _log_site(marketplace.code, "SKIPPED", "nothing_new_to_submit")
            return
        if unresolved:
            # 没做成的放最前、不用「；」:卡片原因栏只截到第一个「；」,原来截下来
            # 只剩「已提交并确认 1 条」,那一下结果不明的点击在卡片上消失了
            # (09-23 第三轮复核)。
            incomplete = (
                f"{unresolved} 条点击后未能确认落地或处理失败（另有 {submitted} 条已确认），"
                "请到运行详情逐条核对"
            )
            await self.repository.append_event(
                run.id,
                "gpsr_site_incomplete",
                incomplete,
                marketplace_code=marketplace.code,
                details={
                    "reason_code": "items_unresolved",
                    "confirmed": submitted,
                    "unresolved": unresolved,
                },
            )
            # SiteStatus 没有「部分」:FAILED 让运行落 PARTIAL(红卡「部分失败」)。
            # 同一句也写进站点错误,卡片的汇总行才读得到它。
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.FAILED, error=incomplete
            )
            _log_site(
                marketplace.code,
                "INCOMPLETE",
                "items_unresolved",
                confirmed=submitted,
                unresolved=unresolved,
            )
            return
        # 站点级的「这次做了什么」。没有它的话,卡片的原因栏只剩计划阶段那句
        # 「待提交 N 个」 —— 一次提交了 2 条的成功运行,卡上看不出提交过几条,
        # 和什么都没做的运行长得一样(2026-09-26 自审)。
        await self.repository.append_event(
            run.id,
            "gpsr_site_completed",
            f"本次确认提交 {submitted} 个安全证明",
            marketplace_code=marketplace.code,
            details={"reason_code": "submitted", "confirmed": submitted},
        )
        await self.repository.set_site_status(
            run.id, marketplace.code, SiteStatus.CONFIRMED
        )

    async def _submit_one(
        self,
        run: WorkflowRun,
        page: Any,
        marketplace: MarketplaceRef,
        item: PolicyRow,
    ) -> str:
        """先「发起」再「确认到达」，两件事分开判。

        ⚠️ 原来这里只有一个 bool，而它的含义是「save.click() 已经调用过且没抛异常」
        —— 那是**发出了动作**，不是**目标态达成**。亚马逊在保存时返回校验错误或
        5xx（侧栏弹提示、卡片仍停在「未开始」）时，click() 照样不抛异常，于是这一条
        被记成「已提交安全证明」，金丝雀判通过，剩下的全部沿同一条坏路径做完，
        站点 CONFIRMED、飞书绿卡。而这条流程唯一不可逆的动作就在这里。

        回读用的是**现成且验证过的判据**：`open_attestation_panel` 的状态，
        以及 `already_submitted` 那条跳过路径一直在用的 ``审核中``。

        四个出口，不许再合并回一个布尔：
        ``confirmed`` 保存并回读确认 / ``skipped`` 本来就已是审核中 /
        ``unconfirmed`` 点了但没读到确认 / ``failed`` 根本没点。
        """

        outcome = await self._dispatch_one(run, page, marketplace, item)
        if outcome != _ITEM_SAVED:
            return outcome
        if self._panel_stuck:
            # 页面没回到安全态，这时候的回读读不准；调用方会据此停掉整站。
            await self._record_item(
                run, marketplace, item, "gpsr_save_unconfirmed", "panel_stuck"
            )
            return _ITEM_UNCONFIRMED
        return await self._confirm_saved(run, page, marketplace, item)

    async def _confirm_saved(
        self,
        run: WorkflowRun,
        page: Any,
        marketplace: MarketplaceRef,
        item: PolicyRow,
    ) -> str:
        """回读这一条的卡片状态。判据是**结果态**，不是「我点了保存」。

        读不到确认时给的是 ``unconfirmed`` 而不是 ``failed``：请求可能真的发出去了，
        把它说成「没提交」和说成「已提交」一样是假话。金丝雀把它当作未通过 ——
        对不可逆动作，拿不准就该停下。
        """

        try:
            panel = await self.page_adapter.open_attestation_panel(page, item.asin)
        except Exception as exc:  # noqa: BLE001 —— 回读失败不改写已经发生的提交
            await self._record_item(
                run, marketplace, item, "gpsr_save_unconfirmed", type(exc).__name__
            )
            return _ITEM_UNCONFIRMED
        finally:
            self._panel_stuck = not await self.page_adapter.return_to_list(
                page, marketplace.domain
            )

        if panel.ok and panel.status == STATUS_SUBMITTED:
            await self._record_item(
                run, marketplace, item, "gpsr_submitted", "submitted"
            )
            return _ITEM_CONFIRMED
        await self._record_item(
            run,
            marketplace,
            item,
            "gpsr_save_unconfirmed",
            panel.status or panel.reason or "status_empty",
        )
        return _ITEM_UNCONFIRMED

    async def _dispatch_one(
        self,
        run: WorkflowRun,
        page: Any,
        marketplace: MarketplaceRef,
        item: PolicyRow,
    ) -> str:
        """处理一条。返回「这一条确认落地了吗」。

        中途任何失败都要把页面收回安全态（关掉侧栏），否则下一条会在一个半开的
        面板上开始——那正是「点开 A 的面板、提交到 B 头上」的温床。
        """

        try:
            panel = await self.page_adapter.open_attestation_panel(page, item.asin)
            if not panel.ok:
                await self._record_item(
                    run, marketplace, item, "gpsr_panel_refused", panel.reason
                )
                return _ITEM_FAILED

            # 幂等的载体：状态写在亚马逊页面上，所以这条流程不需要自己的台账表。
            if panel.status == STATUS_SUBMITTED:
                await self._record_item(
                    run, marketplace, item, "gpsr_already_submitted", "already_submitted"
                )
                return _ITEM_SKIPPED
            if panel.status != STATUS_ACTIONABLE:
                # 既不是「未开始」也不是「审核中」——不认识的状态不许猜，
                # 更不能当成已完成。
                await self._record_item(
                    run, marketplace, item, "gpsr_status_unknown", panel.status or "empty"
                )
                return _ITEM_FAILED

            if not await self.page_adapter.tick_attestation(page):
                await self._record_item(
                    run, marketplace, item, "gpsr_tick_failed", "checkbox_not_confirmed"
                )
                return _ITEM_FAILED

            # 运营 2026-09-16 要求留证：点下去之前拍一张现场。
            evidence = await self.page_adapter.capture_evidence(
                page, run, marketplace.code, label=f"attest-{item.asin}"
            )
            if evidence is not None:
                await self.repository.save_evidence(
                    run.id, marketplace.code, evidence
                )

            result = await self.page_adapter.save_attestation(page, item.asin)
            if not result.ok:
                await self._record_item(
                    run, marketplace, item, "gpsr_save_refused", result.reason
                )
                return _ITEM_FAILED
            # ⚠️ 这里**不记**「已提交」——那要等回读确认。记在这里就又变成了
            # 「我点了 = 成功」。
            return _ITEM_SAVED
        finally:
            # ⚠️ 关不掉侧栏 = 页面没回到安全态。后面每一条都会去点被它盖住的行按钮
            # 然后超时——2026-09-17 实机测试正是这样：第一条提交成功、面板留着，
            # 整站被判失败。所以这件事必须被记下来，由调用方停掉整站。
            self._panel_stuck = not await self.page_adapter.return_to_list(
                page, marketplace.domain
            )

    async def _record_item(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        item: PolicyRow,
        event_type: str,
        reason_code: str,
    ) -> None:
        messages = {
            "gpsr_submitted": "已提交安全证明",
            "gpsr_already_submitted": "亚马逊显示此前已提交，本次跳过",
            "gpsr_panel_refused": "未能确认面板属于该商品，未提交",
            "gpsr_status_unknown": "安全证明卡状态无法识别，未提交",
            "gpsr_tick_failed": "勾选未生效，未提交",
            "gpsr_save_refused": "点击保存前的复核未通过，未提交",
            "gpsr_save_unconfirmed": "已点击保存，但回读未确认落地",
        }
        await self.repository.append_event(
            run.id,
            event_type,
            f"{item.asin} {messages.get(event_type, event_type)}",
            marketplace_code=marketplace.code,
            details={"reason_code": reason_code, "asin": item.asin},
        )
        _log_site(marketplace.code, event_type.replace("gpsr_", "").upper(), reason_code)

    def _consumed_for(self, run: WorkflowRun) -> set[str]:
        if self._consumed_run_id != run.id:
            self._consumed_run_id = run.id
            self._consumed_asins = set()
            self._confirmed_asins = set()
        return self._consumed_asins

    async def reconcile(
        self, run: WorkflowRun, page: Any, pending: Sequence[Any]
    ) -> Sequence[OperationRecord]:
        return ()

    async def report(self, run: WorkflowRun, status: RunStatus) -> WorkflowReport:
        return WorkflowReport(
            run_id=run.id,
            status=status,
            title=f"{run.store.name} · GPSR 安全证明",
            summary="空跑：本次只读取与判定，未提交任何安全证明。逐条明细见运行详情。",
            fields={
                "店铺": run.store.name,
                "站点": "、".join(item.code for item in run.marketplaces),
            },
        )


def gpsr_snapshot(
    marketplace: MarketplaceRef, actionable: int, *, contract_version: str
) -> MarketplaceSnapshot:
    """用引擎共用的计划类型表达一条非资金的站点结果。

    金额字段是 ``MarketplaceSnapshot`` 结构上要求的，这里全是 0；GPSR 真正关心的
    数字走运行事件。
    """

    return MarketplaceSnapshot(
        marketplace_code=marketplace.code,
        domain=marketplace.domain,
        seller_id="",
        payment_account="",
        currency=marketplace.currency,
        payable_amount=Decimal("0"),
        delayed_amount=Decimal("0"),
        settlement_key=f"gpsr:{marketplace.code}",
        can_submit=actionable > 0,
        contract_version=contract_version,
        page_fingerprint=f"gpsr-policies:{marketplace.code}",
        skip_reason=None if actionable else "no_actionable_gpsr",
    )


__all__ = ["AmazonGpsrWorkflow", "gpsr_snapshot"]
