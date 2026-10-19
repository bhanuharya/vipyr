"""LLVM backend / IR structural regression tests."""

import unittest

import llvmlite.binding as llvm

from unittests.helpers import ir_text_for

llvm.initialize()
llvm.initialize_all_targets()
llvm.initialize_all_asmprinters()


class TestTargetAndLayout(unittest.TestCase):
    def test_triple(self):
        ir = ir_text_for('x = 1\n')
        self.assertIn('target triple = "riscv32-unknown-linux-gnu"', ir)

    def test_data_layout_pointer_32(self):
        # p:32:32 is load-bearing for SROA of slice aggregates.
        ir = ir_text_for('x = 1\n')
        self.assertIn('p:32:32', ir)

    def test_main_returns_i32_zero(self):
        ir = ir_text_for('x = 1\n')
        self.assertIn('define i32 @"main"()', ir)
        self.assertIn('ret i32 0', ir)


class TestNoUndef(unittest.TestCase):
    def test_bool_op_no_placeholder_block_crash(self):
        # Regression: `and`/`or` used to construct ir.Block(None) and crash.
        src = 'a = True\nb = False\nif a and not b:\n    print(1)\n'
        ir = ir_text_for(src)
        self.assertNotIn('undef', ir)

    def test_void_functions_return_defined_value(self):
        # Missing-return is rejected by the type checker; a defensive path
        # must still never introduce undef for a non-void function.
        src = '''
def f() -> int:
    return 1
print(f())
'''
        ir = ir_text_for(src)
        self.assertNotIn('undef', ir)


class TestIntDivisionIsTotal(unittest.TestCase):
    SRC = '''
def f(a: int, b: int) -> int:
    x = a
    x /= b
    return a / b + a % b + x
print(f(7, 0))
'''

    def test_divisor_is_guarded(self):
        # sdiv/srem are UB for a zero divisor or INT_MIN / -1, so every
        # divisor must come from the guard's select, never straight from
        # user data.
        mod = llvm.parse_assembly(ir_text_for(self.SRC))
        divs = 0
        for fn in mod.functions:
            for bb in fn.blocks:
                for ins in bb.instructions:
                    if ins.opcode in ('sdiv', 'srem'):
                        divs += 1
                        divisor = list(ins.operands)[1]
                        self.assertTrue(str(divisor).strip().split(' = ')[1].startswith('select'),
                                        f'unguarded divisor in: {ins}')
        self.assertEqual(divs, 3)

    def test_module_verifies(self):
        llvm.parse_assembly(ir_text_for(self.SRC)).verify()


class TestIntOnlyOperators(unittest.TestCase):
    SRC = '''
def f(a: int, n: int) -> int:
    x = (a << n) + (a >> n) + a // n + a ** n + (a & n) + (a | n) + (a ^ n) + ~a
    for i in range(3):
        if i == 1:
            continue
        x += i
    while x > 100:
        x -= 1
        if x == 50:
            continue
    return x
print(f(7, 2))
'''

    def test_shift_counts_are_masked(self):
        # LLVM shl/ashr are poison for counts >= 32; the count must come
        # from `and n, 31`.
        mod = llvm.parse_assembly(ir_text_for(self.SRC))
        shifts = 0
        for fn in mod.functions:
            for bb in fn.blocks:
                for ins in bb.instructions:
                    if ins.opcode in ('shl', 'ashr'):
                        shifts += 1
                        count = str(list(ins.operands)[1]).strip()
                        self.assertRegex(count, r'= and i32 .*, 31$', f'unmasked count in: {ins}')
        self.assertEqual(shifts, 2)

    def test_pow_calls_runtime(self):
        self.assertIn('call i32 @"pyr_ipow"', ir_text_for(self.SRC))

    def test_module_verifies(self):
        llvm.parse_assembly(ir_text_for(self.SRC)).verify()


class TestIRVerifies(unittest.TestCase):
    def test_verify_accepts(self):
        src = '''
def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)

a = fib(5)
b = a < 10 and a > 0
items = [1, 2, 3]
for it in items:
    print(it)
while a > 0:
    a -= 1
print(a, b)
'''
        ir_text = ir_text_for(src)
        mod = llvm.parse_assembly(ir_text)
        mod.verify()  # raises on failure


class TestSubscriptAssignmentIR(unittest.TestCase):
    """Structural checks for a[i] = x lowering."""

    def test_store_through_gep(self):
        # The backend must lower a[i] = x to a store through a GEP into
        # the list data buffer. There must be no undef.
        src = 'a = [1, 2, 3]\na[0] = 5\nprint(a[0])\n'
        ir = ir_text_for(src)
        self.assertIn('getelementptr', ir)
        self.assertIn('store', ir)
        self.assertNotIn('undef', ir)

    def test_subscript_assign_module_verifies(self):
        src = 'a = [1, 2, 3]\nb = a\nb[0] = 99\nprint(a[0], b[1])\n'
        ir = ir_text_for(src)
        mod = llvm.parse_assembly(ir)
        mod.verify()


