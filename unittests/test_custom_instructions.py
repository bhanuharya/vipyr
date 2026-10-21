"""Tests for --accel: calls compiled to custom-0 instructions.

The end-to-end tests need QEMU and the RISC-V toolchain, and are skipped
without them.
"""

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

from src import icount, runner
from src.error import CompileError
from src.llvm import LLVMBackend
from src.type_check import TypeChecker
from unittests.helpers import DEFAULT_PASSES, oracle_output, toolchain_available

PROGRAM = '''
def q15mul(a: int16, b: int16) -> int16:
    p = int(a) * int(b)
    r = (p + 16384) >> 15
    return int16(min(max(r, -32768), 32767))

def negative(x: int) -> bool:
    return x < 0

total = 0
for i in range(300):
    x = int16(i * 211 - 30000)
    y = int16(i * 97 + 17)
    total = total + int(q15mul(x, y))
    if negative(int(x)):
        total = total + 1
print(total)
print(q15mul(-32768, -32768))
'''
# 300 + 1 calls to q15mul and 300 to negative.
CUSTOM_INSTRUCTIONS = 601
ACCEL = ['q15mul:0:0', 'negative:1:2']


def ir_with_accel(src: str, accel: dict) -> str:
    checked = TypeChecker('t').evalNode(ast.parse(src))
    backend = LLVMBackend('t', outFile='/dev/null', accel=accel)
    backend.evalNode(checked, False)
    backend.emitFile()
    return str(backend.module)


class TestCustomInstructionIR(unittest.TestCase):

    def test_calls_become_side_effect_free_custom_instructions(self):
        ir = ir_with_accel(PROGRAM, {'q15mul': (0, 0), 'negative': (1, 2)})
        self.assertRegex(ir, r'asm\s+"\.insn r 11, 0, 0, \$0, \$1, \$2", "=r,r,r"')
        self.assertRegex(ir, r'asm\s+"\.insn r 11, 1, 2, \$0, \$1, \$2", "=r,r,r"')
        self.assertNotIn('sideeffect', ir)
        main = ir.split('define i32 @"main"()', 1)[1]
        self.assertNotIn('call i16 @"t.q15mul"', main)

    def test_emulation_copies_take_whole_registers(self):
        ir = ir_with_accel(PROGRAM, {'q15mul': (0, 0)})
        self.assertRegex(ir, r'define i32 @"?pyr_emul\.t\.q15mul"?\(i32 %"?\.1"?, i32 %"?\.2"?\)')
        self.assertIn('pyr_accel_table', ir)
        self.assertIn('call void @"pyr_accel_init"', ir)

    def test_unknown_function_is_an_error(self):
        with self.assertRaises(CompileError):
            ir_with_accel('x = 1\nprint(x)\n', {'nope': (0, 0)})

    def test_function_that_no_instruction_can_replace_is_an_error(self):
        cases = {
            'first': 'def first(xs: list[int]) -> int:\n    return xs[0]\nprint(first([1]))\n',
            'scale': 'def scale(x: float) -> float:\n    return x * 2.0\nprint(scale(1.0))\n',
            'three': 'def three(a: int, b: int, c: int) -> int:\n    return a\nprint(three(1, 2, 3))\n',
        }
        for name, src in cases.items():
            with self.subTest(fn=name), self.assertRaises(CompileError):
                ir_with_accel(src, {name: (0, 0)})

    def test_requests_are_validated(self):
        for bad in ('q15mul', 'q15mul:8:0', 'q15mul:0:128', '1x:0:0', 'q15mul:0'):
            with self.subTest(request=bad), self.assertRaises(runner.ToolchainError):
                runner.accel_flags([bad])
        with self.assertRaises(runner.ToolchainError):
            runner.accel_flags(['a:3:4', 'b:3:4'])


@unittest.skipUnless(toolchain_available(), 'RISC-V toolchain not available')
class TestCustomInstructionsRun(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pyrv-accel-'))
        (self.tmp / 'prog.py').write_text(PROGRAM)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name, accel=None):
        return runner.compile_program(str(self.tmp / 'prog.py'), str(self.tmp / name),
                                      passes=DEFAULT_PASSES, name='prog', accel=accel)

    def test_emulated_program_matches_and_counts_one_per_instruction(self):
        plain, fast = self.build('plain'), self.build('fast', ACCEL)
        expected, _ = oracle_output(PROGRAM)
        self.assertEqual(runner.run_qemu(plain['exe'])[:2], (0, expected.encode()))
        self.assertEqual(runner.run_qemu(fast['exe'])[:2], (0, expected.encode()))

        before, after = icount.profile_program(plain), icount.profile_program(fast)
        self.assertEqual(before.custom_instructions, 0)
        self.assertEqual(after.custom_instructions, CUSTOM_INSTRUCTIONS)
        self.assertGreater(after.emulation, 0)
        self.assertEqual(after.by_function().get('prog.q15mul', 0), 0)
        self.assertFalse(any(f.startswith('pyr_emul.') for f in after.program_functions))
        self.assertLess(after.program, before.program)

    def test_unknown_custom_instruction_still_kills_the_program(self):
        src = self.tmp / 'stray.c'
        src.write_text('void pyr_accel_init(const void *table, unsigned count);\n'
                       'int main(void) {\n'
                       '    pyr_accel_init(0, 0);\n'
                       '    __asm__ volatile(".insn r 11, 7, 0, a0, a0, a0");\n'
                       '    return 0;\n'
                       '}\n')
        exe = self.tmp / 'stray.riscv'
        proc = runner._run(runner.link_command() + [str(src), runner.runtime_path(), '-o', str(exe)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, _out, _err = runner.run_qemu(str(exe))
        self.assertNotEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
