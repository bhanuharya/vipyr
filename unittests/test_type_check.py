"""Type-checker and frontend regression tests."""

import unittest

from src.error import CompileError
from src.reference_evaluator import InterpretError

from unittests.helpers import typecheck_source, oracle_output


class TestTypeCheckerAccepts(unittest.TestCase):
    def test_core_scalars(self):
        src = '''
a = 1
b = 2.5
c = True
d = "hello"
x = a + 2
y = b * 2.0
z = not c
print(x, y, z, d)
'''
        typecheck_source(src)  # must not raise

    def test_unary_ops(self):
        typecheck_source('x = -5\n')
        typecheck_source('x = +5\n')
        typecheck_source('b = True\nc = not b\n')

    def test_pass(self):
        typecheck_source('def f():\n    pass\n')

    def test_string_concat(self):
        typecheck_source('a = "hi"\nb = a + "!"\nprint(b)\n')

    def test_string_repeat_str_int(self):
        typecheck_source('a = "ab" * 3\nprint(a)\n')

    def test_string_repeat_int_str(self):
        typecheck_source('a = 3 * "ab"\nprint(a)\n')

    def test_builtin_conversions(self):
        typecheck_source('a = str(42)\nb = str(2.5)\nprint(a, b)\n')
        typecheck_source('a = bool(0)\nprint(a)\n')

    def test_builtin_numeric(self):
        typecheck_source('print(abs(-5), abs(2.5), min(1, 2), max(3.0, 4.0))\n')

    def test_builtin_float_result_assignable_to_float_var(self):
        # abs/min/max must return the argument's type: if the checker hard-
        # coded int, assigning the result to a float variable would fail.
        typecheck_source('a = 0.0\na = abs(-2.5)\nb = min(1.5, 2.5)\nc = max(0.5, 1.5)\n')

    def test_builtin_str_result_usable_as_str(self):
        # str() must produce a real str that concatenates with literals.
        typecheck_source('a = "n=" + str(7)\nprint(a)\n')

    def test_void_function_returns_none(self):
        src = '''
def f() -> None:
    return
f()
'''
        typecheck_source(src)

    def test_recursion(self):
        src = '''
def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
print(fib(10))
'''
        typecheck_source(src)


class TestTypeCheckerRejects(unittest.TestCase):
    def assert_compile_error(self, src, *fragments):
        with self.assertRaises(CompileError) as cm:
            typecheck_source(src)
        msg = str(cm.exception)
        for frag in fragments:
            self.assertIn(frag, msg)

    def test_missing_return(self):
        self.assert_compile_error(
            'def f() -> int:\n    x = 1\n', 'does not return')

    def test_undefined_name(self):
        self.assert_compile_error('x = y + 1\n', 'Undefined identifier y')

    def test_type_mismatch(self):
        self.assert_compile_error(
            'x = 1\nx = 2.5\n', 'Cannot assign')

    def test_bool_not_int(self):
        self.assert_compile_error(
            'x = 1\nx = True\n', 'Cannot assign')

    def test_break_outside_loop(self):
        self.assert_compile_error('break\n', 'break outside')

    def test_non_bool_condition(self):
        self.assert_compile_error('if 1:\n    pass\n', 'not a boolean')

    def test_mixed_arithmetic(self):
        self.assert_compile_error('x = 1 + 2.0\n', 'Cannot perform')

    def test_unannotated_arg(self):
        self.assert_compile_error(
            'def f(x):\n    return x\n', 'needs to have a type annotation')

    def test_chained_comparison(self):
        self.assert_compile_error(
            'a = 1\nb = 2\nc = a < b < 3\n', 'Multiple comperators')

    def test_subscript_assign(self):
        typecheck_source('a = [1, 2, 3]\na[0] = 5\nprint(a[0])\n')

    def test_nested_subscript_assign(self):
        typecheck_source('m = [[1, 2], [3, 4]]\nm[0][1] = 10\n')

    def test_subscript_assign_type_mismatch(self):
        self.assert_compile_error(
            'a = [1, 2, 3]\na[0] = "x"\n', 'Cannot assign')

    def test_return_wrong_type(self):
        self.assert_compile_error(
            'def f() -> int:\n    return 1.5\n', 'Cannot return')

    def test_str_plus_int_rejected(self):
        self.assert_compile_error('a = "x" + 1\n', 'binary operation')

    def test_str_plus_list_rejected(self):
        self.assert_compile_error('a = [1]\nb = "x" + a\n', 'binary operation')

    def test_str_times_float_rejected(self):
        self.assert_compile_error('a = "x" * 1.5\n', 'binary operation')

    def test_str_of_str_rejected(self):
        self.assert_compile_error('a = str("hi")\n', 'str()')

    def test_abs_of_str_rejected(self):
        self.assert_compile_error('a = abs("hi")\n', 'abs()')

    def test_min_mixed_types_rejected(self):
        self.assert_compile_error('a = min(1, 2.5)\n', 'min()')

    def test_max_mixed_types_rejected(self):
        self.assert_compile_error('a = max(1.5, 2)\n', 'max()')

    def test_bool_of_float_rejected(self):
        self.assert_compile_error('a = bool(2.5)\n', 'bool()')

    def test_abs_wrong_arity_rejected(self):
        self.assert_compile_error('a = abs(1, 2)\n', 'argument')

    def test_min_one_arg_rejected(self):
        self.assert_compile_error('a = min(1)\n', 'argument')

    def test_int_times_list_rejected(self):
        self.assert_compile_error('a = [1]\nb = 2 * a\n', 'binary operation')


