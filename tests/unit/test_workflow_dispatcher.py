from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from ziniao_automation.workflows.dispatcher import WorkflowDispatcher


@dataclass(frozen=True)
class _Definition:
    execution_class: str
    workflow: object | None = True


class _Registry:
    def __init__(self, classes: dict[str, str]) -> None:
        self.classes = classes

    def definition(self, key: str) -> _Definition:
        return _Definition(self.classes[key])

    def definitions(self) -> tuple[_Definition, ...]:
        return tuple(_Definition(value) for value in self.classes.values())


class _Engine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def start(self, run):
        self.calls.append(("start", run.workflow))
        return self.name

    async def cancel(self, run):
        self.calls.append(("cancel", run.workflow))
        return True


@pytest.mark.asyncio
async def test_dispatcher_routes_each_registered_execution_class() -> None:
    financial = _Engine("financial")
    ordinary = _Engine("ordinary")
    dispatcher = WorkflowDispatcher(
        registry=_Registry({"payout": "financial_guarded", "report": "read_only"}),
        engines={"financial_guarded": financial, "read_only": ordinary},
        repository=object(),
    )

    assert await dispatcher.start(SimpleNamespace(workflow="payout")) == "financial"
    assert await dispatcher.start(SimpleNamespace(workflow="report")) == "ordinary"
    assert financial.calls == [("start", "payout")]
    assert ordinary.calls == [("start", "report")]


@pytest.mark.asyncio
async def test_dispatcher_rejects_definition_without_installed_engine() -> None:
    with pytest.raises(ValueError, match="missing execution engines"):
        WorkflowDispatcher(
            registry=_Registry({"future": "ordinary"}),
            engines={"financial_guarded": _Engine("financial")},
            repository=object(),
        )


def test_dispatcher_rejects_metadata_only_workflow_definition() -> None:
    class MetadataOnlyRegistry:
        def definitions(self):
            return (
                SimpleNamespace(
                    key="future_report",
                    execution_class="read_only",
                    workflow=None,
                ),
            )

    with pytest.raises(ValueError, match="missing executable bindings"):
        WorkflowDispatcher(
            registry=MetadataOnlyRegistry(),
            engines={"read_only": _Engine("ordinary")},
            repository=object(),
        )
