"""Code-registered definition for the German inventory GPSR workflow."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..contracts import Workflow
from ..registry import WorkflowDefinition, WorkflowExecutionClass
from ..types import RunMode


class AmazonInventoryGpsrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    marketplace_codes: tuple[Literal["DE", "IT", "FR", "ES", "PL"], ...] = Field(
        default_factory=tuple, title="亚马逊站点",
        description="通过商城菜单切站，扫描全部分页的需要批准商品。",
    )
    processing_types: tuple[Literal["safety_attestation", "responsible_person", "manufacturer"], ...] = Field(
        default=("safety_attestation",), min_length=1,
        title="处理项目", description="安全证明、负责人及制造商信息关联；旧任务不自动扩大处理范围。",
        json_schema_extra={"x-create-default": ["safety_attestation", "responsible_person", "manufacturer"]},
    )

    @field_validator("processing_types")
    @classmethod
    def unique_processing_types(cls, value):
        return tuple(dict.fromkeys(value))

    @field_validator("marketplace_codes", mode="before")
    @classmethod
    def normalize_marketplace_codes(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError("marketplace_codes 必须是站点列表")
        try:
            return tuple(dict.fromkeys(str(code).strip().upper() for code in value))
        except TypeError as exc:
            raise ValueError("marketplace_codes 必须是站点列表") from exc


def build_amazon_inventory_gpsr_definition(workflow: Workflow | None = None) -> WorkflowDefinition:
    return WorkflowDefinition(
        key="amazon_inventory_gpsr_attestation",
        display_name="库存待批准商品合规处理",
        description="意大利、法国、西班牙、波兰及德国：完整加载待批准商品，分别处理安全证明、负责人和制造商信息。空跑不勾选、不送出。",
        workflow=workflow,
        supported_modes=(RunMode.DRY_RUN, RunMode.AUTO),
        default_mode=RunMode.DRY_RUN,
        config_version=1,
        config_model=AmazonInventoryGpsrConfig,
        requires_marketplace_targets=True,
        requires_confirmed_identity=True,
        requires_financial_lock=False,
        execution_class=WorkflowExecutionClass.STANDARD,
        business_priority=4,
    )
