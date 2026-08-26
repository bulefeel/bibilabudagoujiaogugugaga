"""Static registration metadata for the Amazon disbursement workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..contracts import Workflow
from ..registry import WorkflowDefinition, WorkflowExecutionClass
from ..types import RunMode


MarketplaceCode = Literal["CA", "UK", "AU"]


class AmazonDisbursementConfig(BaseModel):
    """Version 1 scheduling inputs; runtime objects never enter this model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    marketplace_codes: tuple[MarketplaceCode, ...] = Field(
        default_factory=tuple,
        title="亚马逊站点",
        description="需要执行提现检查的站点；启用排期前至少选择一个。",
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


def build_amazon_disbursement_definition(
    workflow: Workflow | None = None,
) -> WorkflowDefinition:
    """Build the fixed definition, optionally binding its live executor.

    Web-only application instances use the metadata form without constructing
    a database repository, Ziniao controller or browser workflow.  Production
    composition passes the live object and receives the same public contract.
    """

    return WorkflowDefinition(
        key="amazon_disbursement",
        display_name="亚马逊提现",
        description="读取可用资金，并按所选模式生成审核清单或提交提现。",
        workflow=workflow,
        supported_modes=(RunMode.DRY_RUN, RunMode.APPROVAL, RunMode.AUTO),
        default_mode=RunMode.DRY_RUN,
        config_version=1,
        config_model=AmazonDisbursementConfig,
        requires_marketplace_targets=True,
        requires_confirmed_identity=True,
        requires_financial_lock=True,
        execution_class=WorkflowExecutionClass.FINANCIAL,
        business_priority=1,
    )
