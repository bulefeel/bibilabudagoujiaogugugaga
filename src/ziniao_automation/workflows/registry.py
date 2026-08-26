"""Code-only workflow allow-list and safe workflow metadata.

The registry stores executable workflow objects, but it only exposes a small,
explicit metadata DTO to the web layer.  A browser client can therefore render
registered workflows without ever receiving Python class names, import paths,
credentials or other runtime objects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from .contracts import Workflow
from .errors import WorkflowNotRegistered
from .types import RunMode


_WORKFLOW_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_PROPERTY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# Deliberately omit $id, external references, examples and arbitrary extension
# keys.  The frontend only needs this small JSON Schema subset to build fixed
# form controls.  Nested model definitions are retained, but a $ref is accepted
# only when it points to those local definitions.
_SAFE_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)


class WorkflowExecutionClass(StrEnum):
    """How the shared runtime must isolate a workflow."""

    READ_ONLY = "read_only"
    STANDARD = "standard"
    FINANCIAL = "financial"


class EmptyWorkflowConfig(BaseModel):
    """Fail-closed configuration used by legacy workflow registrations."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Executable workflow plus code-owned scheduling and form metadata."""

    key: str
    display_name: str
    description: str
    workflow: Workflow | None
    supported_modes: tuple[RunMode, ...]
    default_mode: RunMode
    config_version: int
    config_model: type[BaseModel]
    requires_marketplace_targets: bool = False
    requires_confirmed_identity: bool = False
    requires_financial_lock: bool = False
    execution_class: WorkflowExecutionClass = WorkflowExecutionClass.STANDARD
    business_priority: int = 100

    def __post_init__(self) -> None:
        key = str(self.key).strip()
        if not _WORKFLOW_KEY.fullmatch(key):
            raise ValueError(
                "workflow key 只能包含小写字母、数字和下划线，并且必须以字母开头"
            )
        if key != self.key:
            raise ValueError("workflow key 前后不能包含空格")

        if self.workflow is not None:
            runtime_name = str(getattr(self.workflow, "name", "")).strip()
            if runtime_name != key:
                raise ValueError(
                    f"工作流定义 key 与运行对象 name 不一致：{key!r} != {runtime_name!r}"
                )

        display_name = str(self.display_name).strip()
        if not display_name:
            raise ValueError("工作流显示名称不能为空")
        if len(display_name) > 80:
            raise ValueError("工作流显示名称不能超过 80 个字符")
        if display_name != self.display_name:
            object.__setattr__(self, "display_name", display_name)

        description = str(self.description).strip()
        if len(description) > 500:
            raise ValueError("工作流说明不能超过 500 个字符")
        if description != self.description:
            object.__setattr__(self, "description", description)

        try:
            modes = tuple(RunMode(mode) for mode in self.supported_modes)
        except (TypeError, ValueError) as exc:
            raise ValueError("工作流包含不支持的运行模式") from exc
        if not modes:
            raise ValueError("工作流至少需要支持一种运行模式")
        if len(modes) != len(set(modes)):
            raise ValueError("工作流运行模式不能重复")
        object.__setattr__(self, "supported_modes", modes)

        try:
            default_mode = RunMode(self.default_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("工作流默认运行模式无效") from exc
        if default_mode not in modes:
            raise ValueError("工作流默认运行模式必须位于 supported_modes 中")
        object.__setattr__(self, "default_mode", default_mode)

        if (
            isinstance(self.config_version, bool)
            or not isinstance(self.config_version, int)
            or self.config_version < 1
        ):
            raise ValueError("工作流配置版本必须是大于 0 的整数")
        if not isinstance(self.config_model, type) or not issubclass(
            self.config_model, BaseModel
        ):
            raise TypeError("config_model 必须是 Pydantic BaseModel 类型")
        if self.config_model.model_config.get("extra") != "forbid":
            raise ValueError("config_model 必须设置 extra='forbid'")

        try:
            execution_class = WorkflowExecutionClass(self.execution_class)
        except (TypeError, ValueError) as exc:
            raise ValueError("工作流执行类型无效") from exc
        object.__setattr__(self, "execution_class", execution_class)
        if bool(self.requires_financial_lock) != (
            execution_class is WorkflowExecutionClass.FINANCIAL
        ):
            raise ValueError(
                "requires_financial_lock 必须与 financial 执行类型保持一致"
            )

        if isinstance(self.business_priority, bool) or not isinstance(
            self.business_priority, int
        ):
            raise ValueError("工作流业务优先级必须是非负整数")
        if self.business_priority < 0:
            raise ValueError("工作流业务优先级必须是非负整数")

    @classmethod
    def legacy(cls, workflow: Workflow) -> WorkflowDefinition:
        """Wrap an old ``register(workflow)`` call without changing behavior.

        Legacy registration exists for engine tests and third-party code that
        predates metadata.  Production composition registers an explicit
        definition, so no scheduling decisions depend on these neutral values.
        """

        key = str(getattr(workflow, "name", "")).strip()
        return cls(
            key=key,
            display_name=key,
            description="",
            workflow=workflow,
            supported_modes=tuple(RunMode),
            default_mode=RunMode.DRY_RUN,
            config_version=1,
            config_model=EmptyWorkflowConfig,
        )

    def validate_config(self, value: Mapping[str, Any] | None = None) -> BaseModel:
        """Validate a workflow configuration and reject unknown fields."""

        return self.config_model.model_validate({} if value is None else value)

    def normalize_config(self, value: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a JSON-safe, canonical configuration snapshot."""

        return self.validate_config(value).model_dump(mode="json")

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize only the fixed, non-sensitive frontend contract."""

        return {
            "key": self.key,
            "display_name": self.display_name,
            "description": self.description,
            "supported_modes": [mode.value for mode in self.supported_modes],
            "default_mode": self.default_mode.value,
            "config_version": self.config_version,
            "requires_marketplace_targets": self.requires_marketplace_targets,
            "requires_confirmed_identity": self.requires_confirmed_identity,
            "requires_financial_lock": self.requires_financial_lock,
            "execution_class": self.execution_class.value,
            "business_priority": self.business_priority,
            "config_schema": _safe_json_schema(self.config_model.model_json_schema()),
        }


class WorkflowRegistry:
    """Registry deliberately has no dynamic import or upload feature."""

    def __init__(
        self, workflows: Iterable[Workflow | WorkflowDefinition] = ()
    ) -> None:
        self._items: dict[str, Workflow] = {}
        self._definitions: dict[str, WorkflowDefinition] = {}
        for workflow in workflows:
            self.register(workflow)

    def register(self, workflow: Workflow | WorkflowDefinition) -> None:
        """Register a definition, or wrap the legacy workflow-only form."""

        definition = (
            workflow
            if isinstance(workflow, WorkflowDefinition)
            else WorkflowDefinition.legacy(workflow)
        )
        self.register_definition(definition)

    def register_definition(self, definition: WorkflowDefinition) -> None:
        if not isinstance(definition, WorkflowDefinition):
            raise TypeError("definition 必须是 WorkflowDefinition")
        name = definition.key
        if name in self._definitions:
            raise ValueError(f"工作流已注册：{name}")
        self._definitions[name] = definition
        if definition.workflow is not None:
            self._items[name] = definition.workflow

    def get(self, name: str) -> Workflow:
        """Return the executable object (backward-compatible API)."""

        definition = self.definition(name)
        if definition.workflow is None:
            raise WorkflowNotRegistered(
                f"工作流已注册页面元数据，但当前进程未绑定执行器：{name}"
            )
        return definition.workflow

    def definition(self, name: str) -> WorkflowDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise WorkflowNotRegistered(f"工作流不在代码白名单中：{name}") from exc

    def definitions(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def validate_config(
        self, name: str, value: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Validate and normalize config through the registered Pydantic model."""

        return self.definition(name).normalize_config(value)

    def public_metadata(self) -> tuple[dict[str, Any], ...]:
        """Return a fresh, sorted metadata snapshot safe for an HTTP response."""

        return tuple(definition.to_public_dict() for definition in self.definitions())

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


def _safe_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only a conservative, local-only JSON Schema subset."""

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SAFE_SCHEMA_KEYS:
            continue
        if key in {"properties", "$defs"}:
            if not isinstance(value, Mapping):
                continue
            result[key] = {
                str(name): _safe_json_schema(child)
                for name, child in value.items()
                if _PROPERTY_NAME.fullmatch(str(name)) and isinstance(child, Mapping)
            }
            continue
        if key == "$ref":
            if isinstance(value, str) and value.startswith("#/$defs/"):
                result[key] = value
            continue
        if key in {"items"}:
            if isinstance(value, Mapping):
                result[key] = _safe_json_schema(value)
            continue
        if key in {"allOf", "anyOf", "oneOf"}:
            if isinstance(value, list):
                result[key] = [
                    _safe_json_schema(item) for item in value if isinstance(item, Mapping)
                ]
            continue
        if key in {"required", "enum"}:
            if isinstance(value, list):
                result[key] = [_safe_scalar(item) for item in value if _is_safe_scalar(item)]
            continue
        if _is_safe_scalar(value):
            result[key] = _safe_scalar(value)
    return result


def _is_safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if not _is_safe_scalar(value):  # pragma: no cover - guarded by callers
        raise TypeError("JSON Schema value is not scalar")
    return value