class TestOracleSemantics(unittest.TestCase):
    def test_int_division_is_truncating(self):
        out, status = oracle_output('print(-7 / 3)\nprint(7 / -3)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '-2\n-2\n')

    def test_int_mod_is_srem(self):
        out, status = oracle_output('print(-7 % 3)\nprint(7 % -3)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '-1\n1\n')

    def test_int_division_by_zero_is_riscv_result(self):
        out, status = oracle_output('a = 7\nb = 0\nprint(a / b)\nprint(a % b)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '-1\n7\n')

    def test_int_division_overflow_wraps(self):
        src = 'a = -2147483647 - 1\nb = -1\nprint(a / b)\nprint(a % b)\n'
        out, status = oracle_output(src)
        self.assertEqual(status, 0)
        self.assertEqual(out, '-2147483648\n0\n')

    def test_aug_assign_wraps(self):
        # Regression: `+=` used to skip the i32 wrap that `x = x + 1` applied.
        src = 'x = 2147483647\nx += 1\nprint(x)\na = [2147483647]\na[0] *= 2\nprint(a[0])\n'
        out, status = oracle_output(src)
        self.assertEqual(status, 0)
        self.assertEqual(out, '-2147483648\n-2\n')

    def test_float_mod_is_fmod(self):
        out, status = oracle_output('print(-7.0 % 3.0)\nprint(7.0 % -3.0)\nprint(7.0 % 0.0)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '-1.000000\n1.000000\nnan\n')

    def test_float_division_by_zero_is_ieee(self):
        out, status = oracle_output('print(7.0 / 0.0)\nprint(-7.0 / 0.0)\nprint(0.0 / 0.0)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'inf\n-inf\nnan\n')

    def test_bool_print_is_01(self):
        out, status = oracle_output('print(True)\nprint(False)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '1\n0\n')

    def test_short_circuit(self):
        src = '''
def side(x: int) -> bool:
    print(x)
    return x > 0

a = side(1) and side(0) and side(2)
b = side(1) or side(0) or side(2)
print(a, b)
'''
        out, status = oracle_output(src)
        self.assertEqual(status, 0)
        # side(2) must not run: and short-circuits at side(0)==False,
        # or short-circuits at side(1)==True.
        self.assertEqual(out, '1\n0\n1\n0 1\n')

    def test_aug_assign_subscript_calls_index_once(self):
        # The reference evaluator must call the side-effecting index
        # expression exactly once.  Pre-fix it called it twice, which
        # (a) incremented the counter twice and (b) stored the result
        # at the wrong slot.
        src = '''
ticks = 0
def tick() -> int:
    ticks = ticks + 1
    return ticks - 1
a = [10, 20, 30]
a[tick()] += 5
print(a[0], a[1], a[2], ticks)
'''
        out, status = oracle_output(src)
        self.assertEqual(status, 0)
        self.assertEqual(out, '15 20 30 1\n')

    def test_subscript_assign_aliases(self):
        # Lists are reference types: writing through one alias must be
        # visible through another.
        src = '''a = [1, 2, 3]
b = a
b[0] = 99
print(a[0], a[1], b[2])
'''
        out, status = oracle_output(src)
        self.assertEqual(status, 0)
        self.assertEqual(out, '99 2 3\n')

    def test_string_concat(self):
        out, status = oracle_output(
            'a = "Hello, "\nb = "World!"\nprint(a + b)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'Hello, World!\n')

    def test_string_concat_empty(self):
        out, status = oracle_output(
            'a = "x" + ""\nb = "" + "y"\nc = "" + ""\nprint(a, b, c)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'x y \n')

    def test_string_concat_chained(self):
        out, status = oracle_output(
            'a = "a" + "b" + "c" + "d"\nprint(a)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'abcd\n')

    def test_string_repeat_str_int(self):
        out, status = oracle_output('print("ab" * 3)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'ababab\n')

    def test_string_repeat_int_str(self):
        out, status = oracle_output('print(3 * "ab")\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'ababab\n')

    def test_string_repeat_zero(self):
        out, status = oracle_output('print("ab" * 0)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '\n')

    def test_string_repeat_negative(self):
        # CPython: "ab" * -1 == ""
        out, status = oracle_output('print("ab" * -1)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '\n')

    def test_string_repeat_one(self):
        out, status = oracle_output('print("test" * 1)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'test\n')

    def test_string_repeat_empty_string(self):
        # "" * 5 == "" (count is 0, loop runs but copies 0 bytes)
        out, status = oracle_output('print("" * 5)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '\n')

    def test_string_concat_then_len(self):
        out, status = oracle_output(
            'a = "Hello, " + "World!"\nprint(len(a))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '13\n')

    def test_string_repeat_in_loop(self):
        out, status = oracle_output('''i = 0
while i < 4:
    print("x" * i)
    i = i + 1
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, '\nx\nxx\nxxx\n')

    def test_string_concat_with_input(self):
        # Verifies concat works on a non-literal (input) string
        out, status = oracle_output(
            'a = input("> ")\nb = a + "!"\nprint(b)\n', stdin='hi\n')
        self.assertEqual(status, 0)
        # The input prompt "> " is printed before the result.
        self.assertEqual(out, '> hi!\n')

    def test_concat_in_function_returned(self):
        # A function returns a concatenated string. The buffer must
        # survive the function return (heap-allocated, not stack).
        out, status = oracle_output('''def make() -> str:
    s = "foo" + "bar" + "baz"
    return s

print(make())
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'foobarbaz\n')

    def test_repeat_in_function_returned(self):
        out, status = oracle_output('''def make() -> str:
    s = "ab" * 5
    return s

print(make())
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'ababababab\n')

    def test_list_in_function_returned(self):
        # A function builds a list literal and returns it. The data
        # buffer must survive the function return.
        out, status = oracle_output('''def make() -> list[int]:
    a = [10, 20, 30]
    return a

x = make()
print(x[0], x[1], x[2])
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, '10 20 30\n')

    def test_range_in_function_returned(self):
        out, status = oracle_output('''def make() -> list[int]:
    r = range(4)
    return r

x = make()
i = 0
for v in x:
    print(v)
    i = i + 1
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, '0\n1\n2\n3\n')

    def test_list_mutation_across_function_call(self):
        # Build a list, pass it to a function that mutates it, verify
        # the mutation is visible after the function returns.
        out, status = oracle_output('''def fill(a: list[int]):
    i = 0
    while i < 3:
        a[i] = i * 100
        i = i + 1

x = [0, 0, 0]
fill(x)
print(x[0], x[1], x[2])
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, '0 100 200\n')

    def test_list_returned_then_mutated(self):
        # A function returns a list, the caller mutates it.
        out, status = oracle_output('''def make() -> list[int]:
    a = [1, 2, 3, 4, 5]
    return a

x = make()
x[2] = 99
print(x[0], x[1], x[2], x[3], x[4])
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, '1 2 99 4 5\n')

    def test_concat_multiple_function_calls(self):
        # Multiple calls to a function that returns a string: each call
        # must produce a fresh, independent buffer.
        out, status = oracle_output('''def greet(name: str) -> str:
    return "Hello, " + name + "!"

a = greet("Alice")
b = greet("Bob")
print(a)
print(b)
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'Hello, Alice!\nHello, Bob!\n')

    def test_str_int_conversion(self):
        out, status = oracle_output('print(str(123))\nprint(str(-456))\nprint(str(0))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '123\n-456\n0\n')

    def test_str_float_uses_printf_format(self):
        # str(float) must match the backend's sprintf("%f") output:
        # exactly 6 digits after the decimal point.
        out, status = oracle_output('a = str(3.14)\nb = str(-2.5)\nc = str(0.0)\nprint(a)\nprint(b)\nprint(c)\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '3.140000\n-2.500000\n0.000000\n')

    def test_bool_cast(self):
        out, status = oracle_output('print(bool(0))\nprint(bool(1))\nprint(bool(-5))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '0\n1\n1\n')

    def test_abs_values(self):
        out, status = oracle_output('print(abs(5))\nprint(abs(-5))\nprint(abs(0))\nprint(abs(-2.5))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '5\n5\n0\n2.500000\n')

    def test_min_max_values(self):
        out, status = oracle_output(
            'print(min(3, 7), min(7, 3), max(3, 7), max(7, 3), min(-1, -5), max(-1, -5))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '3 3 7 7 -5 -1\n')

    def test_min_max_floats(self):
        out, status = oracle_output('print(min(3.14, 2.71))\nprint(max(3.14, 2.71))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '2.710000\n3.140000\n')

    def test_min_max_ties_return_first(self):
        # On a tie both are equal; result value must be that value.
        out, status = oracle_output('print(min(4, 4))\nprint(max(4, 4))\n')
        self.assertEqual(status, 0)
        self.assertEqual(out, '4\n4\n')

    def test_builtins_compose(self):
        src = '''a = str(min(10, 20)) + str(max(30, 40))
b = abs(-10) + abs(5)
c = bool(1) + bool(0)
print(a)
print(b)
print(c)
'''
        out, status = oracle_output(src)
        self.assertEqual(status, 0)
        self.assertEqual(out, '1040\n15\n1\n')

    def test_str_in_function_returned(self):
        out, status = oracle_output('''def label(n: int) -> str:
    return "v=" + str(n)

print(label(42))
''')
        self.assertEqual(status, 0)
        self.assertEqual(out, 'v=42\n')

    def test_oob_read_raises(self):
        with self.assertRaises(InterpretError) as cm:
            oracle_output('a = [1, 2, 3]\nb = a[7]\n')
        self.assertIn('IndexError', str(cm.exception))

    def test_oob_write_raises(self):
        with self.assertRaises(InterpretError):
            oracle_output('a = [1, 2, 3]\na[5] = 0\n')

    def test_negative_index_raises(self):
        # The language does not support negative indices.
        with self.assertRaises(InterpretError) as cm:
            oracle_output('a = [1, 2, 3]\nb = a[-1]\n')
        self.assertIn('IndexError', str(cm.exception))

    def test_string_oob_raises(self):
        with self.assertRaises(InterpretError) as cm:
            oracle_output('s = "hi"\nc = s[5]\n')
        self.assertIn('IndexError', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
