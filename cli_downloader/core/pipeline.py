from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from cli_downloader.core.events import TaskEvent


@dataclass(frozen=True)
class PipelineContext:
    task_id: str
    data: dict[str, Any] = field(default_factory=dict)
    events: tuple[TaskEvent, ...] = ()

    def with_data(self, **values: Any) -> "PipelineContext":
        data = dict(self.data)
        data.update(values)
        return replace(self, data=data)

    def emit(self, name: str, reason: str, **payload: Any) -> "PipelineContext":
        event = TaskEvent(
            task_id=self.task_id,
            name=name,
            reason=reason,
            payload=payload,
        )
        return replace(self, events=(*self.events, event))


class PipelineStage(Protocol):
    name: str

    async def run(self, context: PipelineContext) -> PipelineContext: ...


class PipelineRunner:
    def __init__(self, stages: tuple[PipelineStage, ...]) -> None:
        self._stages = stages

    async def run(self, context: PipelineContext) -> PipelineContext:
        current = context
        for stage in self._stages:
            current = current.emit(
                name="stage_started",
                reason=f"{stage.name}_started",
            )
            current = await stage.run(current)
            current = current.emit(
                name="stage_finished",
                reason=f"{stage.name}_finished",
            )
        return current
