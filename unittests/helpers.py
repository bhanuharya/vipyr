"""
Shared helpers for the pyrv regression test suite.

These keep the tests deterministic and free of hard-coded absolute paths:
all external tools are resolved through src.runner, which honours the
PYRV_* environment variables with sensible defaults.
"""

from __future__ import annotations

import ast
import io
import shutil
import tempfile
from pathlib import Path

from src.compiler import Compiler
from src.reference_evaluator import ReferenceEvaluator
from src.type_check import TypeChecker
from src.error import CompileError
from src import runner

# The default opt pipeline for equivalence testing. Overridable per test.
DEFAULT_PASSES = runner.DEFAULT_PASSES


def typecheck_source(src: str, name: str = 'test.py'):
    """Return the checked AST root, or raise CompileError."""
    tc = TypeChecker(name)
    root = ast.parse(src, filename=name)
    return tc.evalNode(root)


def ir_text_for(src: str, name: str = 'test', bounds_check: bool = False) -> str:
    """Run the frontend+backend and return the emitted LLVM IR text."""
    tc = TypeChecker(name)
    checked = tc.evalNode(ast.parse(src, filename=name))
    from src.llvm import LLVMBackend
    backend = LLVMBackend(name, outFile='/dev/null', boundsCheck=bounds_check)
    backend.evalNode(checked, False)
    backend.emitFile()
    return str(backend.module)


def oracle_output(src: str, stdin: str | None = None) -> tuple[str, int]:
    """
    Run the reference evaluator on `src`.

    Returns (stdout, exit_status). The evaluator's exit status is always 0
    (mirroring a program that runs to completion and main returns 0).
    """
    tc = TypeChecker('oracle.py')
    root = ast.parse(src, filename='oracle.py')
    checked = tc.evalNode(root)
    stream = io.StringIO(stdin) if stdin is not None else None
    ev = ReferenceEvaluator(stdin=stream)
    return ev.run(checked)


def _write_temp(src: str, name: str) -> str:
    d = tempfile.mkdtemp(prefix='pyrv-test-')
    p = Path(d) / name
    p.write_text(src)
    return str(p)


def qemu_output(src: str, passes: str | None = DEFAULT_PASSES,
                stdin: str | None = None, timeout: int = 120,
                bounds_check: bool = False) -> tuple[int, str]:
    """Compile `src` and run it under QEMU. Returns (exit_code, stdout)."""
    py = _write_temp(src, 'prog.py')
    try:
        tmp = tempfile.mkdtemp(prefix='pyrv-run-')
        try:
            arts = runner.compile_program(py, tmp, passes=passes, name='prog',
                                          bounds_check=bounds_check)
            code, out, _err = runner.run_qemu(arts['exe'], stdin=stdin,
                                              timeout=timeout)
            return code, out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        shutil.rmtree(Path(py).parent, ignore_errors=True)


def toolchain_available() -> bool:
    """True if the full external toolchain is present."""
    try:
        runner.check_toolchain()
        return True
    except runner.ToolchainError:
        return False
