"""Route a persisted workflow run to its code-registered execution engine.

The payout engine deliberately keeps all of its financial invariants.  This
small outer router is the extension seam for future read-only or ordinary
store workflows, so adding one does not require weakening the payout engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .registry import WorkflowRegistry


class WorkflowDispatcher:
    """Dispatch engine calls by the registered workflow execution class."""

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        engines: Mapping[str, Any],
        repository: Any,
    ) -> None:
        self.registry = registry
        self.engines = dict(engines)
        self.repository = repository
        if not self.engines:
            raise ValueError("at least one workflow execution engine is required")
        definitions = getattr(registry, "definitions", None)
        if callable(definitions):
            registered = tuple(definitions())
            unbound = sorted(
                str(getattr(definition, "key", "<unknown>"))
                for definition in registered
                if definition.workflow is None
            )
            if unbound:
                raise ValueError(
                    "registered workflows are missing executable bindings: "
                    + ", ".join(unbound)
                )
            missing = sorted(
                {
                    str(definition.execution_class)
                    for definition in registered
                    if str(definition.execution_class) not in self.engines
                }
            )
            if missing:
                raise ValueError(
                    "registered workflows are missing execution engines: "
                    + ", ".join(missing)
                )

    @property
    def delivery_service(self) -> Any | None:
        for engine in self.engines.values():
            delivery = getattr(engine, "delivery_service", None)
            if delivery is not None:
                return delivery
        return None

    @delivery_service.setter
    def delivery_service(self, value: Any | None) -> None:
        for engine in self.engines.values():
            if hasattr(engine, "delivery_service"):
                engine.delivery_service = value

    def engine_for(self, workflow_key: str) -> Any:
        definition = self.registry.definition(workflow_key)
        execution_class = str(definition.execution_class)
        try:
            return self.engines[execution_class]
        except KeyError as exc:
            raise RuntimeError(
                f"工作流 {workflow_key} 的执行器尚未在当前版本注册"
            ) from exc

    async def start(self, run: Any) -> Any:
        return await self.engine_for(run.workflow).start(run)

    async def approve(self, run: Any, *, actor: str = "admin") -> Any:
        return await self.engine_for(run.workflow).approve(run, actor=actor)

    async def execute_approved(self, run: Any) -> Any:
        engine = self.engine_for(run.workflow)
        action = getattr(engine, "execute_approved", None)
        if not callable(action):
            raise RuntimeError(f"工作流 {run.workflow} 不支持审核后执行")
        return await action(run)

    async def continue_auth(self, run: Any) -> Any:
        return await self.engine_for(run.workflow).continue_auth(run)

    async def reconcile(self, run: Any) -> Any:
        return await self.engine_for(run.workflow).reconcile(run)

    async def expire_auth(self, run: Any) -> Any:
        return await self.engine_for(run.workflow).expire_auth(run)

    async def cancel(self, run: Any) -> Any:
        return await self.engine_for(run.workflow).cancel(run)
