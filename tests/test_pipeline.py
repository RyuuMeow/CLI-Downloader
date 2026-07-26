import asyncio
from unittest import TestCase

from cli_downloader.core.pipeline import PipelineContext, PipelineRunner


class _Stage:
    name = "example"

    async def run(self, context: PipelineContext) -> PipelineContext:
        return context.with_data(done=True)


class PipelineRunnerTest(TestCase):
    def test_runner_wraps_stage_with_events(self) -> None:
        result = asyncio.run(PipelineRunner((_Stage(),)).run(PipelineContext(task_id="task-1")))

        self.assertTrue(result.data["done"])
        self.assertEqual(
            [event.reason for event in result.events], ["example_started", "example_finished"]
        )
