"""
Count the RISC-V instructions a program actually executes.

Counting lines of disassembly (a static count) says how big the code is, not
how much work it does: a 7-instruction loop body that runs 1000 times costs
7000 instructions. This module measures the dynamic count with QEMU's own
logging, so it needs no QEMU plugins:

    -one-insn-per-tb   translate one guest instruction per translation block
    -d nochain,exec    log every block as it starts running

Together they make QEMU log exactly one line per executed instruction:

    Trace 0: 0x7f3820000100 [00000000/0000000000038904/04018078/00000201] main
                                       ^ guest PC                         ^ function

The log goes through a pipe and is tallied as it streams, so a long run
never lands on disk. Counting runs about 40x slower than a plain run.

libc's startup code walks the environment and argv[0], so the guest gets an
empty environment and a fixed argv[0]. That makes the total the same on every
machine, not just the program's own share.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from src import runner

QEMU_COUNT_ARGS = ('-0', 'prog', '-one-insn-per-tb', '-d', 'nochain,exec')

_TRACE_RE = re.compile(rb'^Trace \d+: \S+ \[[0-9a-f]+/([0-9a-f]+)/[0-9a-f]+/[0-9a-f]+\] ?(\S*)')
_ASM_FUNC_RE = re.compile(r'^\s*\.type\s+([^,\s]+),\s*@function', re.MULTILINE)
_OBJDUMP_RE = re.compile(r'^\s*([0-9a-f]+):\s+(\S+)', re.MULTILINE)


@dataclass
class Profile:
    """Executed-instruction counts for one run of a program."""
    by_pc: Counter[int]
    function_at: dict[int, str]
    exit_code: int
    stdout: bytes
    stderr: bytes
    program_functions: frozenset[str] = frozenset()
    mnemonic_at: dict[int, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.by_pc.values())

    def by_function(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for pc, n in self.by_pc.items():
            counts[self.function_at.get(pc) or f'0x{pc:x}'] += n
        return counts

    @property
    def program(self) -> int:
        """Instructions executed in functions compiled from the program."""
        return sum(n for pc, n in self.by_pc.items()
                   if self.function_at.get(pc) in self.program_functions)

    def program_mix(self) -> Counter[str]:
        """Executed count per mnemonic, over the program's own functions."""
        mix: Counter[str] = Counter()
        for pc, n in self.by_pc.items():
            if self.function_at.get(pc) in self.program_functions:
                mix[self.mnemonic_at.get(pc, '?')] += n
        return mix


def run_counted(exe: str, stdin: str | bytes | None = None,
                timeout: int = 600) -> Profile:
    """Run `exe` under QEMU and count every instruction it executes.

    With stdin=None the guest reads this process's stdin, as in run_qemu.
    """
    if isinstance(stdin, str):
        stdin = stdin.encode()
    # Guest I/O goes to files rather than pipes, so a chatty program can't
    # block while we are busy reading the log.
    with tempfile.TemporaryFile() as fin, tempfile.TemporaryFile() as fout, \
            tempfile.TemporaryFile() as ferr:
        fin.write(stdin or b'')
        fin.seek(0)
        read_fd, write_fd = os.pipe()
        try:
            cmd = runner.qemu_command(
                exe, QEMU_COUNT_ARGS + ('-D', f'/dev/fd/{write_fd}'))
            proc = subprocess.Popen(cmd, stdin=None if stdin is None else fin,
                                    stdout=fout, stderr=ferr,
                                    pass_fds=(write_fd,), env={})
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            # Lines differ only in PC and in which host code QEMU generated
            # for it, so tally whole lines and parse each distinct one once.
            lines: Counter[bytes] = Counter()
            with os.fdopen(read_fd, 'rb', buffering=1 << 20) as log:
                lines.update(log)
            exit_code = proc.wait()
        finally:
            timer.cancel()
        fout.seek(0)
        ferr.seek(0)
        stdout, stderr = fout.read(), ferr.read()

    by_pc: Counter[int] = Counter()
    function_at: dict[int, str] = {}
    for line, n in lines.items():
        m = _TRACE_RE.match(line)
        if m:
            pc = int(m.group(1), 16)
            by_pc[pc] += n
            function_at[pc] = m.group(2).decode(errors='replace')
    return Profile(by_pc, function_at, -1 if timed_out.is_set() else exit_code,
                   stdout, stderr)


def program_functions(asm: str) -> frozenset[str]:
    """Names of the functions defined in an llc assembly file."""
    return frozenset(_ASM_FUNC_RE.findall(Path(asm).read_text()))


def mnemonics(exe: str, functions: frozenset[str]) -> dict[int, str]:
    """Map each instruction address in `functions` to its mnemonic.

    Returns {} if llvm-objdump is missing. Disassembling only these functions
    matters: a static binary carries all of libc.
    """
    if not functions:
        return {}
    try:
        objdump = runner.objdump_path()
        proc = subprocess.run([objdump, '-d', '--no-show-raw-insn',
                               '--disassemble-symbols=' + ','.join(sorted(functions)),
                               str(exe)], capture_output=True, text=True)
    except (runner.ToolchainError, OSError):
        return {}
    return {int(pc, 16): op for pc, op in _OBJDUMP_RE.findall(proc.stdout)}


def profile_program(arts: dict[str, str], stdin: str | None = None,
                    timeout: int = 600) -> Profile:
    """Count a program built by runner.compile_program."""
    profile = run_counted(arts['exe'], stdin=stdin, timeout=timeout)
    profile.program_functions = program_functions(arts['asm'])
    profile.mnemonic_at = mnemonics(arts['exe'], profile.program_functions)
    return profile


def report(profile: Profile, top: int = 8) -> str:
    """A short human-readable summary of a Profile."""
    total = profile.total
    prog = profile.program
    pct = (lambda n: f'{100 * n / total:5.1f}%') if total else (lambda n: '')
    lines = [
        'executed RISC-V instructions',
        f'  your program  {prog:>12,}  {pct(prog)}',
        f'  everything else {total - prog:>10,}  {pct(total - prog)}'
        '   (startup, printf, runtime)',
        f'  total         {total:>12,}',
    ]
    functions = profile.by_function().most_common()
    lines += ['', 'your functions']
    lines += [f'  {n:>12,}  {name}' for name, n in functions
              if name in profile.program_functions]
    lines += ['', f'hottest elsewhere (top {top})']
    lines += [f'  {n:>12,}  {name}' for name, n in
              [f for f in functions if f[0] not in profile.program_functions][:top]]
    mix = profile.program_mix()
    if mix and '?' not in mix:
        lines += ['', 'instruction mix in your program']
        lines += [f'  {n:>12,}  {op}' for op, n in mix.most_common(top)]
    return '\n'.join(lines)
