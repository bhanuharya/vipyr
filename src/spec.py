"""
The answer key for a custom instruction, taken from the compiler.

An AI agent may propose a circuit to replace a function, but it never says
what the circuit must compute. That comes from here: the function's own
optimized LLVM IR, translated to a Z3 formula over the instruction's input
registers. src/cage.py then proves a candidate circuit equal to it.

This module is part of the trusted base, so it is kept small: it accepts
only pure, single-block integer functions of one or two arguments, and
rejects anything else rather than guess. Instruction flags that only add
poison (nsw, nuw, exact, disjoint, nneg, samesign) are ignored. That makes
the answer key stricter than the IR, never looser: whatever matches it is
a valid replacement.

The instruction contract, for a function f with argument widths w1, w2 and
result width wr:

    result[31:0] == ext32(f(a[w1-1:0], b[w2-1:0]))   for every a, b

ext32 sign-extends integers and zero-extends bool (i1), matching how the
compiler keeps such values in 32-bit registers.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import z3

from src import runner

REGISTER_BITS = 32

FLAGS = {'nsw', 'nuw', 'exact', 'disjoint', 'nneg', 'samesign'}
BINARY = {
    'add': lambda x, y: x + y,
    'sub': lambda x, y: x - y,
    'mul': lambda x, y: x * y,
    'and': lambda x, y: x & y,
    'or': lambda x, y: x | y,
    'xor': lambda x, y: x ^ y,
    'shl': lambda x, y: x << y,
    'lshr': z3.LShR,
    'ashr': lambda x, y: x >> y,
    'udiv': z3.UDiv,
    'sdiv': lambda x, y: x / y,
    'urem': z3.URem,
    'srem': z3.SRem,
}
ICMP = {
    'eq': lambda x, y: x == y,
    'ne': lambda x, y: x != y,
    'ugt': z3.UGT, 'uge': z3.UGE, 'ult': z3.ULT, 'ule': z3.ULE,
    'sgt': lambda x, y: x > y, 'sge': lambda x, y: x >= y,
    'slt': lambda x, y: x < y, 'sle': lambda x, y: x <= y,
}
MINMAX = {
    'smax': lambda x, y: z3.If(x > y, x, y),
    'smin': lambda x, y: z3.If(x < y, x, y),
    'umax': lambda x, y: z3.If(z3.UGT(x, y), x, y),
    'umin': lambda x, y: z3.If(z3.ULT(x, y), x, y),
}

_DEFINE_RE = re.compile(r'^define\s+.*?\b(i\d+)\s+@"?([^"(\s]+)"?\((.*)\)')
_ASSIGN_RE = re.compile(r'^%("?[^"\s=]+"?)\s*=\s*(.*)$')
_CALL_RE = re.compile(r'^(?:tail |musttail |notail )?call\s+(?:\S+\s+)*?(i\d+)\s+@"?(llvm\.[\w.]+)"?\((.*)\)')


class SpecError(Exception):
    """The function cannot serve as an instruction's answer key."""


def _width(t: str) -> int:
    if not re.fullmatch(r'i\d+', t):
        raise SpecError(f'only integer types are supported, got {t}')
    return int(t[1:])


@dataclass
class Spec:
    """What a custom instruction replacing `function` must compute."""
    function: str
    arg_bits: list[int]
    ret_bits: int
    ir: str
    _body: list[tuple[str, str]]
    _arg_names: list[str]

    def result(self, a: z3.BitVecRef, b: z3.BitVecRef) -> z3.BitVecRef:
        """The required 32-bit result for input registers a and b."""
        ctx = a.ctx
        env = {}
        for name, bits, reg in zip(self._arg_names, self.arg_bits, (a, b)):
            env[name] = z3.Extract(bits - 1, 0, reg)
        ret = None
        for kind, text in self._body:
            if kind == 'ret':
                ret = _operand(text, env, self.ret_bits, ctx)
            else:
                env[kind] = _instruction(text, env, ctx)
        assert ret is not None
        ext = z3.ZeroExt if self.ret_bits == 1 else z3.SignExt
        return ext(REGISTER_BITS - self.ret_bits, ret) if self.ret_bits < REGISTER_BITS else ret

    def evaluate(self, a: int, b: int = 0) -> int:
        """result() on concrete registers, as a signed 32-bit int."""
        ctx = z3.Context()
        r = z3.simplify(self.result(z3.BitVecVal(a, REGISTER_BITS, ctx), z3.BitVecVal(b, REGISTER_BITS, ctx)))
        return r.as_signed_long()


def _operand(text: str, env: dict, bits: int, ctx: z3.Context) -> z3.BitVecRef:
    text = text.strip()
    if text.startswith('%'):
        name = text[1:].strip('"')
        if name not in env:
            raise SpecError(f'use of unknown value %{name}')
        return env[name]
    if text in ('true', 'false'):
        return z3.BitVecVal(1 if text == 'true' else 0, bits, ctx)
    if re.fullmatch(r'-?\d+', text):
        return z3.BitVecVal(int(text), bits, ctx)
    raise SpecError(f'unsupported operand {text!r} (poison and undef are rejected)')


def _split_typed(text: str) -> list[tuple[int, str]]:
    """'i32 %x, i32 -5' -> [(32, '%x'), (32, '-5')]."""
    out = []
    for part in text.split(','):
        tokens = [t for t in part.split() if t not in ('noundef', 'signext', 'zeroext', 'immarg')]
        out.append((_width(tokens[0]), tokens[-1]))
    return out


