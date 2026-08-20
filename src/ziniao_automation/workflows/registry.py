"""Code-only workflow allow-list."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import Workflow
from .errors import WorkflowNotRegistered


class WorkflowRegistry:
    """Registry deliberately has no dynamic import or upload feature."""

    def __init__(self, workflows: Iterable[Workflow] = ()) -> None:
        self._items: dict[str, Workflow] = {}
        for workflow in workflows:
            self.register(workflow)

    def register(self, workflow: Workflow) -> None:
        name = str(workflow.name).strip()
        if not name:
            raise ValueError("workflow.name 不能为空")
        if name in self._items:
            raise ValueError(f"工作流已注册：{name}")
        self._items[name] = workflow

    def get(self, name: str) -> Workflow:
        try:
            return self._items[name]
        except KeyError as exc:
            raise WorkflowNotRegistered(f"工作流不在代码白名单中：{name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

