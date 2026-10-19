"""
Reference evaluator (oracle) for the vipyr language subset.

This interpreter walks the type-checked AST (the same CheckedNode tree the
LLVM backend consumes) and executes it using pure Python values.  It is
independent of the LLVM lowering and is used as the correctness oracle in
the test suite:

    source program
      ├── reference evaluator → expected output/result
      └── LLVM backend → RISC-V binary → QEMU → actual output/result

Semantics implemented here MUST match the language specification in
docs/language-spec.md and the LLVM backend in src/llvm.py:

- int is a 32-bit two's-complement value; arithmetic wraps (LLVM i32).
- `/` between ints is truncating integer division (sdiv semantics);
  `%` between ints is the signed remainder (srem semantics). Division by
  zero is defined with the RISC-V M-extension results (x / 0 == -1,
  x % 0 == x).
- float is IEEE-754 double precision; `%` between floats is C fmod.
- `//`, `**`, `&`, `|`, `^`, `<<`, `>>` and `~` are int-only and total
  (spec §2.1); shift counts use only their low 5 bits.
- print formatting matches the C printf conversions used by the backend:
  int → %d, float → %f (6 decimals), bool → %d (0/1), str → %s, char → %c.
- `and` / `or` short-circuit and require bool operands.
- scalars are passed by value; lists and strings are passed by reference.
- `for` iterates over a list/string/range; `break` skips the `else` clause.
- assignment rebinds the innermost existing binding, otherwise declares a
  new binding in the current scope (matching the type checker).
- `input(prompt)` writes the prompt and reads one line from stdin.

The evaluator does NOT re-run type checking: it is only ever invoked on
trees that the TypeChecker has already accepted, so it can assume the tree
is well-typed.
"""

from __future__ import annotations

import ast
import io
import math
from typing import List, Optional

from .checked_nodes import (
    CheckedNode,
    CheckedModuleNode,
    CheckedFnDefNode,
    CheckedCallNode,
    CheckedBinNode,
    CheckedUnaryOpNode,
    CheckedAssignNode,
    CheckedReturnNode,
    CheckedIfNode,
    CheckedForNode,
    CheckedListNode,
    CheckedBoolOpNode,
    CheckedSubscriptNode,
)
from .type import Class


class InterpretError(Exception):
    """Runtime error in the reference evaluator."""


class _Return(Exception):
    def __init__(self, value):
        super().__init__(f'return {value!r}')
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


def wrap_i32(value: int) -> int:
    """Wrap a Python int into a signed 32-bit value (LLVM i32 semantics)."""
    v = value & 0xFFFFFFFF
    return v - (1 << 32) if v >= (1 << 31) else v


def _is_int(value) -> bool:
    # bool is a subclass of int in Python; treat it separately.
    return isinstance(value, int) and not isinstance(value, bool)


def format_float(value: float) -> str:
    """Format a float like printf("%f") (6 digits after the decimal point)."""
    if math.isnan(value):
        return 'nan'
    if math.isinf(value):
        return 'inf' if value > 0 else '-inf'
    return f'{value:.6f}'


