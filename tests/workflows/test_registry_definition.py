from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from ziniao_automation.workflows.amazon_disbursement.definition import (
    AmazonDisbursementConfig,
    build_amazon_disbursement_definition,
)
from ziniao_automation.workflows.errors import WorkflowNotRegistered
from ziniao_automation.workflows.registry import (
    WorkflowDefinition,
    WorkflowExecutionClass,
    WorkflowRegistry,
)
from ziniao_automation.workflows.types import RunMode


class FakeWorkflow:
    def __init__(self, name: str = "fake_workflow") -> None:
        self.name = name


class FakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = "default"


def _definition(workflow: Any | None = None) -> WorkflowDefinition:
    workflow = workflow or FakeWorkflow()
    return WorkflowDefinition(
        key=workflow.name,
        display_name="测试流程",
        description="只用于注册表单元测试。",
        workflow=workflow,
        supported_modes=(RunMode.DRY_RUN,),
        default_mode=RunMode.DRY_RUN,
        config_version=2,
        config_model=FakeConfig,
        requires_marketplace_targets=False,
        requires_confirmed_identity=False,
        requires_financial_lock=False,
        execution_class=WorkflowExecutionClass.READ_ONLY,
        business_priority=30,
    )


def test_registry_keeps_legacy_get_register_and_names_contract() -> None:
    first = FakeWorkflow("zeta")
    second = FakeWorkflow("alpha")
    registry = WorkflowRegistry((first,))

    registry.register(second)

    assert registry.get("zeta") is first
    assert registry.get("alpha") is second
    assert registry.names() == ("alpha", "zeta")
    assert registry.definition("zeta").workflow is first

    with pytest.raises(ValueError, match="已注册"):
        registry.register(first)
    with pytest.raises(WorkflowNotRegistered):
        registry.get("missing")


def test_public_metadata_is_a_fixed_safe_contract() -> None:
    workflow = FakeWorkflow()
    registry = WorkflowRegistry((_definition(workflow),))

    (metadata,) = registry.public_metadata()

    assert set(metadata) == {
        "key",
        "display_name",
        "description",
        "supported_modes",
        "default_mode",
        "config_version",
        "requires_marketplace_targets",
        "requires_confirmed_identity",
        "requires_financial_lock",
        "execution_class",
        "business_priority",
        "config_schema",
    }
    assert metadata["key"] == "fake_workflow"
    assert metadata["supported_modes"] == ["dry_run"]
    assert metadata["execution_class"] == "read_only"
    assert metadata["business_priority"] == 30
    assert "workflow" not in metadata
    assert "config_model" not in metadata
    assert "import" not in repr(metadata).lower()

    # Each call is detached from the previous result, so an HTTP caller cannot
    # mutate the registry's source of truth.
    metadata["config_schema"].clear()
    assert registry.public_metadata()[0]["config_schema"]["type"] == "object"


def test_amazon_definition_normalizes_and_deduplicates_marketplaces() -> None:
    workflow = FakeWorkflow("amazon_disbursement")
    definition = build_amazon_disbursement_definition(workflow)

    assert definition.normalize_config(
        {"marketplace_codes": [" ca ", "UK", "CA", "au"]}
    ) == {"marketplace_codes": ["CA", "UK", "AU"]}
    assert definition.requires_marketplace_targets is True
    assert definition.requires_confirmed_identity is True
    assert definition.requires_financial_lock is True
    assert definition.execution_class is WorkflowExecutionClass.FINANCIAL
    assert definition.business_priority == 1

    schema = definition.to_public_dict()["config_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["marketplace_codes"]["items"]["enum"] == [
        "CA",
        "UK",
        "AU",
    ]


def test_metadata_only_definition_does_not_require_runtime_dependencies() -> None:
    definition = build_amazon_disbursement_definition()
    registry = WorkflowRegistry((definition,))

    assert registry.names() == ("amazon_disbursement",)
    assert registry.public_metadata()[0]["display_name"] == "亚马逊提现"
    with pytest.raises(WorkflowNotRegistered, match="未绑定执行器"):
        registry.get("amazon_disbursement")


@pytest.mark.parametrize(
    "config",
    [
        {"marketplace_codes": ["US"]},
        {"marketplace_codes": "CA"},
        {"marketplace_codes": ["CA"], "script": "payload"},
    ],
)
def test_amazon_config_rejects_unknown_targets_and_fields(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AmazonDisbursementConfig.model_validate(config)


def test_definition_rejects_mismatched_runtime_and_open_config_model() -> None:
    with pytest.raises(ValueError, match="不一致"):
        WorkflowDefinition(
            key="declared",
            display_name="错误定义",
            description="",
            workflow=FakeWorkflow("actual"),
            supported_modes=(RunMode.DRY_RUN,),
            default_mode=RunMode.DRY_RUN,
            config_version=1,
            config_model=FakeConfig,
        )

    class OpenConfig(BaseModel):
        pass

    with pytest.raises(ValueError, match="extra='forbid'"):
        WorkflowDefinition(
            key="actual",
            display_name="错误定义",
            description="",
            workflow=FakeWorkflow("actual"),
            supported_modes=(RunMode.DRY_RUN,),
            default_mode=RunMode.DRY_RUN,
            config_version=1,
            config_model=OpenConfig,
        )
