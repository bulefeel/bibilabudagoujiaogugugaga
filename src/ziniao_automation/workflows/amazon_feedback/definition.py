"""Static registration metadata for the Amazon feedback removal workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..contracts import Workflow
from ..registry import WorkflowDefinition, WorkflowExecutionClass
from ..types import RunMode


MarketplaceCode = Literal["CA", "UK", "AU"]


class AmazonFeedbackConfig(BaseModel):
    """Version 1 scheduling inputs; runtime objects never enter this model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    marketplace_codes: tuple[MarketplaceCode, ...] = Field(
        default_factory=tuple,
        title="亚马逊站点",
        description="需要清扫中差评的站点；启用排期前至少选择一个。",
    )
    max_submissions_per_run: int = Field(
        default=20,
        ge=1,
        le=200,
        title="单次运行最多提交条数",
        description=(
            "提交请求审核是不可逆的，一条反馈只能请求一次。"
            "达到上限后本次运行停止提交，其余条目留到下一次。"
        ),
    )

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


def build_amazon_feedback_definition(
    workflow: Workflow | None = None,
) -> WorkflowDefinition:
    """Build the fixed definition, optionally binding its live executor.

    ``requires_financial_lock`` is False on purpose.  A feedback sweep can run
    for a long time, and taking the global funds lock would stall every store's
    payout — which Amazon rate limits on a rolling 24-hour window.
    """

    return WorkflowDefinition(
        key="amazon_feedback_removal",
        display_name="店铺 1-3 星 Feedback 自动删除",
        description="读取中差评，判定请求原因，并按所选模式生成清单或提交请求审核。",
        workflow=workflow,
        supported_modes=(RunMode.DRY_RUN, RunMode.APPROVAL, RunMode.AUTO),
        # Submitting is irreversible and one-shot per feedback, so the default
        # is the mode that only reports what it would do.
        default_mode=RunMode.DRY_RUN,
        config_version=1,
        config_model=AmazonFeedbackConfig,
        requires_marketplace_targets=True,
        requires_confirmed_identity=True,
        requires_financial_lock=False,
        execution_class=WorkflowExecutionClass.STANDARD,
        # Payouts win any queue contention; feedback can always wait.
        business_priority=2,
    )