class TestGlobalVariablesIR(unittest.TestCase):
    """Module-scope variables must be lowered to LLVM globals so that
    function bodies can read and write them."""

    def test_module_var_is_global(self):
        src = 'c = 10\nprint(c)\n'
        ir = ir_text_for(src)
        # The variable must be a module-level @global, not an alloca in
        # main. (llvmlite quotes global names that contain a dot, but
        # plain identifiers are emitted unquoted.)
        self.assertRegex(ir, r'@"?g_c"?\s*=\s*internal global i32')
        self.assertNotIn('alloca i32', ir.split('define i32 @"main"()', 1)[1])

    def test_function_reads_global(self):
        src = '''
c = 10
def get() -> int:
    return c
print(get())
'''
        ir = ir_text_for(src)
        mod = llvm.parse_assembly(ir)
        mod.verify()
        # The function body must reference the @g_c global.
        self.assertRegex(ir, r'load i32, i32\* @"?g_c"?')

    def test_function_writes_global(self):
        src = '''
counter = 0
def bump() -> int:
    counter = counter + 1
    return counter
print(bump())
print(counter)
'''
        ir = ir_text_for(src)
        mod = llvm.parse_assembly(ir)
        mod.verify()
        # The function body must store into the @g_counter global.
        self.assertIn('store i32', ir)
        self.assertIn('g_counter', ir)


class TestAugAssignIR(unittest.TestCase):
    """AugAssign must lower to one address + one load + one binop +
    one store. Re-evaluating the lvalue would emit a second call to
    any side-effecting index expression."""

    def test_aug_assign_calls_index_once(self):
        src = '''
ticks = 0
def tick() -> int:
    ticks = ticks + 1
    return ticks - 1
a = [10, 20, 30]
a[tick()] += 5
'''
        ir = ir_text_for(src)
        mod = llvm.parse_assembly(ir)
        mod.verify()
        # Isolate the body of @main.
        main = ir.split('define i32 @"main"()', 1)[1].split('}', 1)[0]
        # Exactly one call to tick() inside @main: the old lowering
        # called it twice (once for the read, once for the store).
        self.assertEqual(main.count('call i32 @"test.tick"()'), 1)
        # Exactly one `add` (the +=5): the old double-eval path also
        # re-ran the binop, producing two adds.
        self.assertEqual(main.count('add i32'), 1)



class TestStringConcatIR(unittest.TestCase):
    """Structural checks for string concat/repeat lowering."""

    def test_concat_uses_memcpy(self):
        src = 'a = "hi" + "!"\nprint(a)\n'
        ir_text = ir_text_for(src)
        # The lowering must call llvm.memcpy to copy the two string
        # data buffers into a fresh buffer.
        self.assertIn('llvm.memcpy', ir_text)
        # Must store a null terminator.
        self.assertIn('store i8 0', ir_text)

    def test_repeat_has_loop(self):
        # String repeat lowers to a loop with a conditional branch and
        # a basic block per loop part (cond / body / after).
        src = 'a = "ab" * 3\nprint(a)\n'
        ir_text = ir_text_for(src)
        self.assertIn('llvm.memcpy', ir_text)
        # Loop back-edge: a `br` to a block that re-checks the condition.
        self.assertIn('repeat-cond:', ir_text)
        self.assertIn('repeat-body:', ir_text)
        self.assertIn('repeat-after:', ir_text)
        # No undef.
        self.assertNotIn('undef', ir_text)

    def test_concat_module_verifies(self):
        src = 'a = "Hello, " + "World!"\nb = 3 * "x"\nprint(a, b)\n'
        ir_text = ir_text_for(src)
        mod = llvm.parse_assembly(ir_text)
        mod.verify()

    def test_repeat_negative_does_not_crash(self):
        # Negative repeat must clamp to 0 and still produce a valid module.
        src = 'a = "x" * -3\nprint(a)\n'
        ir_text = ir_text_for(src)
        mod = llvm.parse_assembly(ir_text)
        mod.verify()


class TestHeapAllocatorIR(unittest.TestCase):
    """Structural checks for the heap allocator integration."""

    def test_pyr_alloc_declared(self):
        src = 'a = [1, 2, 3]\nprint(a[0])\n'
        ir_text = ir_text_for(src)
        self.assertIn('declare i8* @"pyr_alloc"', ir_text)
        self.assertIn('@"pyr_alloc_init"', ir_text)


