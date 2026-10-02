import asyncio
import ast
import threading
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "core" / "browser_use_codex_oauth.py"


class _Logger:
    def info(self, *args, **kwargs):
        return None


def _load_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    wanted = {"_has_running_asyncio_loop", "_run_in_isolated_thread", "run_browser_use_codex_oauth"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"threading": threading, "logger": _Logger()}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class BrowserUseThreadIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = _load_functions()

    def test_active_asyncio_loop_is_detected(self):
        async def probe():
            return self.namespace["_has_running_asyncio_loop"]()

        self.assertTrue(asyncio.run(probe()))

    def test_isolated_thread_runs_elsewhere_and_forwards_exception(self):
        caller_thread = threading.get_ident()
        observed = {}

        def work(value):
            observed["thread"] = threading.get_ident()
            return value

        result = self.namespace["_run_in_isolated_thread"](work, "result")
        self.assertEqual(result, "result")
        self.assertNotEqual(observed["thread"], caller_thread)

        def fail():
            raise ValueError("fixture failure")

        with self.assertRaisesRegex(ValueError, "fixture failure"):
            self.namespace["_run_in_isolated_thread"](fail)

    def test_public_entry_routes_to_isolated_thread_when_loop_is_running(self):
        self.namespace["_has_running_asyncio_loop"] = lambda: True
        caller_thread = threading.get_ident()

        def implementation(**kwargs):
            return {"thread": threading.get_ident(), "email": kwargs["email"]}

        self.namespace["_run_browser_use_codex_oauth_impl"] = implementation
        result = self.namespace["run_browser_use_codex_oauth"]("user@example.com", force=True)

        self.assertEqual(result["email"], "user@example.com")
        self.assertNotEqual(result["thread"], caller_thread)


if __name__ == "__main__":
    unittest.main()
