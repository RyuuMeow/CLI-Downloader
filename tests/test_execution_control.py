import unittest

from cli_downloader.application.execution_control import (
    ExecutionStopReason,
    ExecutionStopSignal,
)


class ExecutionStopSignalTests(unittest.TestCase):
    def test_user_cancel_and_owner_shutdown_are_distinguishable(self) -> None:
        user_cancel = ExecutionStopSignal()
        user_cancel.set()
        self.assertTrue(user_cancel.is_set())
        self.assertTrue(user_cancel.is_user_cancel())
        self.assertFalse(user_cancel.is_owner_shutdown())
        self.assertEqual(user_cancel.reason, ExecutionStopReason.USER_CANCEL)

        owner_shutdown = ExecutionStopSignal()
        owner_shutdown.request_shutdown()
        self.assertTrue(owner_shutdown.is_set())
        self.assertFalse(owner_shutdown.is_user_cancel())
        self.assertTrue(owner_shutdown.is_owner_shutdown())
        self.assertEqual(owner_shutdown.reason, ExecutionStopReason.OWNER_SHUTDOWN)

    def test_explicit_cancel_wins_over_later_shutdown(self) -> None:
        signal = ExecutionStopSignal()
        signal.set()
        signal.request_shutdown()
        self.assertEqual(signal.reason, ExecutionStopReason.USER_CANCEL)


if __name__ == "__main__":
    unittest.main()
