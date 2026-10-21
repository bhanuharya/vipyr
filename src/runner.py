"""
Build/run harness for the vipyr compiler.

This module separates the *compile* (frontend, opt, llc, link) and *execute*
(QEMU) steps from the compiler itself, and resolves all external tool paths
from environment variables with sensible defaults. It is used by the test
suite and can be used interactively.

Environment variable configuration:

    PYRV_OPT        path to opt (default: opt-14, then opt)
    PYRV_LLC        path to llc with RISC-V backend (default: llc-14, then llc)
    PYRV_CC         path to the RISC-V cross-clang (default:
                    riscv32-unknown-linux-gnu-clang)
    PYRV_SYSROOT    cross sysroot for --sysroot (default: /opt/riscv/sysroot)
    PYRV_QEMU_USER  qemu-riscv32 user-mode binary (default: qemu-riscv32)
    PYRV_OBJDUMP    llvm-objdump, for --count's instruction mix
                    (default: llvm-objdump-14, then llvm-objdump)
    PYRV_READLINE   path to readline.c helper (default: ./readline.c)
    PYRV_RUNTIME_LIB path to a runtime shared dir containing readline.c
                     (used when readline.c is not in the current dir)
    PYRV_RUNTIME    path to runtime.c (heap allocator) (default: ./runtime.c)

When neither PYRV_CC nor riscv32-unknown-linux-gnu-clang is available, the
link step falls back to Zig's bundled clang and musl (`pip install
ziglang`). That needs no sysroot and produces static rv32gc/ilp32d binaries.

Command line:

    python -m src.runner prog.py [--passes simplifycfg,instcombine] [--count]
                                 [--accel CERT]

--count runs the program under QEMU's instruction trace and reports how many
RISC-V instructions it executed, and where (see src/icount.py).
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The optimization pipeline used by default in build_benchmark / tests.
DEFAULT_PASSES = 'simplifycfg,instcombine,sroa,dce'

RISCV_MARCH = 'rv32gc'
RISCV_MABI = 'ilp32d'
LLC_MATTRS = '+m,+a,+f,+d'

# Zig fallback: baseline_rv32 is rv32gc, which selects the ilp32d ABI.
ZIG_TARGET = 'riscv32-linux-musl'
ZIG_CPU = 'baseline_rv32'

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _which(default: str, env: str) -> str:
    """Return env override if set, else the resolved path of `default`.

    A bare name (no directory separator) is resolved through PATH even when
    it comes from an environment override, so `PYRV_QEMU_USER=qemu-riscv32`
    works the same as `PYRV_QEMU_USER=/usr/bin/qemu-riscv32`.
    """
    override = os.environ.get(env)
    name = override if override else default
    if name and os.sep not in name:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return name if name else default


def _which_any(defaults: list[str], env: str) -> str:
    """Like _which, but without an override try each default on PATH."""
    if not os.environ.get(env):
        for name in defaults:
            resolved = shutil.which(name)
            if resolved:
                return resolved
    return _which(defaults[0], env)


def opt_path() -> str:
    return _which_any(['opt-14', 'opt'], 'PYRV_OPT')


def llc_path() -> str:
    return _which_any(['llc-14', 'llc'], 'PYRV_LLC')


def cc_path() -> str:
    return _which('riscv32-unknown-linux-gnu-clang', 'PYRV_CC')


def sysroot_path() -> str:
    return os.environ.get('PYRV_SYSROOT', '/opt/riscv/sysroot')


def qemu_user_path() -> str:
    return _which('qemu-riscv32', 'PYRV_QEMU_USER')


def objdump_path() -> str:
    return _which_any(['llvm-objdump-14', 'llvm-objdump'], 'PYRV_OBJDUMP')


def readline_path() -> str:
    """Locate readline.c (the C runtime helper for input())."""
    override = os.environ.get('PYRV_READLINE')
    if override:
        return override
    local = _REPO_ROOT / 'readline.c'
    if local.exists():
        return str(local)
    libdir = os.environ.get('PYRV_RUNTIME_LIB')
    if libdir:
        candidate = Path(libdir) / 'readline.c'
        if candidate.exists():
            return str(candidate)
    return str(local)


def runtime_path() -> str:
    """Locate runtime.c (the C runtime helper: bump allocator)."""
    override = os.environ.get('PYRV_RUNTIME')
    if override:
        return override
    local = _REPO_ROOT / 'runtime.c'
    if local.exists():
        return str(local)
    return str(local)


def _run(cmd: list[str], cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)


class ToolchainError(Exception):
    """A required external tool is missing or a stage failed."""


def _check_tool(path: str, name: str):
    if not os.path.exists(path) or not os.access(path, os.X_OK):
        raise ToolchainError(f'{name} not found or not executable: {path}')


@functools.lru_cache(maxsize=None)
def _opt_has_no_verify_fixpoint(opt: str) -> bool:
    proc = subprocess.run([opt, '-passes=instcombine<no-verify-fixpoint>',
                           '-disable-output'], input='', capture_output=True)
    return proc.returncode == 0


def opt_pipeline(opt: str, passes: str) -> str:
    """
    Turn off instcombine's fixpoint self-check where opt supports it.

    Since LLVM 18, naming `instcombine` in -passes runs one iteration and
    aborts if more work remains. It is a check for LLVM's own tests (the -O
    pipelines leave it off), it does not indicate a miscompile, and it
    fires on tests/medium.py.
    """
    if not _opt_has_no_verify_fixpoint(opt):
        return passes
    return re.sub(r'\binstcombine\b(?!<)', 'instcombine<no-verify-fixpoint>', passes)


def use_zig() -> bool:
    """True when linking falls back to Zig + musl (see module docstring)."""
    if os.environ.get('PYRV_CC') or shutil.which('riscv32-unknown-linux-gnu-clang'):
        return False
    return importlib.util.find_spec('ziglang') is not None


def link_command() -> list[str]:
    """Compiler-driver prefix that links IR/C inputs into a RISC-V executable."""
    if use_zig():
        return [sys.executable, '-m', 'ziglang', 'cc', '-target', ZIG_TARGET,
                f'-mcpu={ZIG_CPU}', '-Wno-override-module']
    cc = cc_path()
    _check_tool(cc, 'clang')
    return [cc, f'--sysroot={sysroot_path()}', f'-march={RISCV_MARCH}',
            f'-mabi={RISCV_MABI}']


def check_toolchain():
    """Raise ToolchainError unless everything needed for a target run is present."""
    _check_tool(opt_path(), 'opt')
    _check_tool(llc_path(), 'llc')
    _check_tool(qemu_user_path(), 'qemu-riscv32')
    if not use_zig():
        _check_tool(cc_path(), 'clang')
        if not os.path.isdir(sysroot_path()):
            raise ToolchainError(f'sysroot not found: {sysroot_path()}')


def accel_flags(src: str, certificates: list[str], out_dir: Path) -> list[str]:
    """
    Frontend flags for proven custom instructions, after checking each
    certificate against the program being compiled. Its answer key is
    recomputed from the source. If that differs from the one the circuit was
    proved against (the function changed, or another LLVM version prints it
    differently), the circuit named by the certificate is put through the
    cage again, and the certificate is refused unless it passes.
    """
    from src import accel, cage, spec
    flags, keys = [], {}
    for path in certificates:
        cert = accel.Certificate.load(path)
        if cert.key in keys:
            raise ToolchainError(f'{path} and {keys[cert.key]} use the same encoding '
                                 f'(funct3={cert.funct3}, funct7={cert.funct7})')
        keys[cert.key] = path
        try:
            key = spec.from_program(src, cert.function, str(out_dir / f'spec-{cert.function}'))
        except spec.SpecError as e:
            raise ToolchainError(f'{path}: {e}')
        if accel.sha256_text(key.ir) != cert.answer_key_sha256:
            circuit = Path(path).parent / cert.circuit
            if not circuit.is_file() or accel.sha256_file(circuit) != cert.circuit_sha256:
                raise ToolchainError(f'{path}: {cert.function} has changed since its circuit was '
                                     f'proved, and the circuit {circuit} is missing or modified')
            verdict = cage.judge(str(circuit), key)
            if not verdict.accepted:
                failed = '; '.join(c.detail for c in verdict.checks if not c.passed)
                raise ToolchainError(f'{path}: {cert.function} has changed since its circuit was '
                                     f'proved, and the circuit no longer matches it: {failed}')
        flags += ['--accel', f'{cert.function}:{cert.funct3}:{cert.funct7}']
    return flags


def compile_program(src: str, out_dir: str, passes: str | None = None,
                    name: str | None = None,
                    bounds_check: bool = False,
                    accel: list[str] | None = None) -> dict[str, str]:
    """
    Compile a .py source through IR -> opt -> llc -> link.

    `accel` lists certificates (src/accel.py) for functions to replace with
    proven custom instructions.

    Returns a dict mapping stage names to output file paths:
        ir, opt_bc, obj, asm, exe
    """
    src = str(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = name or Path(src).stem

    ir = out_dir / f'{base}.ll'
    opt_bc = out_dir / f'{base}.opt.bc'
    obj = out_dir / f'{base}.o'
    asm = out_dir / f'{base}.s'
    exe = out_dir / f'{base}.riscv'

    # Frontend (the compiler itself). Run in-process via a small helper
    # script to avoid importing from the harness's own cwd issues.
    frontend = _REPO_ROOT / 'main.py'
    frontend_cmd = [os.sys.executable, str(frontend), src, '-o', str(ir)]
    if bounds_check:
        frontend_cmd.append('--bounds-check')
    if accel:
        frontend_cmd += accel_flags(src, accel, out_dir)
    proc = _run(frontend_cmd)
    if proc.returncode != 0:
        raise ToolchainError(f'frontend failed:\n{proc.stderr}')

    # Optimize + verify.
    opt = opt_path()
    _check_tool(opt, 'opt')
    if passes:
        proc = _run([opt, str(ir), '-o', str(opt_bc),
                     '-passes=' + opt_pipeline(opt, passes)])
        if proc.returncode != 0:
            raise ToolchainError(f'opt failed:\n{proc.stderr}')
    else:
        opt_bc = ir

    # Always verify the module that will be code-generated.
    proc = _run([opt, str(opt_bc), '-passes=verify', '-S', '-o', os.devnull])
    if proc.returncode != 0:
        raise ToolchainError(f'opt -verify failed:\n{proc.stderr}')

    # Codegen. llc writes the object we link and, for reading, the same code
    # as assembly. Linking the bitcode instead would let the C driver
    # recompile it at -O0 and throw llc's output away.
    llc = llc_path()
    _check_tool(llc, 'llc')
    llc_cmd = [llc, '-march=riscv32', '-mattr=' + LLC_MATTRS,
               '-target-abi=' + RISCV_MABI, str(opt_bc)]
    for filetype, out in (('obj', obj), ('asm', asm)):
        proc = _run(llc_cmd + ['-filetype=' + filetype, '-o', str(out)])
        if proc.returncode != 0:
            raise ToolchainError(f'llc failed:\n{proc.stderr}')

    # Link. Float `%` lowers to a call to fmod, hence -lm.
    proc = _run(link_command() + [str(obj), readline_path(),
                                  runtime_path(), '-lm', '-o', str(exe)])
    if proc.returncode != 0:
        raise ToolchainError(f'link failed:\n{proc.stderr}')

    return {
        'ir': str(ir),
        'opt_bc': str(opt_bc),
        'obj': str(obj),
        'asm': str(asm),
        'exe': str(exe),
    }


def qemu_command(exe: str, qemu_args: tuple[str, ...] = ()) -> list[str]:
    """The qemu-riscv32 command line that runs `exe`."""
    qemu = qemu_user_path()
    _check_tool(qemu, 'qemu-riscv32')
    sysroot = sysroot_path()
    # -L lets QEMU find a glibc sysroot's dynamic loader. Static Zig/musl
    # binaries don't need one, so skip it when there is no sysroot.
    if os.path.isdir(sysroot):
        qemu_args = ('-L', sysroot) + tuple(qemu_args)
    return [qemu, *qemu_args, str(exe)]


def run_qemu(exe: str, stdin: str | None = None, timeout: int = 120,
             max_output: int = 10 * 1024 * 1024) -> tuple[int, str, str]:
    """
    Run a RISC-V executable under QEMU user mode.

    Returns (exit_code, stdout, stderr). The guest exit status is returned
    as-is (0 on success). Output beyond max_output bytes is truncated.
    """
    cmd = qemu_command(exe)
    try:
        proc = subprocess.run(
            cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout)
        return proc.returncode, proc.stdout[:max_output], proc.stderr[:max_output]
    except subprocess.TimeoutExpired as e:
        return -1, (e.stdout or b'')[:max_output], (e.stderr or b'')[:max_output]


def build_and_run(src: str, passes: str | None = DEFAULT_PASSES,
                  timeout: int = 120, keep: bool = False) -> tuple[int, str]:
    """
    Compile `src` (with the given opt passes) and run it under QEMU.

    Returns (exit_code, stdout text). Raises ToolchainError on build failure.
    """
    tmp = tempfile.mkdtemp(prefix='pyrv-')
    try:
        compile_program(src, tmp, passes=passes)
        code, out, err = run_qemu(
            Path(tmp) / (Path(src).stem + '.riscv'), timeout=timeout)
        if code != 0 and err:
            out = out + '\n[stderr]\n' + err
        return code, out
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m src.runner',
        description='Compile a vipyr program to RISC-V and run it under QEMU.')
    parser.add_argument('src', help='input .py file')
    parser.add_argument('--passes', default=DEFAULT_PASSES,
                        help=f'opt pass pipeline, "" for none (default: {DEFAULT_PASSES})')
    parser.add_argument('--bounds-check', action='store_true',
                        help='emit runtime bounds checks for subscripts')
    parser.add_argument('--keep', metavar='DIR',
                        help='write the .ll/.s/.riscv artifacts to DIR')
    parser.add_argument('--count', action='store_true',
                        help='count executed instructions (report on stderr)')
    parser.add_argument('--accel', action='append', metavar='CERT',
                        help='use the proven custom instruction in certificate CERT '
                             '(written by python -m src.cage --accept)')
    args = parser.parse_args(argv)

    out_dir = args.keep or tempfile.mkdtemp(prefix='pyrv-')
    try:
        arts = compile_program(args.src, out_dir, passes=args.passes or None,
                               bounds_check=args.bounds_check, accel=args.accel)
        if args.count:
            from src import icount
            profile = icount.profile_program(arts)
            code, out, err = profile.exit_code, profile.stdout, profile.stderr
        else:
            code, out, err = run_qemu(arts['exe'])
        sys.stdout.buffer.write(out)
        sys.stderr.buffer.write(err)
        if args.count:
            sys.stdout.flush()
            print('\n' + icount.report(profile), file=sys.stderr)
        return code
    except ToolchainError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
