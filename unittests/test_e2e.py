"""End-to-end regression tests: oracle vs QEMU, opt vs no-opt.

These tests require the external RISC-V toolchain and QEMU. They are skipped
automatically when the toolchain is unavailable.
"""

import unittest

from unittests.helpers import (
    oracle_output,
    qemu_output,
    toolchain_available,
    DEFAULT_PASSES,
)

BENCHMARKS = ['variables', 'standard', 'medium', 'complex']


def benchmark_source(name: str) -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / 'tests' / f'{name}.py').read_text()


@unittest.skipUnless(toolchain_available(), 'RISC-V toolchain not available')
class TestOracleVsQemu(unittest.TestCase):
    maxDiff = None

    def check_equivalence(self, src: str, passes=None):
        expected, exp_status = oracle_output(src)
        code, actual = qemu_output(src, passes=passes)
        self.assertEqual(exp_status, 0, 'oracle reported non-zero status')
        self.assertEqual(code, 0, f'QEMU exited {code}')
        self.assertEqual(actual, expected.encode())

    def test_benchmarks_optimized(self):
        for name in BENCHMARKS:
            with self.subTest(name=name):
                self.check_equivalence(benchmark_source(name),
                                       passes=DEFAULT_PASSES)

    def test_benchmarks_unoptimized(self):
        for name in BENCHMARKS:
            with self.subTest(name=name):
                self.check_equivalence(benchmark_source(name), passes=None)

    def test_scalar_edge_cases(self):
        src = '''
a = -7
b = 3
print(a / b)
print(a % b)
x = 5
if x > 0 and x < 10:
    print("and")
if x < 0 or x > 2:
    print("or")
print(not False)
print(True, False)
'''
        self.check_equivalence(src)

    def test_sroa_pointer_layout_regression(self):
        # Regression: SROA used to mis-scalarize the {i32, i8*} slice
        # aggregate because the data layout lacked p:32:32. This exercises
        # list construction + iteration through the sroa pass.
        src = '''
items = ["foo", "bar", "baz"]
for it in items:
    print(it)
print(len(items))
'''
        self.check_equivalence(src, passes='sroa')

    def test_void_main_exit_status_regression(self):
        # Regression: the entry point must be i32 @main returning 0, so the
        # process exit status is 0 (not garbage from a void main).
        src = 'a = 1\nb = 2\nprint(a + b)\n'
        self.check_equivalence(src)

    def test_recursion_and_calls(self):
        src = '''
def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

def add(a: int, b: int) -> int:
    return a + b

print(fib(10))
print(add(3, 4))
'''
        self.check_equivalence(src)

    def test_global_int_read_from_function(self):
        # A function body must be able to read a module-scope int.
        src = '''
c = 10
def get() -> int:
    return c
print(c)
print(get())
'''
        self.check_equivalence(src)

    def test_global_int_write_from_function(self):
        # A function body must be able to mutate a module-scope int, and
        # the mutation must be visible to subsequent reads (including
        # in main and other functions).
        src = '''
counter = 0
def bump() -> int:
    counter = counter + 1
    return counter

print(counter)
print(bump())
print(bump())
print(counter)
'''
        self.check_equivalence(src)

    def test_global_list_shared_with_function(self):
        # Lists are reference types; a function mutating a global list
        # must be visible to the caller and to other functions.
        src = '''
items = [1, 2, 3]
def push(x: int) -> None:
    items[0] = x
def first() -> int:
    return items[0]
print(items[0], items[1], items[2])
push(99)
print(items[0], items[1], items[2])
print(first())
'''
        self.check_equivalence(src)

    def test_global_string_read_from_function(self):
        src = '''
greeting = "hello"
def greet() -> str:
    return greeting + "!"
print(greeting)
print(greet())
'''
        self.check_equivalence(src)


    def test_subscript_assignment(self):
        # Plain element assignment a[i] = x (the feature this commit adds).
        src = 'a = [1, 2, 3]\na[0] = 5\nprint(a[0])\n'
        self.check_equivalence(src)

    def test_subscript_assign_through_alias(self):
        # Lists are reference types: writing through one alias is visible
        # through another. This must hold in the backend exactly as in
        # the oracle.
        src = 'a = [1, 2, 3]\nb = a\nb[0] = 99\nprint(a[0], a[1], b[2])\n'
        self.check_equivalence(src)

    def test_nested_subscript_assignment(self):
        src = 'm = [[1, 2], [3, 4]]\nm[0][1] = 10\nprint(m[0][0], m[0][1], m[1][1])\n'
        self.check_equivalence(src)

    def test_subscript_assign_in_loop(self):
        src = 'a = [0, 0, 0]\nfor i in range(3):\n    a[i] = i * 10\nprint(a[0], a[1], a[2])\n'
        self.check_equivalence(src)

    def test_aug_assign_scalar(self):
        # Pure-int LHS: regression coverage for `+= -= *= /= %=` on a
        # plain variable.
        src = '''
x = 10
x += 3
y = x
y -= 4
z = 2
z *= 5
w = 20
w /= 4
v = 17
v %= 5
print(x, y, z, w, v)
'''
        self.check_equivalence(src)

    def test_int_division_is_total(self):
        # LLVM sdiv/srem are UB for these inputs; the binary must produce
        # the RISC-V results the oracle models. Function arguments keep the
        # divisors away from constant folding.
        src = '''
def div(a: int, b: int) -> int:
    return a / b

def rem(a: int, b: int) -> int:
    return a % b

m = -2147483647 - 1
print(div(7, 0), rem(7, 0))
print(div(-7, 0), rem(-7, 0))
print(div(m, -1), rem(m, -1))
print(div(-7, 2), rem(-7, 2))
x = 9
x /= 0
y = 9
y %= 0
print(x, y)
'''
        self.check_equivalence(src)
        self.check_equivalence(src, passes=DEFAULT_PASSES)

    def test_aug_assign_wraps(self):
        src = '''
x = 2147483647
x += 1
a = [2147483647]
a[0] *= 2
print(x, a[0])
'''
        self.check_equivalence(src)

    def test_float_division_and_mod(self):
        src = '''
def fdivide(a: float, b: float) -> float:
    return a / b

def fremainder(a: float, b: float) -> float:
    return a % b

print(fdivide(7.0, 0.0), fdivide(-7.0, 0.0), fdivide(0.0, 0.0))
print(fremainder(-7.0, 3.0), fremainder(7.0, -3.0), fremainder(7.0, 0.0))
'''
        self.check_equivalence(src)

    def test_int_only_operators(self):
        # Function arguments keep every operand away from constant folding,
        # so the RISC-V code itself computes each edge case.
        src = '''
def fdiv(a: int, b: int) -> int:
    return a // b

def power(a: int, b: int) -> int:
    return a ** b

def shl(a: int, b: int) -> int:
    return a << b

def shr(a: int, b: int) -> int:
    return a >> b

def bits(a: int, b: int) -> int:
    return (a & b) + (a | b) * 3 + (a ^ b) * 7 + ~a

m = -2147483647 - 1
print(fdiv(-7, 2), fdiv(7, -2), fdiv(7, 2), fdiv(-7, -2), fdiv(7, 0), fdiv(-7, 0), fdiv(m, -1))
print(power(3, 21), power(2, 31), power(2, 32), power(-3, 3), power(0, 0))
print(power(1, -5), power(-1, -3), power(-1, -4), power(0, -1), power(2, -1), power(-1, m))
print(shl(1, 33), shl(1, -1), shl(1, 31), shr(-8, 1), shr(-8, 33), shr(5, 32))
print(bits(-6, 3), bits(12345, -987))
x = 5
x //= 2
x **= 3
x <<= 2
x >>= 1
x &= 63
x |= 64
x ^= 5
print(x)
'''
        self.check_equivalence(src)
        self.check_equivalence(src, passes=DEFAULT_PASSES)

    def test_globals_declared_after_functions(self):
        # Regression: these globals used to become locals of main, so f
        # read other functions' values (or failed to compile).
        src = '''
def first(x: int) -> int:
    return x
K1 = 7
K2: int16 = 300
def f(a: int, b: int16, c: int) -> int:
    return K1 * 100 + a * 10 + int(K2) + int(b) + c
print(f(1, 2, 3))
print(first(K1))
'''
        self.check_equivalence(src)
        self.check_equivalence(src, passes=DEFAULT_PASSES)

    def test_narrow_ints(self):
        # Edge cases go through arguments so constant folding can't hide
        # the generated code.
        src = '''
def edge(m: int8, k: int8, s: int8) -> int:
    print(m / k, m % k, -m, abs(m), m // 0, int8(1) << s, m >> s, m ** 3, str(m), bool(m))
    print(max(m, 0), min(m, k), ~m, m & 15, m ^ k, m | 3, m * k, m + k, m - k)
    return 0

def wide(a: int16, b: int16) -> int16:
    return a * b

def dot(a: list[int8], b: list[int8]) -> int:
    acc = 0
    for i in range(len(a)):
        acc = acc + int(a[i]) * int(b[i])
    return acc

edge(-128, -1, 9)
edge(37, 5, 3)
edge(127, 127, 7)
print(wide(300, 300), wide(-32768, -1), int(wide(181, 181)))
w: list[int8] = [12, -3, 7, -128, 127]
v: list[int8] = [-128, -128, 127, -128, 127]
print(dot(w, v), int8(200), int16(70000), float(w[3]))
'''
        self.check_equivalence(src)
        self.check_equivalence(src, passes=DEFAULT_PASSES)

    def test_continue(self):
        src = '''
def count(n: int) -> int:
    total = 0
    for i in range(n):
        if i % 3 == 0:
            continue
        total += i
    else:
        total += 1000
    j = 0
    while j < n:
        j += 1
        if j % 2 == 0:
            continue
        total += j
    return total

print(count(10))
items = ["a", "skip", "b"]
for it in items:
    if it == "skip":
        continue
    print(it)
'''
        self.check_equivalence(src)
        self.check_equivalence(src, passes=DEFAULT_PASSES)

    def test_aug_assign_subscript_pure_index(self):
        # The "consistently wrong" case: the index has no side effects
        # so both the old and the new lowering produce the same answer.
        # Kept to ensure the refactor doesn't break the simple case.
        src = 'a = [10, 20, 30]\na[1] += 5\nprint(a[0], a[1], a[2])\n'
        self.check_equivalence(src)

    def test_aug_assign_subscript_side_effect(self):
        # The real bug: `a[f()] += x` must call `f` exactly once and
        # store at the index returned. With the old lowering `f` was
        # called twice and the second call's index won, writing the
        # result to the wrong slot.
        src = '''
ticks = 0
def tick() -> int:
    ticks = ticks + 1
    return ticks - 1
a = [10, 20, 30]
a[tick()] += 5
print(a[0], a[1], a[2], ticks)
'''
        self.check_equivalence(src)

    def test_aug_assign_subscript_global_index(self):
        # Side-effecting index that reads a global (so the read inside
        # the function must see the live value, not a stale snapshot).
        src = '''
idx = 0
def next_idx() -> int:
    idx = idx + 1
    return idx - 1
a = [100, 200, 300]
a[next_idx()] += 1
a[next_idx()] += 10
print(a[0], a[1], a[2], idx)
'''
        self.check_equivalence(src)

    def test_aug_assign_in_loop(self):
        # Accumulators: `s += a[i]` over a list.
        src = '''
a = [1, 2, 3, 4, 5]
s = 0
for v in a:
    s += v
print(s)
'''
        self.check_equivalence(src)

    def test_string_concat(self):
        src = 'a = "Hello, " + "World!"\nprint(a)\n'
        self.check_equivalence(src)

    def test_string_repeat_str_int(self):
        src = 'a = "ab" * 3\nprint(a)\n'
        self.check_equivalence(src)

    def test_string_repeat_int_str(self):
        src = 'a = 3 * "ab"\nprint(a)\n'
        self.check_equivalence(src)

    def test_string_repeat_zero_and_negative(self):
        # CPython: both yield empty string
        src = 'print("ab" * 0)\nprint("ab" * -1)\n'
        self.check_equivalence(src)

    def test_string_concat_in_loop(self):
        src = '''s = ""
i = 0
while i < 5:
    s = s + "x"
    i = i + 1
print(s)
'''
        self.check_equivalence(src)

    def test_string_repeat_in_for(self):
        src = 'for i in range(4):\n    print("x" * i)\n'
        self.check_equivalence(src)

    def test_string_concat_with_len(self):
        src = 'a = "Hello, " + "World!"\nprint(len(a))\n'
        self.check_equivalence(src)

    def test_string_returned_from_function(self):
        # A function returns a concatenated string. The buffer must
        # survive the function return (heap-allocated, not stack).
        src = '''def make() -> str:
    s = "foo" + "bar" + "baz"
    return s
print(make())
'''
        self.check_equivalence(src)

    def test_list_returned_from_function(self):
        # A function builds a list and returns it. The data buffer must
        # survive the function return.
        src = '''def make() -> list[int]:
    a = [10, 20, 30]
    return a
x = make()
print(x[0], x[1], x[2])
'''
        self.check_equivalence(src)

    def test_range_returned_from_function(self):
        src = '''def make() -> list[int]:
    r = range(4)
    return r
x = make()
for v in x:
    print(v)
'''
        self.check_equivalence(src)

    def test_list_passed_and_mutated(self):
        # Pass a list to a function that mutates it; mutation visible
        # after return. This requires heap-allocated buffers.
        src = '''def fill(a: list[int]):
    i = 0
    while i < 3:
        a[i] = i * 100
        i = i + 1
x = [0, 0, 0]
fill(x)
print(x[0], x[1], x[2])
'''
        self.check_equivalence(src)

    def test_builtin_str(self):
        src = '''print(str(123))
print(str(-456))
print(str(0))
print(str(3.14))
print(str(-2.5))
print(str(0.0))
a = "n=" + str(7)
print(a)
'''
        self.check_equivalence(src)

    def test_builtin_bool_abs(self):
        src = '''print(bool(0))
print(bool(1))
print(bool(-5))
print(abs(5))
print(abs(-5))
print(abs(0))
print(abs(-2.5))
'''
        self.check_equivalence(src)

    def test_builtin_min_max(self):
        src = '''print(min(3, 7), max(3, 7), min(-1, -5), max(-1, -5))
print(min(3.14, 2.71))
print(max(3.14, 2.71))
x = min(10, 20) + max(10, 20)
print(x)
'''
        self.check_equivalence(src)

    def test_builtins_in_function(self):
        # Builtins must work inside user functions whose results are
        # returned (exercises heap buffers + polymorphic types).
        src = '''def label(n: int) -> str:
    return "v=" + str(n)

def clip(x: int) -> int:
    return min(max(x, 0), 100)

a = label(42)
b = clip(-5)
c = clip(150)
d = abs(-2.5)
print(a, b, c, d)
'''
        self.check_equivalence(src)

    def test_bounds_check_inbounds_equivalent(self):
        # With --bounds-check every in-range access behaves identically.
        src = '''a = [1, 2, 3]
s = "hey"
a[0] = 10
print(a[0], a[1], a[2])
print(s[0], s[2])
i = 0
while i < 3:
    print(a[i])
    i = i + 1
'''
        expected, exp_status = oracle_output(src)
        code, actual = qemu_output(src, bounds_check=True)
        self.assertEqual(exp_status, 0)
        self.assertEqual(code, 0)
        self.assertEqual(actual, expected.encode())

    def test_bounds_check_oob_exits_nonzero(self):
        # An out-of-range read under --bounds-check must exit with status 1,
        # print nothing to stdout, and report an IndexError on stderr.
        import shutil
        import tempfile
        from pathlib import Path
        from unittests.helpers import runner, _write_temp
        src = 'a = [1, 2, 3]\nb = a[7]\nprint(b)\n'
        py = _write_temp(src, 'oob.py')
        tmp = tempfile.mkdtemp(prefix='pyrv-oob-')
        try:
            arts = runner.compile_program(py, tmp, name='prog',
                                          bounds_check=True)
            code, out, err = runner.run_qemu(arts['exe'])
            self.assertEqual(code, 1)
            self.assertEqual(out, b'')
            self.assertIn(b'IndexError', err)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(Path(py).parent, ignore_errors=True)


@unittest.skipUnless(toolchain_available(), 'RISC-V toolchain not available')
class TestOptEquivalence(unittest.TestCase):
    """Optimized and unoptimized binaries must agree with each other."""

    def test_opt_vs_noopt(self):
        for name in BENCHMARKS:
            with self.subTest(name=name):
                src = benchmark_source(name)
                code1, out1 = qemu_output(src, passes=None)
                code2, out2 = qemu_output(src, passes=DEFAULT_PASSES)
                self.assertEqual(code1, 0)
                self.assertEqual(code2, 0)
                self.assertEqual(out1, out2)


if __name__ == '__main__':
    unittest.main()
