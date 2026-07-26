import asyncio
from threading import Event, Thread
from unittest import TestCase

from cli_downloader.application.task_execution_coordinator import TaskExecutionCoordinator
from cli_downloader.core.errors import StaleTaskRevisionError, TaskExecutionConflictError
from cli_downloader.storage.task_store import (
    InMemoryTaskStore,
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
)


class TaskExecutionCoordinatorTests(TestCase):
    def test_concurrent_claims_allow_exactly_one_active_run(self) -> None:
        async def scenario() -> None:
            store = InMemoryTaskStore()
            store.upsert(TaskRecord(task_id="batch-1", status=TaskStatus.PLANNED))
            coordinator = TaskExecutionCoordinator(
                task_store=store,
                run_id_factory=iter(("run-1", "run-2")).__next__,
            )

            claimed, rejected = await asyncio.gather(
                coordinator.claim("batch-1"),
                coordinator.claim("batch-1"),
                return_exceptions=True,
            )

            handles = [
                result for result in (claimed, rejected) if not isinstance(result, Exception)
            ]
            conflicts = [
                result
                for result in (claimed, rejected)
                if isinstance(result, TaskExecutionConflictError)
            ]
            self.assertEqual(len(handles), 1)
            self.assertEqual(len(conflicts), 1)
            record = store.get("batch-1")
            self.assertEqual(record.status, TaskStatus.RUNNING)
            self.assertEqual(record.active_run_id, "run-1")
            self.assertEqual(record.run_generation, 1)
            self.assertIs(coordinator.active_handle("batch-1"), handles[0])

        asyncio.run(scenario())

    def test_control_tokens_update_revision_and_reject_stale_worker_callback(self) -> None:
        async def scenario() -> None:
            store = InMemoryTaskStore()
            store.upsert(TaskRecord(task_id="batch-1", status=TaskStatus.PLANNED))
            coordinator = TaskExecutionCoordinator(task_store=store, run_id_factory=lambda: "run-1")

            handle = await coordinator.claim("batch-1")
            stale_revision = handle.revision
            pause = await coordinator.request_pause("batch-1", expected_revision=stale_revision)
            self.assertTrue(handle.pause_requested)
            self.assertEqual(pause.status, TaskStatus.RUNNING)

            with self.assertRaises(StaleTaskRevisionError):
                await coordinator.finish(
                    handle,
                    status=TaskStatus.COMPLETED,
                    expected_revision=stale_revision,
                )

            paused = await coordinator.acknowledge_pause(
                handle,
                expected_revision=handle.revision,
            )
            self.assertEqual(paused.status, TaskStatus.PAUSED)
            resumed = await coordinator.request_resume(
                "batch-1",
                expected_revision=handle.revision,
            )
            self.assertFalse(handle.pause_requested)
            self.assertEqual(resumed.status, TaskStatus.PAUSED)
            running = await coordinator.acknowledge_running(
                handle,
                expected_revision=handle.revision,
            )
            self.assertEqual(running.status, TaskStatus.RUNNING)

            completed = await coordinator.finish(
                handle,
                status=TaskStatus.COMPLETED,
                expected_revision=handle.revision,
            )
            self.assertEqual(completed.status, TaskStatus.COMPLETED)
            self.assertIsNone(completed.active_run_id)
            self.assertIsNone(coordinator.active_handle("batch-1"))

        asyncio.run(scenario())

    def test_finished_task_can_claim_next_run_generation(self) -> None:
        async def scenario() -> None:
            store = InMemoryTaskStore()
            store.upsert(TaskRecord(task_id="batch-1", status=TaskStatus.PLANNED))
            coordinator = TaskExecutionCoordinator(
                task_store=store,
                run_id_factory=iter(("run-1", "run-2")).__next__,
            )

            first = await coordinator.claim("batch-1")
            await coordinator.request_cancel("batch-1", expected_revision=first.revision)
            await coordinator.finish(
                first,
                status=TaskStatus.CANCELED,
                expected_revision=first.revision,
            )
            planned = await coordinator.request_retry("batch-1")
            second = await coordinator.claim("batch-1", expected_revision=planned.revision)

            self.assertEqual(second.run_id, "run-2")
            self.assertEqual(second.run_generation, 2)

        asyncio.run(scenario())

    def test_finish_commits_canceled_when_cancel_wins_terminal_race(self) -> None:
        async def scenario() -> None:
            store = InMemoryTaskStore()
            store.upsert(TaskRecord(task_id="batch-1", status=TaskStatus.PLANNED))
            coordinator = TaskExecutionCoordinator(
                task_store=store,
                run_id_factory=lambda: "run-1",
            )

            handle = await coordinator.claim("batch-1")
            await coordinator.request_cancel("batch-1", expected_revision=handle.revision)

            completed = await coordinator.finish(
                handle,
                status=TaskStatus.COMPLETED,
                expected_revision=handle.revision,
            )

            self.assertEqual(completed.status, TaskStatus.CANCELED)
            self.assertEqual(completed.desired_control, TaskDesiredControl.CANCEL)
            self.assertIsNone(completed.active_run_id)
            self.assertIsNone(coordinator.active_handle("batch-1"))

        asyncio.run(scenario())

    def test_synchronous_control_submits_to_background_execution_loop(self) -> None:
        store = InMemoryTaskStore()
        store.upsert(TaskRecord(task_id="batch-1", status=TaskStatus.PLANNED))
        coordinator = TaskExecutionCoordinator(task_store=store, run_id_factory=lambda: "run-1")
        claimed = Event()
        release = Event()
        errors: list[BaseException] = []

        def run() -> None:
            async def scenario() -> None:
                handle = await coordinator.claim("batch-1")
                claimed.set()
                await asyncio.to_thread(release.wait)
                await coordinator.finish(
                    handle,
                    status=TaskStatus.COMPLETED,
                    expected_revision=handle.revision,
                )

            try:
                asyncio.run(scenario())
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run)
        thread.start()
        self.assertTrue(claimed.wait(timeout=1))

        paused = coordinator.request_control_sync(TaskDesiredControl.PAUSE, "batch-1")
        self.assertEqual(paused.status, TaskStatus.RUNNING)
        self.assertEqual(paused.desired_control, TaskDesiredControl.PAUSE)
        resumed = coordinator.request_control_sync(TaskDesiredControl.RUN, "batch-1")
        self.assertEqual(resumed.desired_control, TaskDesiredControl.RUN)

        release.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(store.get("batch-1").status, TaskStatus.COMPLETED)