class TestBuiltinsIR(unittest.TestCase):
    """Structural checks for the str/bool/abs/min/max builtins."""

    def test_sprintf_declared(self):
        src = 'a = str(42)\nprint(a)\n'
        ir_text = ir_text_for(src)
        self.assertIn('declare i32 @"sprintf"', ir_text)

    def test_str_int_uses_sprintf_and_strlen(self):
        src = 'a = str(42)\nprint(a)\n'
        ir_text = ir_text_for(src)
        # sprintf writes into a heap buffer; strlen measures the result.
        self.assertIn('call i32 (i8*, i8*, ...) @"sprintf"', ir_text)
        self.assertIn('@"strlen"', ir_text)
        self.assertIn('call i8* @"pyr_alloc"', ir_text)

    def test_bool_cast_is_icmp(self):
        src = 'a = bool(5)\nprint(a)\n'
        ir_text = ir_text_for(src)
        # bool() must lower to an icmp against zero producing an i1.
        self.assertRegex(ir_text, r'icmp[^\n]*i32[^\n]*0')

    def test_abs_int_uses_select(self):
        src = 'a = abs(-5)\nprint(a)\n'
        ir_text = ir_text_for(src)
        self.assertRegex(ir_text, r'select\s+i1')

    def test_float_minmax_avoids_select(self):
        # llc-14 miscompiles `select` on f64 for RV32 soft-float, so float
        # min/max/abs must use an explicit branch instead.
        src = 'a = min(3.14, 2.71)\nb = max(3.14, 2.71)\nc = abs(-2.5)\nprint(a, b, c)\n'
        ir_text = ir_text_for(src)
        self.assertNotIn('select double', ir_text)
        self.assertIn('fcmp', ir_text)

    def test_module_verifies_with_builtins(self):
        src = '''a = str(-7)
b = abs(-2.5)
c = min(3.0, 9.0)
d = bool(4)
print(a, b, c, d)
'''
        ir_text = ir_text_for(src)
        mod = llvm.parse_assembly(ir_text)
        mod.verify()


class TestBoundsCheckIR(unittest.TestCase):
    """Structural checks for the --bounds-check flag."""

    def test_no_check_by_default(self):
        # Without the flag no bounds-check call may appear (the declaration
        # is always present but unused).
        src = 'a = [1, 2, 3]\nprint(a[1])\n'
        ir_text = ir_text_for(src)
        self.assertNotIn('call void @"pyr_bounds_check"', ir_text)

    def test_declared_when_enabled(self):
        src = 'a = [1, 2, 3]\nprint(a[1])\n'
        ir_text = ir_text_for(src, bounds_check=True)
        self.assertIn('declare void @"pyr_bounds_check"', ir_text)

    def test_load_checked(self):
        src = 'a = [1, 2, 3]\nprint(a[1])\n'
        ir_text = ir_text_for(src, bounds_check=True)
        self.assertIn('call void @"pyr_bounds_check"', ir_text)
        # The count is loaded from the descriptor (field 0) for the check.
        self.assertIn('getelementptr', ir_text)

    def test_store_checked(self):
        src = 'a = [1, 2, 3]\na[2] = 9\nprint(a[2])\n'
        ir_text = ir_text_for(src, bounds_check=True)
        # Two accesses: the subscript store and the print load.
        self.assertEqual(ir_text.count('call void @"pyr_bounds_check"'), 2)

    def test_string_subscript_checked(self):
        src = 's = "hey"\nprint(s[1])\n'
        ir_text = ir_text_for(src, bounds_check=True)
        self.assertIn('call void @"pyr_bounds_check"', ir_text)

    def test_module_verifies_with_bounds_check(self):
        src = '''a = [10, 20]
s = "abc"
i = 1
a[i] = a[0] + len(s)
print(a[i], s[i])
'''
        ir_text = ir_text_for(src, bounds_check=True)
        mod = llvm.parse_assembly(ir_text)
        mod.verify()

    def test_pyr_alloc_init_called(self):
        # The init function must be called at the start of main so the
        # heap is ready before any allocation.
        src = 'print(1)\n'
        ir_text = ir_text_for(src)
        self.assertIn('call void @"pyr_alloc_init"()', ir_text)

    def test_list_uses_pyr_alloc(self):
        # A list literal must lower to a call to pyr_alloc, not alloca.
        src = 'a = [1, 2, 3]\nprint(a[0])\n'
        ir_text = ir_text_for(src)
        self.assertIn('call i8* @"pyr_alloc"', ir_text)

    def test_string_concat_uses_pyr_alloc(self):
        src = 'a = "hi" + "!"\nprint(a)\n'
        ir_text = ir_text_for(src)
        self.assertIn('call i8* @"pyr_alloc"', ir_text)

    def test_string_repeat_uses_pyr_alloc(self):
        src = 'a = "ab" * 3\nprint(a)\n'
        ir_text = ir_text_for(src)
        self.assertIn('call i8* @"pyr_alloc"', ir_text)

    def test_range_uses_pyr_alloc(self):
        src = 'for i in range(3):\n    print(i)\n'
        ir_text = ir_text_for(src)
        self.assertIn('call i8* @"pyr_alloc"', ir_text)

    def test_module_verifies_with_heap_alloc(self):
        # The module must still verify when heap allocation is used.
        src = '''def make() -> list[int]:
    a = [1, 2, 3]
    b = "foo" + "bar"
    return a
x = make()
print(x[0])
'''
        ir_text = ir_text_for(src)
        mod = llvm.parse_assembly(ir_text)
        mod.verify()


if __name__ == '__main__':
    unittest.main()
