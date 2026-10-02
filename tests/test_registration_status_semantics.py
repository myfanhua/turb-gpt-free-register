import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCES = (
    ROOT / "main.py",
    ROOT / "core" / "roxy_registration.py",
    ROOT / "core" / "cloakbrowser_registration.py",
    ROOT / "core" / "browser_use_registration.py",
)
EXPECTED_KEYS = (
    "task_status",
    "account_status",
    "codex_status",
    "phase",
    "error_code",
    "retryable",
)


def _load_status_builder(source_path):
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    assignments = {}
    result_return = None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {"codex_status", "codex_ok", "task_status"}:
                assignments[target.id] = node
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            contains_codex_ok = any(
                isinstance(child, ast.Name) and child.id == "codex_ok"
                for value in node.value.values
                for child in ast.walk(value)
            )
            if "task_status" in keys and contains_codex_ok:
                result_return = node

    missing = {"codex_status", "codex_ok"} - assignments.keys()
    if missing or result_return is None:
        raise AssertionError(f"status contract not found in {source_path}: {missing}")

    result_items = [
        (key, value)
        for key, value in zip(result_return.value.keys, result_return.value.values)
        if isinstance(key, ast.Constant) and key.value in EXPECTED_KEYS
    ]
    body = [assignments[name] for name in ("codex_status", "codex_ok", "task_status") if name in assignments]
    body.append(ast.Return(value=ast.Dict(
        keys=[key for key, _ in result_items],
        values=[value for _, value in result_items],
    )))
    function = ast.FunctionDef(
        name="build_status",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="codex_result")],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=body,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["build_status"]


class RegistrationStatusSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builders = {path: _load_status_builder(path) for path in SOURCES}

    def test_explicit_codex_skip_is_complete_success(self):
        expected = {
            "task_status": "success",
            "account_status": "success",
            "codex_status": "skipped",
            "phase": "completed",
            "error_code": None,
            "retryable": False,
        }
        for path, build_status in self.builders.items():
            with self.subTest(path=path):
                self.assertEqual(
                    build_status({"status": "skipped", "ok": False, "message": "disabled"}),
                    expected,
                )

    def test_failed_codex_remains_partial_success_and_retryable(self):
        expected = {
            "task_status": "partial_success",
            "account_status": "success",
            "codex_status": "failed",
            "phase": "codex",
            "error_code": "codex_failed",
            "retryable": True,
        }
        for path, build_status in self.builders.items():
            with self.subTest(path=path):
                self.assertEqual(
                    build_status({"status": "failed", "ok": False, "message": "transient"}),
                    expected,
                )

    def test_deactivated_codex_is_partial_success_without_retry(self):
        for path, build_status in self.builders.items():
            with self.subTest(path=path):
                result = build_status({"status": "deactivated", "ok": False})
                self.assertEqual(result["task_status"], "partial_success")
                self.assertFalse(result["retryable"])


if __name__ == "__main__":
    unittest.main()
