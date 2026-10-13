"""Run Core verification suite."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CORE_TEST_ROOT = ROOT / "tests"
# These suites exercise process-global Windows resources (MSYS git and a local
# HTTP server).  Running them in a fresh interpreter prevents unrelated Textual
# and asyncio cleanup from making the deterministic Core gate flaky.
ISOLATED_CORE_MODULES = frozenset(
    {"test_plugin_manager", "test_shell", "test_shell_task_paging", "test_torrent"}
)


def _load_file_suite(path: Path, loader: unittest.TestLoader) -> unittest.TestSuite:
    module_name = f"_cli_downloader_verify_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load test module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return loader.loadTestsFromModule(module)


def _core_suite(loader: unittest.TestLoader) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for path in sorted(CORE_TEST_ROOT.glob("test_*.py")):
        if path.stem in ISOLATED_CORE_MODULES:
            continue
        suite.addTests(_load_file_suite(path, loader))
    return suite


def _run_suite(label: str, suite: unittest.TestSuite) -> bool:
    print(f"\n=== {label} ===")
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)

    status = "PASS" if result.wasSuccessful() else "FAIL"
    print(f"[{status}] {label}: {result.testsRun} tests")
    return result.wasSuccessful()


def _run_isolated_module(path: Path) -> bool:
    """Run a resource-sensitive module outside the shared Core interpreter."""
    label = f"Core isolated: {path.stem}"
    print(f"\n=== {label} ===")
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", f"tests.{path.stem}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="")

    success = completed.returncode == 0
    status = "PASS" if success else "FAIL"
    print(f"[{status}] {label}")
    return success


def main(argv: list[str] | None = None) -> int:
    # Legacy tests can invoke CLI defaults. Never let them migrate the user's
    # state database, contact a live Shell owner, or activate installed plugins.
    with TemporaryDirectory(prefix="clid-verify-") as directory:
        with patch.dict(
            os.environ,
            {
                "CLI_DOWNLOADER_DATA_ROOT": str(Path(directory) / "data"),
                "CLI_DOWNLOADER_PORTABLE_ROOT": "",
                "LOCALAPPDATA": str(Path(directory) / "local"),
            },
        ):
            return _verify()


def _verify() -> int:
    loader = unittest.TestLoader()
    success = True
    for module_name in sorted(ISOLATED_CORE_MODULES):
        success = _run_isolated_module(CORE_TEST_ROOT / f"{module_name}.py") and success
    success = _run_suite("Core", _core_suite(loader)) and success
    print("\nVerification gate: PASS" if success else "\nVerification gate: FAIL")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
