"""
The cage: decide whether an AI-proposed circuit may become a custom instruction.

The circuit comes from an untrusted source, such as an AI agent that may have
been tricked by a prompt injection. It is accepted only if it passes three
mechanical checks, none of which reads anything the agent wrote except the
Verilog itself:

    interface    exactly two 32-bit inputs, a and b, and one 32-bit output,
                 result: two numbers in, one number out, no other wires
    no memory    only combinational cells and no logic loops: no flip-flops,
                 latches, memories or debug outputs, and no gates wired into
                 a latch, so it cannot remember earlier inputs or count its
                 way to a time bomb
    equivalence  Z3 proves result equal to the answer key (src/spec.py), taken
                 from the compiler, for every one of the 2^64 input pairs

Yosys turns the Verilog into a netlist for the first two checks. Only a
circuit that passes both is turned into an SMT formula for the third. Undriven wires and undefined (x) bits become
free variables, so a circuit cannot pass by leaving its output unspecified.

Command line:

    python -m src.cage examples/cage/pid.py q15mul examples/cage/candidates/*.v
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import z3

from src import spec as specs

TOP = 'accel'
PORTS = {'a': ('input', 32), 'b': ('input', 32), 'result': ('output', 32)}
PROOF_TIMEOUT_MS = 120_000

# Combinational Yosys cells after `proc; flatten`. Anything else, including
# every flip-flop, latch, memory, $print and formal cell, is rejected. So are
# division and $shiftx: Verilog leaves division by zero and out-of-range
# shifts undefined, and the proof's model of them might not match the
# hardware that gets built.
COMBINATIONAL_CELLS = {
    '$not', '$pos', '$neg', '$and', '$or', '$xor', '$xnor',
    '$reduce_and', '$reduce_or', '$reduce_xor', '$reduce_xnor', '$reduce_bool',
    '$shl', '$shr', '$sshl', '$sshr', '$shift',
    '$lt', '$le', '$eq', '$ne', '$eqx', '$nex', '$ge', '$gt',
    '$add', '$sub', '$mul',
    '$logic_not', '$logic_and', '$logic_or',
    '$mux', '$pmux', '$bmux', '$bwmux', '$demux',
    '$scopeinfo',
}


@dataclass
class Check:
    rule: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    candidate: str
    checks: list[Check] = field(default_factory=list)
    # On a failed proof: input registers, required result, circuit's result.
    counterexample: dict[str, int] | None = None

    @property
    def accepted(self) -> bool:
        return len(self.checks) == 3 and all(c.passed for c in self.checks)


def yosys_path() -> str:
    for name in ('yosys', 'yowasp-yosys'):
        found = shutil.which(name) or shutil.which(name, path=str(Path(sys.executable).parent))
        if found:
            return found
    raise FileNotFoundError('Yosys not found: pip install yowasp-yosys')


def _yosys(workdir: Path, commands: list[str]) -> str:
    """Run Yosys quietly; return its warnings. Raises ValueError on an error."""
    proc = subprocess.run([yosys_path(), '-q', '-p', '; '.join(commands)], cwd=workdir,
                          capture_output=True, text=True)
    output = (proc.stderr + proc.stdout).strip()
    if proc.returncode != 0:
        raise ValueError(output.splitlines()[-1])
    return output


def structural_problems(workdir: Path) -> list[str]:
    """Yosys `check` problems in the netlist in `workdir`, one line each.

    The one that matters most is a logic loop: two gates feeding each other
    can store a bit, which makes memory out of cells that are each
    combinational.
    """
    proc = subprocess.run([yosys_path(), '-q', '-p', 'read_json netlist.json; check -assert'],
                          cwd=workdir, capture_output=True, text=True)
    if proc.returncode == 0:
        return []
    found = [line.removeprefix('Warning: ').rstrip(':')
             for line in (proc.stderr + proc.stdout).splitlines() if line.startswith('Warning: ')]
    return found or [(proc.stderr + proc.stdout).strip().splitlines()[-1]]


def read_netlist(verilog: str, workdir: Path, top: str = TOP) -> dict:
    """Elaborate the candidate with Yosys and return its JSON netlist.

    The candidate is copied into an empty `workdir` and Yosys runs there with
    relative paths only. The YoWASP build of Yosys is sandboxed to its working
    directory, so an `include` or `$readmemh` in the candidate cannot read
    other files. Raises ValueError on Verilog Yosys rejects.
    """
    shutil.copyfile(verilog, workdir / 'candidate.v')
    _yosys(workdir, [
        'read_verilog -sv candidate.v',
        f'hierarchy -check -top {top}',
        'proc', 'flatten', 'opt_clean',
        'write_json netlist.json',
    ])
    return json.loads((workdir / 'netlist.json').read_text())['modules'][top]


def write_formula(workdir: Path, top: str = TOP) -> str:
    """The candidate in `workdir` as SMT-LIB2, for a circuit already checked."""
    _yosys(workdir, [
        'read_json netlist.json',
        # Model unknown bits as anything at all, so they cannot hide a fault.
        'setundef -undriven -anyseq', 'setundef -anyseq',
        'write_smt2 circuit.smt2',
    ])
    return (workdir / 'circuit.smt2').read_text()


def check_interface(module: dict) -> Check:
    ports = {name: (p['direction'], len(p['bits'])) for name, p in module['ports'].items()}
    if ports == PORTS:
        return Check('interface', True, 'inputs a, b and output result, 32 bits each')
    problems = []
    for name, (direction, bits) in sorted(ports.items()):
        want = PORTS.get(name)
        if want is None:
            problems.append(f'extra {direction} port {name!r}')
        elif want != (direction, bits):
            problems.append(f'port {name!r} is a {bits}-bit {direction}, expected {want[1]}-bit {want[0]}')
    problems += [f'missing port {name!r}' for name in PORTS if name not in ports]
    return Check('interface', False, '; '.join(problems))


def check_stateless(module: dict, problems: list[str]) -> Check:
    bad: dict[str, int] = {}
    for cell in module['cells'].values():
        if cell['type'] not in COMBINATIONAL_CELLS:
            bad[cell['type']] = bad.get(cell['type'], 0) + 1
    if bad:
        found = ', '.join(f'{n} x {t}' for t, n in sorted(bad.items()))
        return Check('no memory', False, f'cells outside the combinational allowlist: {found}')
    if problems:
        return Check('no memory', False, '; '.join(problems))
    return Check('no memory', True, 'combinational logic only, no loops')


def check_equivalence(smt2: str, spec: specs.Spec, top: str = TOP,
                      timeout_ms: int = PROOF_TIMEOUT_MS) -> tuple[Check, dict | None]:
    # A fresh context per proof keeps proofs independent: Z3's search
    # heuristics depend on what else exists in its context, so a shared one
    # makes proof times depend on which candidates were checked before.
    ctx = z3.Context()
    a, b, r = (z3.BitVec(n, 32, ctx) for n in ('cage_a', 'cage_b', 'cage_r'))
    glue = f'''
(declare-const cage_state |{top}_s|)
(assert (|{top}_h| cage_state))
(assert (= cage_a (|{top}_n a| cage_state)))
(assert (= cage_b (|{top}_n b| cage_state)))
(assert (= cage_r (|{top}_n result| cage_state)))
'''
    solver = z3.Solver(ctx=ctx)
    solver.set('timeout', timeout_ms)
    solver.add(z3.parse_smt2_string(smt2 + glue, decls={'cage_a': a, 'cage_b': b, 'cage_r': r}, ctx=ctx))
    required = spec.result(a, b)
    solver.add(r != required)
    outcome = solver.check()
    if outcome == z3.unsat:
        return Check('equivalence', True, 'equal to the answer key for all 2^64 input pairs'), None
    if outcome == z3.unknown:
        return Check('equivalence', False, f'no proof within {timeout_ms // 1000} s ({solver.reason_unknown()})'), None
    model = solver.model()
    value = lambda e: model.eval(e, model_completion=True).as_long()
    cex = {'a': value(a), 'b': value(b), 'required': value(required), 'got': value(r)}
    return Check('equivalence', False,
                 f'differs at a=0x{cex["a"]:08x}, b=0x{cex["b"]:08x}: '
                 f'required 0x{cex["required"]:08x}, got 0x{cex["got"]:08x}'), cex


def judge(verilog: str, spec: specs.Spec, top: str = TOP) -> Verdict:
    """Run every check on one candidate circuit."""
    verdict = Verdict(str(verilog))
    with tempfile.TemporaryDirectory(prefix='cage-') as tmp:
        try:
            module = read_netlist(verilog, Path(tmp), top)
        except ValueError as e:
            verdict.checks.append(Check('interface', False, f'Yosys rejected the Verilog: {e}'))
            return verdict
        verdict.checks.append(check_interface(module))
        verdict.checks.append(check_stateless(module, structural_problems(Path(tmp))))
        if all(c.passed for c in verdict.checks):
            smt2 = write_formula(Path(tmp), top)
            check, verdict.counterexample = check_equivalence(smt2, spec, top)
        else:
            check = Check('equivalence', False, 'not attempted: the circuit is outside the cage')
        verdict.checks.append(check)
    return verdict


def report(verdict: Verdict) -> str:
    lines = [f'{Path(verdict.candidate).name}: {"ACCEPTED" if verdict.accepted else "REJECTED"}']
    for c in verdict.checks:
        lines.append(f'  {"pass" if c.passed else "FAIL"}  {c.rule:<12} {c.detail}')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m src.cage',
        description='Check AI-proposed circuits against the compiler\'s answer key.')
    parser.add_argument('program', help='vipyr program that defines the function')
    parser.add_argument('function', help='function the custom instruction replaces')
    parser.add_argument('candidates', nargs='+', help='candidate Verilog files')
    parser.add_argument('--top', default=TOP, help=f'top module name (default: {TOP})')
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix='cage-spec-') as tmp:
        spec = specs.from_program(args.program, args.function, tmp)
    widths = ', '.join(f'i{w}' for w in spec.arg_bits)
    print(f'answer key: {spec.function}({widths}) -> i{spec.ret_bits}, from the compiler\'s IR\n')
    accepted = 0
    for candidate in args.candidates:
        verdict = judge(candidate, spec, args.top)
        accepted += verdict.accepted
        print(report(verdict) + '\n')
    print(f'{accepted} of {len(args.candidates)} accepted')
    return 0


if __name__ == '__main__':
    sys.exit(main())
