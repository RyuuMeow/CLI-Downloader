import asyncio
from threading import Event
from unittest import IsolatedAsyncioTestCase

from textual.widgets import DataTable, TabbedContent

from cli_downloader.cli.shell import InteractiveShell, TextualShellApp
from cli_downloader.core.task_tree import (
    TaskKind,
    TaskProgress,
    TaskRootPage,
    TaskRootSummary,
    TaskState,
)


class PagedService:
    def __init__(self):
        self.items = tuple(
            TaskRootSummary(
                f"item-{i}", TaskKind.ITEM, TaskState.COMPLETED, f"Item {i}", TaskProgress(1, 1)
            )
            for i in range(205)
        )
        self.calls = []
        self.started = Event()
        self.release = Event()
        self.release.set()

    def list_root_summaries(self):
        raise AssertionError("UI loaded all roots")

    def list_root_page(self, *, offset, limit):
        self.calls.append((offset, limit))
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError("test did not release projection")
        return TaskRootPage(self.items[offset : offset + limit], len(self.items), offset, limit)

    def get(self, task_id):
        return next(row.to_task_node() for row in self.items if row.task_id == task_id)

    def list_files(self, task_id):
        return ()


class ShellTaskPagingTests(IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.02)

    async def test_roots_page_and_restore_position_after_opening_item(self):
        service = PagedService()
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))
        async with app.run_test(size=(100, 35)) as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await self.wait_for(lambda: len(app._task_nodes) == 100)
            app._task_next_page()
            await self.wait_for(lambda: "item-100" in app._task_nodes)
            self.assertEqual(app._task_page_offset, 100)
            self.assertEqual(app.query_one("#task-table", DataTable).row_count, 100)
            app._open_task("item-100")
            self.assertEqual(app._task_level, "files")
            app._task_back()
            await self.wait_for(lambda: app._task_refresh_thread is None)
            self.assertEqual(app._task_level, "roots")
            self.assertEqual(app._task_page_offset, 100)
            self.assertEqual(app._active_task_id, "item-100")
            app._task_next_page()
            await self.wait_for(lambda: len(app._task_nodes) == 5)
            self.assertEqual(app._task_page_offset, 200)
            # Auto-delete can empty the final page while it is displayed.
            service.items = service.items[:200]
            app._refresh_tasks()
            await self.wait_for(
                lambda: app._task_page_offset == 100 and len(app._task_nodes) == 100
            )
            await pilot.pause()

    async def test_timer_refresh_does_not_invalidate_inflight_same_page(self):
        service = PagedService()
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))
        async with app.run_test(size=(100, 35)) as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await self.wait_for(
                lambda: len(app._task_nodes) == 100 and app._task_refresh_thread is None
            )
            await pilot.pause()
            service.started.clear()
            service.release.clear()
            try:
                app._refresh_tasks()
                await self.wait_for(service.started.is_set)
                generation = app._task_refresh_generation
                calls = len(service.calls)
                for _ in range(5):
                    app._refresh_tasks()
                self.assertEqual(app._task_refresh_generation, generation)
                self.assertEqual(len(service.calls), calls)
                service.release.set()
                await self.wait_for(lambda: app._task_refresh_thread is None)
            finally:
                service.release.set()

    async def test_pausing_roots_can_resume_without_starting_another_worker(self):
        from dataclasses import replace
        from unittest.mock import Mock
        from textual.widgets import Button

        service = PagedService()
        service.items = tuple(
            TaskRootSummary(kind.value, kind, TaskState.PAUSING, kind.value, TaskProgress())
            for kind in (TaskKind.ITEM, TaskKind.BATCH)
        )
        resumed = []

        def resume(task_id):
            resumed.append(task_id)
            service.items = tuple(
                replace(row, state=TaskState.RUNNING) if row.task_id == task_id else row
                for row in service.items
            )

        service.resume = resume
        service.is_running = lambda _: True
        service.is_batch_running = lambda _: True
        background = Mock()
        app = TextualShellApp(
            InteractiveShell(
                task_service_loader=lambda: service,
                background_runner=background,
            )
        )
        async with app.run_test(size=(100, 35)) as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await self.wait_for(lambda: len(app._task_nodes) == 2)
            await pilot.pause()
            for task_id in ("item", "batch"):
                app._active_task_id = task_id
                app._set_task_actions(service.get(task_id))
                button = app.query_one("#task-resume", Button)
                self.assertTrue(button.display)
                self.assertFalse(button.disabled)
                await pilot.pause()
                await pilot.click("#task-resume")
                await self.wait_for(lambda: task_id in resumed)
                await self.wait_for(lambda: app._task_nodes[task_id].state is TaskState.RUNNING)
            self.assertEqual(resumed, ["item", "batch"])
            background.assert_not_called()