def _split_pair(text: str) -> tuple[int, str, str]:
    """'i32 %x, -5' (one type for both operands) -> (32, '%x', '-5')."""
    m = re.fullmatch(r'(i\d+)\s+(\S+),\s*(\S+)', text.strip())
    if not m:
        raise SpecError(f'cannot parse operands {text!r}')
    return _width(m.group(1)), m.group(2), m.group(3)


def _instruction(text: str, env: dict, ctx: z3.Context) -> z3.BitVecRef:
    words = text.split()
    op = words[0]
    rest = ' '.join(w for w in words[1:] if w not in FLAGS)
    if op in BINARY:
        bits, x, y = _split_pair(rest)
        return BINARY[op](_operand(x, env, bits, ctx), _operand(y, env, bits, ctx))
    if op == 'icmp':
        pred, typed = rest.split(None, 1)
        bits, x, y = _split_pair(typed)
        cond = ICMP[pred](_operand(x, env, bits, ctx), _operand(y, env, bits, ctx))
        return z3.If(cond, z3.BitVecVal(1, 1, ctx), z3.BitVecVal(0, 1, ctx))
    if op == 'select':
        (_, c), (bits, x), (_, y) = _split_typed(rest)
        return z3.If(_operand(c, env, 1, ctx) == 1, _operand(x, env, bits, ctx), _operand(y, env, bits, ctx))
    if op in ('sext', 'zext', 'trunc'):
        m = re.fullmatch(r'(i\d+)\s+(\S+)\s+to\s+(i\d+)', rest)
        if not m:
            raise SpecError(f'cannot parse {text!r}')
        src_bits, dst_bits = _width(m.group(1)), _width(m.group(3))
        x = _operand(m.group(2), env, src_bits, ctx)
        if op == 'trunc':
            return z3.Extract(dst_bits - 1, 0, x)
        ext = z3.SignExt if op == 'sext' else z3.ZeroExt
        return ext(dst_bits - src_bits, x)
    if op == 'freeze':
        ((bits, x),) = _split_typed(rest)
        return _operand(x, env, bits, ctx)
    m = _CALL_RE.match(text)
    if m:
        bits, callee = _width(m.group(1)), m.group(2)
        args = [_operand(v, env, w, ctx) for w, v in _split_typed(m.group(3))]
        kind = callee.split('.')[1]
        if kind in MINMAX:
            return MINMAX[kind](args[0], args[1])
        if kind == 'abs':
            # The second argument only says whether abs(INT_MIN) is poison.
            return z3.If(args[0] < 0, -args[0], args[0])
        raise SpecError(f'unsupported intrinsic {callee}')
    raise SpecError(f'unsupported instruction {text!r}: the function must be '
                    f'pure integer code in a single block')


def parse_function(ir_text: str, function: str) -> Spec:
    """Build the answer key for `function` from textual LLVM IR."""
    lines = ir_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _DEFINE_RE.match(line)
        if m and m.group(2) == function:
            start = i
            break
    if start is None:
        raise SpecError(f'no definition of {function} in the IR')
    m = _DEFINE_RE.match(lines[start])
    ret_bits = _width(m.group(1))
    params = _split_typed(m.group(3)) if m.group(3).strip() else []
    if not 1 <= len(params) <= 2:
        raise SpecError(f'{function} takes {len(params)} arguments; an instruction has one or two inputs')
    arg_bits = [bits for bits, _ in params]
    arg_names = [name.lstrip('%').strip('"') for _, name in params]
    if max(arg_bits + [ret_bits]) > REGISTER_BITS:
        raise SpecError('arguments and result must fit in a 32-bit register')

    body = []
    fn_lines = []
    for line in lines[start:]:
        fn_lines.append(line)
        stripped = line.split(';')[0].split(', !')[0].strip()
        if stripped == '}':
            break
        if line is lines[start] or not stripped or stripped.endswith(':'):
            if stripped.endswith(':') and stripped != 'entry:':
                raise SpecError(f'{function} has more than one basic block')
            continue
        if stripped.startswith('ret '):
            body.append(('ret', stripped.split(None, 2)[2]))
            continue
        m = _ASSIGN_RE.match(stripped)
        if not m:
            raise SpecError(f'unsupported instruction {stripped!r}: the function '
                            f'must be pure integer code in a single block')
        body.append((m.group(1).strip('"'), m.group(2)))
    if not body or body[-1][0] != 'ret':
        raise SpecError(f'{function} must end in a single return')

    spec = Spec(function, arg_bits, ret_bits, '\n'.join(fn_lines), body, arg_names)
    # Build it once so unsupported instructions fail here, not mid-proof.
    ctx = z3.Context()
    spec.result(z3.BitVec('a', REGISTER_BITS, ctx), z3.BitVec('b', REGISTER_BITS, ctx))
    return spec


# Canonicalize before reading the function: SROA first turns locals into
# values, so simplifycfg can then fold an if/return into a select. Any
# correct optimization preserves the function's meaning, so the pass order
# only changes which functions fit in one block.
SPEC_PASSES = 'sroa,instcombine,simplifycfg,instcombine'


def from_program(src: str, function: str, out_dir: str,
                 passes: str = SPEC_PASSES) -> Spec:
    """Compile a vipyr program and take the answer key for one of its functions."""
    arts = runner.compile_program(src, out_dir, passes=passes)
    proc = subprocess.run([runner.opt_path(), arts['opt_bc'], '-passes=verify', '-S', '-o', '-'],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SpecError(f'opt could not print the IR:\n{proc.stderr}')
    return parse_function(proc.stdout, f'{Path(src).stem}.{function}')
