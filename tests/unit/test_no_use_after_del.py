"""Forbid reading a local name after ``del`` inside the same function.

``del unused_param`` is the idiom this codebase uses to say "this argument is
deliberately ignored".  It is safe at the end of a body and in a Protocol stub,
and dangerous the moment the deleted name is still needed further down: Python
raises ``UnboundLocalError`` only when execution actually reaches the later
read, so the failure hides behind whatever usually stops the function earlier.

That is exactly how it played out.  ``_run_store_setup_account_sites`` deleted
``store_id`` at the top of its body and read it again when assembling the
terminal payload, so every unified store setup whose assisted login got through
crashed and finalized FAILED — setup could never report success.  Nothing
caught it: no test exercised that return, and ruff (configured in pyproject
with only target-version and line-length) does not flag this pattern.

This scan is a source-level test in the same spirit as
``test_frontend_page_isolation.py``, which scans app.js for its own
whole-file invariant.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ziniao_automation"


def _use_after_delete(tree: ast.AST) -> list[tuple[str, str, int, int]]:
    """Every (function, name, del_line, read_line) that reads a deleted local."""

    findings: list[tuple[str, str, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Walk this function's own body in source order.  Nested functions are
        # visited again as their own top-level entries, and a name they close
        # over is a different binding, so skip their bodies here.
        nested = {
            child
            for child in ast.walk(node)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            and child is not node
        }
        nested_nodes = {
            descendant for parent in nested for descendant in ast.walk(parent)
        }
        deleted: dict[str, int] = {}
        events: list[tuple[int, int, str, str]] = []
        for child in ast.walk(node):
            if child in nested_nodes:
                continue
            if isinstance(child, ast.Delete):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        events.append(
                            (target.lineno, target.col_offset, "del", target.id)
                        )
            elif isinstance(child, ast.Name):
                kind = "read" if isinstance(child.ctx, ast.Load) else "write"
                if kind == "write" and not isinstance(child.ctx, ast.Store):
                    continue
                events.append((child.lineno, child.col_offset, kind, child.id))
        for _line, _col, kind, name in sorted(events):
            if kind == "del":
                deleted[name] = _line
            elif kind == "write":
                deleted.pop(name, None)
            elif name in deleted:
                findings.append((node.name, name, deleted[name], _line))
    return findings


def test_no_source_file_reads_a_local_after_deleting_it() -> None:
    offenders: list[str] = []
    scanned = 0
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        scanned += 1
        # utf-8-sig, not utf-8: some sources carry a BOM (composition.py does),
        # and ast.parse rejects a leading U+FEFF that the interpreter itself
        # strips when importing the same file.
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for function, name, del_line, read_line in _use_after_delete(tree):
            offenders.append(
                f"{path.relative_to(SOURCE_ROOT.parents[1])}::{function} "
                f"读取了已删除的 {name!r}（del@{del_line} → 读取@{read_line}）"
            )
    assert scanned > 10, "扫描范围异常，可能没找到源码目录"
    assert not offenders, "del 掉的局部变量在同一函数里又被读取：\n" + "\n".join(
        offenders
    )


def test_the_scan_detects_the_pattern_it_exists_to_catch() -> None:
    """A scanner that silently matches nothing would pass forever."""

    tree = ast.parse(
        "def f(store_id, selector):\n"
        "    del selector, store_id\n"
        "    return {'store_id': store_id}\n"
    )
    assert _use_after_delete(tree) == [("f", "store_id", 2, 3)]

    reassigned = ast.parse(
        "def f(x):\n    del x\n    x = 1\n    return x\n"
    )
    assert _use_after_delete(reassigned) == [], "删除后重新赋值是合法的，不该报"

    trailing = ast.parse("def f(x, y):\n    return y\n    del x\n")
    assert _use_after_delete(trailing) == [], "del 在读取之后不该报"
