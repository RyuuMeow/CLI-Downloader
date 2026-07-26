from unittest import TestCase

from cli_downloader.core.task_tree import (
    TaskFileNode,
    TaskKind,
    TaskNode,
    TaskProgress,
    TaskState,
)


class TaskProgressTest(TestCase):
    def test_defaults(self) -> None:
        p = TaskProgress()
        self.assertEqual(p.completed_files, 0)
        self.assertIsNone(p.total_files)
        self.assertEqual(p.preparing_items, 0)
        self.assertEqual(p.completed_bytes, 0)
        self.assertIsNone(p.total_bytes)

    def test_preparing_is_true_when_total_files_is_none(self) -> None:
        self.assertTrue(TaskProgress().preparing)

    def test_preparing_is_true_when_preparing_items_positive(self) -> None:
        p = TaskProgress(total_files=5, preparing_items=1)
        self.assertTrue(p.preparing)

    def test_preparing_is_false_when_total_known_and_zero_preparing(self) -> None:
        p = TaskProgress(total_files=5, preparing_items=0, completed_files=0)
        self.assertFalse(p.preparing)

    def test_completed_files_must_be_non_negative_integer(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(completed_files=-1)

    def test_completed_files_must_not_be_bool(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(completed_files=True)  # type: ignore[arg-type]

    def test_preparing_items_must_be_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(preparing_items=-1)

    def test_total_files_rejects_negative(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(total_files=-1)

    def test_total_files_allows_none(self) -> None:
        p = TaskProgress(total_files=None)
        self.assertIsNone(p.total_files)

    def test_completed_cannot_exceed_total(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(completed_files=5, total_files=3)

    def test_total_bytes_rejects_negative(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(total_bytes=-1)

    def test_total_bytes_allows_none(self) -> None:
        p = TaskProgress(total_bytes=None)
        self.assertIsNone(p.total_bytes)

    def test_byte_progress_with_known_total(self) -> None:
        p = TaskProgress(completed_bytes=50, total_bytes=100)
        self.assertEqual(p.completed_bytes, 50)
        self.assertEqual(p.total_bytes, 100)

    def test_byte_progress_is_clamped_to_known_total(self) -> None:
        p = TaskProgress(completed_bytes=2048, total_bytes=1024)
        self.assertEqual(p.completed_bytes, 1024)
        self.assertEqual(p.total_bytes, 1024)

    def test_byte_progress_rejects_negative_completed_bytes(self) -> None:
        with self.assertRaises(ValueError):
            TaskProgress(completed_bytes=-1)

    def test_to_dict_round_trip(self) -> None:
        p = TaskProgress(
            completed_files=3,
            total_files=5,
            preparing_items=1,
            completed_bytes=100,
            total_bytes=500,
        )
        d = p.to_dict()
        self.assertEqual(d["completed_files"], 3)
        self.assertEqual(d["total_files"], 5)
        self.assertEqual(d["preparing"], True)
        self.assertEqual(d["completed_bytes"], 100)
        self.assertEqual(d["total_bytes"], 500)

    def test_to_dict_with_none_total(self) -> None:
        p = TaskProgress(total_files=None, total_bytes=None)
        d = p.to_dict()
        self.assertIsNone(d["total_files"])
        self.assertIsNone(d["total_bytes"])


class TaskNodeTest(TestCase):
    def test_item_node_construction(self) -> None:
        node = TaskNode(
            task_id="task-1",
            kind=TaskKind.ITEM,
            state=TaskState.COMPLETED,
            title="My Download",
            progress=TaskProgress(completed_files=2, total_files=2),
            site_id="example",
            source_url="https://example.test/gallery/x",
            output_files=("a.jpg", "b.jpg"),
        )
        self.assertEqual(node.task_id, "task-1")
        self.assertEqual(node.kind, TaskKind.ITEM)
        self.assertEqual(node.state, TaskState.COMPLETED)
        self.assertEqual(node.title, "My Download")
        self.assertIsNone(node.parent_id)
        self.assertEqual(node.output_files, ("a.jpg", "b.jpg"))

    def test_batch_node_construction(self) -> None:
        node = TaskNode(
            task_id="batch-1",
            kind=TaskKind.BATCH,
            state=TaskState.RUNNING,
            title="Artist Search",
            progress=TaskProgress(preparing_items=1),
        )
        self.assertEqual(node.kind, TaskKind.BATCH)

    def test_child_node_has_parent(self) -> None:
        node = TaskNode(
            task_id="child-1",
            kind=TaskKind.ITEM,
            state=TaskState.RUNNING,
            title="Child Item",
            progress=TaskProgress(completed_files=1, total_files=3),
            parent_id="batch-1",
        )
        self.assertEqual(node.parent_id, "batch-1")

    def test_defaults(self) -> None:
        node = TaskNode(
            task_id="min",
            kind=TaskKind.ITEM,
            state=TaskState.QUEUED,
            title="",
            progress=TaskProgress(),
        )
        self.assertEqual(node.site_id, None)
        self.assertEqual(node.source_url, None)
        self.assertEqual(node.output_files, ())
        self.assertEqual(node.details, {})

    def test_to_dict_includes_all_fields(self) -> None:
        node = TaskNode(
            task_id="t",
            kind=TaskKind.ITEM,
            state=TaskState.FAILED,
            title="Failed Task",
            progress=TaskProgress(completed_files=0),
            parent_id="p",
            site_id="example",
            source_url="http://x",
            output_files=("x.jpg",),
            details={"error": "bad"},
        )
        d = node.to_dict()
        self.assertEqual(d["task_id"], "t")
        self.assertEqual(d["kind"], "item")
        self.assertEqual(d["state"], "failed")
        self.assertEqual(d["title"], "Failed Task")
        self.assertEqual(d["parent_id"], "p")
        self.assertEqual(d["output_files"], ["x.jpg"])
        self.assertEqual(d["details"], {"error": "bad"})

    def test_immutability(self) -> None:
        node = TaskNode(
            task_id="t",
            kind=TaskKind.ITEM,
            state=TaskState.QUEUED,
            title="x",
            progress=TaskProgress(),
        )
        with self.assertRaises(Exception):
            node.task_id = "changed"  # type: ignore[misc]


class TaskFileNodeTest(TestCase):
    def test_construction_without_failure(self) -> None:
        f = TaskFileNode(file_id="f1", name="image.jpg", state="completed", attempts=1)
        self.assertEqual(f.file_id, "f1")
        self.assertEqual(f.name, "image.jpg")
        self.assertEqual(f.state, "completed")
        self.assertEqual(f.attempts, 1)
        self.assertIsNone(f.location)
        self.assertIsNone(f.failure)

    def test_construction_with_failure(self) -> None:
        f = TaskFileNode(
            file_id="f2",
            name="bad.jpg",
            state="failed",
            attempts=2,
            failure={"origin": "plugin", "code": "404"},
        )
        self.assertEqual(f.state, "failed")
        self.assertEqual(f.attempts, 2)
        self.assertEqual(f.failure, {"origin": "plugin", "code": "404"})

    def test_byte_progress(self) -> None:
        f = TaskFileNode(
            file_id="f3",
            name="big.bin",
            state="downloading",
            completed_bytes=1024,
            total_bytes=4096,
        )
        self.assertEqual(f.completed_bytes, 1024)
        self.assertEqual(f.total_bytes, 4096)

    def test_byte_progress_is_clamped_to_known_total(self) -> None:
        f = TaskFileNode(
            file_id="f4",
            name="big.bin",
            state="downloading",
            completed_bytes=8192,
            total_bytes=4096,
        )
        self.assertEqual(f.completed_bytes, 4096)
        self.assertEqual(f.total_bytes, 4096)

    def test_to_dict_without_failure(self) -> None:
        f = TaskFileNode(file_id="f", name="n.jpg", state="completed")
        d = f.to_dict()
        self.assertIsNone(d["failure"])

    def test_to_dict_with_failure_copies(self) -> None:
        failure = {"code": "500"}
        f = TaskFileNode(file_id="f", name="e.jpg", state="failed", failure=failure)
        d = f.to_dict()
        self.assertEqual(d["failure"], {"code": "500"})
        self.assertIsNot(d["failure"], failure)  # defensive copy

    def test_defaults(self) -> None:
        f = TaskFileNode(file_id="f", name="x.jpg", state="queued")
        self.assertEqual(f.attempts, 0)
        self.assertEqual(f.completed_bytes, 0)
        self.assertIsNone(f.total_bytes)


class TaskKindTest(TestCase):
    def test_values(self) -> None:
        self.assertEqual(TaskKind.ITEM.value, "item")
        self.assertEqual(TaskKind.BATCH.value, "batch")


class TaskStateTest(TestCase):
    def test_all_states_defined(self) -> None:
        expected = {
            "preparing",
            "queued",
            "running",
            "paused",
            "completed",
            "partially_completed",
            "failed",
            "cancelled",
            "skipped",
        }
        self.assertEqual({s.value for s in TaskState}, expected)
