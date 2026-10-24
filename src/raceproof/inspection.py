from __future__ import annotations

import ast
from pathlib import Path

from raceproof.contracts import StaticFinding


class ConcurrencyInspector(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.findings: list[StaticFinding] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._inspect_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._inspect_function(node)
        self.generic_visit(node)

    def _inspect_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        source_signals: list[str] = []
        calls = {
            name
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and (name := _call_name(child.func)) is not None
        }
        has_select = bool(calls & {"select", "get", "query", "scalar", "scalar_one"})
        has_count = "count" in calls
        has_if = any(isinstance(child, ast.If) for child in ast.walk(node))
        has_derived_write = any(
            isinstance(child, (ast.Assign, ast.AugAssign))
            and (
                isinstance(child, ast.AugAssign)
                or (isinstance(child, ast.Assign) and isinstance(child.value, ast.BinOp))
            )
            for child in ast.walk(node)
        )
        has_dependent_insert = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"add", "add_all"}
            for child in ast.walk(node)
        )
        has_update_call = bool(calls & {"update", "delete"})
        has_flush = "flush" in calls
        has_transaction_boundary = bool(calls & {"begin", "commit", "flush"})
        has_row_lock = "with_for_update" in calls
        if has_select:
            source_signals.append("row read")
        if has_count:
            source_signals.append("aggregate count read")
        if has_if:
            source_signals.append("check then act")
        if has_derived_write:
            source_signals.append("write derived from observed value")
        if has_update_call:
            source_signals.append("database update")
        if has_dependent_insert:
            source_signals.append("dependent insert")
        if has_flush:
            source_signals.append("transactional flush")
        elif has_transaction_boundary:
            source_signals.append("transaction boundary")
        if has_count and has_dependent_insert:
            source_signals.append("count then insert")
        if has_if and (has_derived_write or has_update_call):
            source_signals.append("check then update")
        if (has_select or has_count) and (
            has_derived_write or has_update_call or has_dependent_insert
        ) and not has_row_lock:
            source_signals.append("no obvious row lock")
        if len(source_signals) >= 3:
            self.findings.append(
                StaticFinding(
                    file=self.relative_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    function=node.name,
                    severity="high",
                    signals=source_signals,
                    explanation=(
                        "Static analysis found a read/check/write transaction shape. This is a "
                        "hypothesis only until a real schedule violates the approved invariant."
                    ),
                )
            )


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def inspect_python_file(path: Path, repository_root: Path) -> list[StaticFinding]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    inspector = ConcurrencyInspector(path.relative_to(repository_root).as_posix())
    inspector.visit(tree)
    return inspector.findings