class ReferenceEvaluator:
    def __init__(self, stdin: Optional[io.TextIOBase] = None):
        # A chain of scopes. Each scope maps a name to either a Python value
        # or a CheckedFnDefNode (functions live in the same namespace).
        self.scopes: List[dict] = [dict()]
        self.output = io.StringIO()
        self.stdin = stdin

    # ------------------------------------------------------------- scopes

    def push_scope(self):
        self.scopes.append(dict())

    def pop_scope(self):
        self.scopes.pop()

    def lookup(self, name: str):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise InterpretError(f'Undefined identifier {name}')

    def _assign_name(self, name: str, value):
        """Rebind the innermost existing binding, else declare in current scope."""
        for scope in reversed(self.scopes):
            if name in scope:
                scope[name] = value
                return
        self.scopes[-1][name] = value

    # ------------------------------------------------------------- entry

    def run(self, module: CheckedModuleNode) -> tuple[str, int]:
        """
        Execute a module. Returns (stdout text, exit status).

        The status is always 0: the compiled program falls through to
        `main` returning 0.
        """
        try:
            self._exec_statements(module.statements)
        except _Return:
            # A bare `return` at module scope ends execution of the module.
            pass
        return self.output.getvalue(), 0

    # --------------------------------------------------------- statements

    def _exec_statements(self, stmts: List[CheckedNode]):
        for stmt in stmts:
            self._exec_stmt(stmt)

    def _exec_stmt(self, stmt: CheckedNode):
        base = stmt.base
        if isinstance(base, ast.FunctionDef):
            assert isinstance(stmt, CheckedFnDefNode)
            # Functions are registered in the enclosing scope so they are
            # visible to later (and recursive) calls.
            self.scopes[-1][base.name] = stmt
            return
        if isinstance(base, ast.Assign):
            assert isinstance(stmt, CheckedAssignNode)
            self._exec_assign(stmt)
            return
        if isinstance(base, ast.Return):
            assert isinstance(stmt, CheckedReturnNode)
            value = self._eval_expr(stmt.expr) if stmt.expr is not None else None
            raise _Return(value)
        if isinstance(base, ast.If):
            assert isinstance(stmt, CheckedIfNode)
            self._exec_if(stmt)
            return
        if isinstance(base, ast.While):
            assert isinstance(stmt, CheckedIfNode)
            self._exec_while(stmt)
            return
        if isinstance(base, ast.For):
            assert isinstance(stmt, CheckedForNode)
            self._exec_for(stmt)
            return
        if isinstance(base, ast.Break):
            raise _Break()
        if isinstance(base, ast.Continue):
            raise _Continue()
        if isinstance(base, ast.Pass):
            return
        # Expression statement (the type checker unwraps ast.Expr).
        self._eval_expr(stmt)

    def _exec_assign(self, stmt: CheckedAssignNode):
        value = self._eval_expr(stmt.right)
        target = stmt.left
        base = target.base
        if isinstance(base, ast.Name):
            # The type checker records which names are *first* declarations in
            # this scope. Those must bind in the current (innermost) frame,
            # so that recursive activations get their own copy. Otherwise the
            # name already exists somewhere and we rebind the innermost
            # existing binding (matching the compiler's findSymbol).
            if base.id in stmt.declaredSymbols:
                self.scopes[-1][base.id] = value
            else:
                self._assign_name(base.id, value)
            return
        if isinstance(base, ast.Subscript):
            assert isinstance(target, CheckedSubscriptNode)
            lst = self._eval_expr(target.op)
            idx = self._eval_expr(target.subscript)
            self._check_index(idx, len(lst))
            lst[idx] = value
            return
        raise InterpretError(f'Unsupported assignment target: {ast.dump(base)}')

    def _exec_if(self, stmt: CheckedIfNode):
        cond = self._eval_expr(stmt.expr)
        if not isinstance(cond, bool):
            raise InterpretError(f'Non-boolean condition: {cond!r}')
        self.push_scope()
        try:
            if cond:
                self._exec_statements(stmt.iftrue)
            else:
                self._exec_statements(stmt.orelse)
        finally:
            self.pop_scope()

    def _exec_while(self, stmt: CheckedIfNode):
        while True:
            cond = self._eval_expr(stmt.expr)
            if not isinstance(cond, bool):
                raise InterpretError(f'Non-boolean condition: {cond!r}')
            if not cond:
                break
            self.push_scope()
            try:
                self._exec_statements(stmt.iftrue)
            except _Continue:
                pass
            except _Break:
                # break jumps past the else clause.
                return
            finally:
                self.pop_scope()
        # Loop finished normally: run the else clause.
        self.push_scope()
        try:
            self._exec_statements(stmt.orelse)
        finally:
            self.pop_scope()

    def _exec_for(self, stmt: CheckedForNode):
        seq = self._eval_expr(stmt.iter)
        (var_name, _var_t) = stmt.vars[0]
        for item in seq:
            self.push_scope()
            self.scopes[-1][var_name] = item
            try:
                self._exec_statements(stmt.body)
            except _Continue:
                pass
            except _Break:
                # break jumps past the else clause.
                return
            finally:
                self.pop_scope()
        # Sequence exhausted: run the else clause.
        self.push_scope()
        try:
            self._exec_statements(stmt.orelse)
        finally:
            self.pop_scope()

    # -------------------------------------------------------- expressions

    def _eval_expr(self, expr: Optional[CheckedNode]):
        if expr is None:
            return None
        base = expr.base
        if isinstance(base, ast.Constant):
            return base.value
        if isinstance(base, ast.Name):
            return self.lookup(base.id)
        if isinstance(base, ast.List):
            assert isinstance(expr, CheckedListNode)
            return [self._eval_expr(v) for v in expr.vals]
        if isinstance(base, ast.BinOp):
            assert isinstance(expr, CheckedBinNode)
            return self._eval_binop(expr)
        if isinstance(base, ast.AugAssign):
            assert isinstance(expr, CheckedBinNode)
            return self._eval_augassign(expr)
        if isinstance(base, ast.UnaryOp):
            assert isinstance(expr, CheckedUnaryOpNode)
            return self._eval_unaryop(expr)
        if isinstance(base, ast.Compare):
            assert isinstance(expr, CheckedBinNode)
            return self._eval_compare(expr)
        if isinstance(base, ast.BoolOp):
            assert isinstance(expr, CheckedBoolOpNode)
            return self._eval_boolop(expr)
        if isinstance(base, ast.Call):
            assert isinstance(expr, CheckedCallNode)
            return self._eval_call(expr)
        if isinstance(base, ast.Subscript):
            assert isinstance(expr, CheckedSubscriptNode)
            lst = self._eval_expr(expr.op)
            idx = self._eval_expr(expr.subscript)
            self._check_index(idx, len(lst))
            return lst[idx]
        raise InterpretError(f'Unsupported expression: {ast.dump(base)}')

    def _check_index(self, idx: int, count: int):
        """
        Reject out-of-range subscripts the way the runtime bounds check does
        (negative indices are not supported by the language).
        """
        if idx < 0 or idx >= count:
            raise InterpretError(
                f'IndexError: index {idx} out of range for size {count}')

    def _eval_binop(self, node: CheckedBinNode):
        left = self._eval_expr(node.left)
        right = self._eval_expr(node.right)
        return self._apply_binop(left, right, node.op)

    @staticmethod
    def _div(a, b):
        if _is_int(a) and _is_int(b):
            # C sdiv semantics: truncation toward zero. A zero divisor gives
            # the RISC-V M-extension result (-1); INT_MIN / -1 wraps to
            # INT_MIN in the caller.
            if b == 0:
                return -1
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            return q
        if b == 0.0:
            # IEEE-754: x / ±0 is ±inf (sign of a XOR sign of b), 0 / 0 is nan.
            if a == 0.0 or math.isnan(a):
                return math.nan
            return math.copysign(math.inf, math.copysign(1.0, a) * math.copysign(1.0, b))
        return a / b

    @staticmethod
    def _mod(a, b):
        if _is_int(a) and _is_int(b):
            # C srem semantics: remainder has the sign of the dividend. A zero
            # divisor gives the RISC-V M-extension result (the dividend).
            if b == 0:
                return a
            r = abs(a) % abs(b)
            if a < 0:
                r = -r
            return r
        # LLVM frem is C fmod (sign of the dividend), not CPython's floor
        # modulo. math.fmod raises where C returns nan.
        if b == 0.0 or math.isinf(a) or math.isnan(a) or math.isnan(b):
            return math.nan
        return math.fmod(a, b)

    def _eval_augassign(self, node: CheckedBinNode):
        # Evaluate the lvalue once: the index expression may have side
        # effects (e.g. `a[f()] += x`), and re-evaluating it both for
        # the read and for the store would call `f` twice and target
        # the wrong slot.
        target = node.left
        base = target.base
        if isinstance(base, ast.Name):
            old = self._eval_expr(target)
            rhs = self._eval_expr(node.right)
            res = self._apply_binop(old, rhs, node.op)
            self._assign_name(base.id, res)
            return res
        if isinstance(base, ast.Subscript):
            assert isinstance(target, CheckedSubscriptNode)
            lst = self._eval_expr(target.op)
            idx = self._eval_expr(target.subscript)
            self._check_index(idx, len(lst))
            old = lst[idx]
            rhs = self._eval_expr(node.right)
            res = self._apply_binop(old, rhs, node.op)
            lst[idx] = res
            return res
        raise InterpretError(f'Unsupported augmented assignment target: {ast.dump(base)}')

    @staticmethod
    def _apply_binop(left, right, op):
        # Shared by plain and augmented binops so both wrap to i32.
        if isinstance(op, ast.Add):
            res = left + right
        elif isinstance(op, ast.Sub):
            res = left - right
        elif isinstance(op, ast.Mult):
            res = left * right
        elif isinstance(op, ast.Div):
            res = ReferenceEvaluator._div(left, right)
        elif isinstance(op, ast.Mod):
            res = ReferenceEvaluator._mod(left, right)
        elif isinstance(op, ast.FloorDiv):
            # x // 0 == -1, as for `/`.
            res = -1 if right == 0 else left // right
        elif isinstance(op, ast.Pow):
            res = ReferenceEvaluator._ipow(left, right)
        elif isinstance(op, ast.BitAnd):
            res = left & right
        elif isinstance(op, ast.BitOr):
            res = left | right
        elif isinstance(op, ast.BitXor):
            res = left ^ right
        elif isinstance(op, ast.LShift):
            # Only the low 5 bits of the count are used (RISC-V sll/sra).
            res = left << (right & 31)
        elif isinstance(op, ast.RShift):
            res = left >> (right & 31)
        else:
            raise InterpretError(f'Unsupported binary operator: {type(op).__name__}')
        return wrap_i32(res) if _is_int(res) else res

    @staticmethod
    def _ipow(base, exp):
        # Wraps modulo 2^32 (via the caller). A negative exponent gives the
        # truncated value of 1 / base**-exp, with 1 / 0 == -1 as for `/`.
        if exp < 0:
            if base == 1:
                return 1
            if base == -1:
                return -1 if exp & 1 else 1
            return -1 if base == 0 else 0
        return pow(base, exp, 1 << 32)

    def _eval_unaryop(self, node: CheckedUnaryOpNode):
        value = self._eval_expr(node.operand)
        op = node.op
        if isinstance(op, ast.USub):
            return wrap_i32(-value) if _is_int(value) else -value
        if isinstance(op, ast.UAdd):
            return value
        if isinstance(op, ast.Not):
            return not value
        if isinstance(op, ast.Invert):
            return ~value
        raise InterpretError(f'Unsupported unary operator: {type(op).__name__}')

    def _eval_compare(self, node: CheckedBinNode):
        left = self._eval_expr(node.left)
        right = self._eval_expr(node.right)
        op = node.op
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        raise InterpretError(f'Unsupported comparison: {type(op).__name__}')

    def _eval_boolop(self, node: CheckedBoolOpNode):
        if node.op == 'and':
            for v in node.vals:
                if not self._eval_expr(v):
                    return False
            return True
        if node.op == 'or':
            for v in node.vals:
                if self._eval_expr(v):
                    return True
            return False
        raise InterpretError(f'Unsupported boolean operator: {node.op}')

    # ------------------------------------------------------------ calls

    def _eval_call(self, node: CheckedCallNode):
        name = node.fnName
        if name == 'print':
            args = [self._eval_expr(a) for a in node.args]
            line = ' '.join(self._format_arg(v, a.t) for v, a in zip(args, node.args))
            self.output.write(line + '\n')
            return None
        if name == 'len':
            assert len(node.args) == 1
            return wrap_i32(len(self._eval_expr(node.args[0])))
        if name == 'range':
            assert len(node.args) == 1
            n = self._eval_expr(node.args[0])
            return list(range(n))
        if name == 'int':
            assert len(node.args) == 1
            return wrap_i32(int(self._eval_expr(node.args[0])))
        if name == 'float':
            assert len(node.args) == 1
            return float(self._eval_expr(node.args[0]))
        if name == 'input':
            assert len(node.args) == 1
            prompt = self._eval_expr(node.args[0])
            self.output.write(prompt)
            if self.stdin is None:
                raise InterpretError('input() called but no stdin stream provided')
            line = self.stdin.readline()
            if line.endswith('\n'):
                line = line[:-1]
            return line
        if name == 'str':
            assert len(node.args) == 1
            val = self._eval_expr(node.args[0])
            if isinstance(val, bool):
                return '1' if val else '0'
            if _is_int(val):
                return str(val)
            if isinstance(val, float):
                return format_float(val)
            raise InterpretError(f'str() argument must be int or float, got {type(val).__name__}')
        if name == 'bool':
            assert len(node.args) == 1
            val = self._eval_expr(node.args[0])
            return val != 0
        if name == 'abs':
            assert len(node.args) == 1
            val = self._eval_expr(node.args[0])
            if _is_int(val):
                return wrap_i32(abs(val))
            return abs(val)
        if name == 'min':
            assert len(node.args) == 2
            a = self._eval_expr(node.args[0])
            b = self._eval_expr(node.args[1])
            return a if a <= b else b
        if name == 'max':
            assert len(node.args) == 2
            a = self._eval_expr(node.args[0])
            b = self._eval_expr(node.args[1])
            return a if a >= b else b
        if name is None:
            # User-defined function call.
            operand = node.operand.base
            assert isinstance(operand, ast.Name)
            fn = self.lookup(operand.id)
            assert isinstance(fn, CheckedFnDefNode)
            if len(fn.args) != len(node.args):
                raise InterpretError(
                    f'Function {operand.id} expects {len(fn.args)} arguments, '
                    f'got {len(node.args)}')
            self.push_scope()
            try:
                for (arg_name, _t), arg_node in zip(fn.args, node.args):
                    self.scopes[-1][arg_name] = self._eval_expr(arg_node)
                try:
                    self._exec_statements(fn.statements)
                except _Return as r:
                    return r.value
                return None
            finally:
                self.pop_scope()
        raise InterpretError(f'Unknown intrinsic: {name}')

    # ---------------------------------------------------------- printing

    def _format_arg(self, value, t: Class) -> str:
        """Format a single print argument the way the backend's printf would."""
        if isinstance(value, bool):
            return '1' if value else '0'
        if _is_int(value):
            return str(value)
        if isinstance(value, float):
            return format_float(value)
        if isinstance(value, str):
            # The backend prints str with %s and char with %c; a Python str
            # value already represents the printed bytes either way.
            return value
        raise InterpretError(f'Cannot format value {value!r} for printing')
