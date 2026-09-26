"""德国站 GPSR 安全证明流程的静态注册元数据。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..contracts import Workflow
from ..registry import WorkflowDefinition, WorkflowExecutionClass
from ..types import RunMode


# GPSR 是欧盟法规，我们目前只有德国站在欧盟境内。写死成 DE 是 fail-closed 的：
# 把这条流程指向 CA/UK/AU 只会打开一个结构完全不同的页面，与其到时候靠页面判据
# 兜，不如在排期表单上就选不出来。
MarketplaceCode = Literal["DE"]


class AmazonGpsrConfig(BaseModel):
    """第 1 版排期输入。运行期对象永远不进这个模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    marketplace_codes: tuple[MarketplaceCode, ...] = Field(
        default_factory=tuple,
        title="亚马逊站点",
        description="需要提交 GPSR 安全证明的站点；目前只有德国站适用。",
    )

    # 刻意**没有**「单次运行最多提交条数」。运营 2026-09-14 明确要求不设上限。
    # ⚠️ 代价记在这里：队列是单线程、无抢占、也没有运行超时，所以一次长扫描会把
    # 排在后面的提现一直拖着，而提现按滑动 24 小时限流、拖过了就是漏一笔。
    # 现在唯一的缓解手段是**把 GPSR 排期和提现排期在时间上错开**，靠人记着。

    @field_validator("marketplace_codes", mode="before")
    @classmethod
    def normalize_marketplace_codes(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError("marketplace_codes 必须是站点列表")
        try:
            codes = tuple(str(code).strip().upper() for code in value)
        except TypeError as exc:
            raise ValueError("marketplace_codes 必须是站点列表") from exc
        return tuple(dict.fromkeys(codes))


def build_amazon_gpsr_definition(
    workflow: Workflow | None = None,
) -> WorkflowDefinition:
    """构造固定的定义，可选地绑定执行器。

    ``requires_financial_lock=False``：这条流程一分钱都不碰，占全局资金锁只会
    拖垮所有店铺的提现。
    """

    return WorkflowDefinition(
        key="amazon_gpsr_attestation",
        display_name="德国站 GPSR 安全证明自动提交",
        description=(
            "读取德国站监管合规性列表，挑出「GPSR：警告和安全信息」的商品，"
            "并按所选模式列出清单或提交安全证明。"
        ),
        workflow=workflow,
        # AUTO 于 2026-09-17 放开。在此之前它**根本不在选项里**——勾那个复选框是
        # 向亚马逊做不可逆的合规声明，所以那条路不该只是「没人选」。放开的前提是
        # 两件事都已在真机上完成：空跑的候选数与人工清点一致（11 条目标 / 10 条
        # 待提交），以及首次单条提交成功并经重新加载回读确认（B0GX12HL64 的卡片
        # 状态变为「审核中」，随后被亚马逊移入「已存档」）。
        #
        # ⚠️ 运营决定不设单次提交上限，所以一次 AUTO 会把当时**所有**候选做完。
        # 剩下的爆炸半径控制只有 workflow 里那只「金丝雀」：本站点第一条没确认
        # 落地就停掉整站，避免把同一个错误连做十遍。
        #
        # APPROVAL 仍然不列：它的人工闸门是「资金审核台」，那个页面整个是围绕
        # 金额和收款账户建的，一条 GPSR 任务停在那里会渲染成 EUR 0.00 的提现。
        supported_modes=(RunMode.DRY_RUN, RunMode.AUTO),
        default_mode=RunMode.DRY_RUN,
        config_version=1,
        config_model=AmazonGpsrConfig,
        requires_marketplace_targets=True,
        requires_confirmed_identity=True,
        requires_financial_lock=False,
        execution_class=WorkflowExecutionClass.STANDARD,
        # 提现 1、反馈 2、GPSR 3：队列争用时提现永远优先，这条最能等。
        business_priority=3,
    )
