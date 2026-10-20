"""Tests for executed-instruction counting (src/icount.py).

These need QEMU and the RISC-V toolchain, and are skipped without them.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from src import icount, runner
from unittests.helpers import toolchain_available, DEFAULT_PASSES


def loop_source(n: int) -> str:
    return f'''
s = 0
i = 0
while i < {n}:
    s = s + i
    i = i + 1
print(s)
'''


@unittest.skipUnless(toolchain_available(), 'RISC-V toolchain not available')
class TestInstructionCount(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pyrv-icount-'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, src: str, name: str = 'prog', passes=DEFAULT_PASSES):
        py = self.tmp / f'{name}.py'
        py.write_text(src)
        return runner.compile_program(str(py), str(self.tmp / name),
                                      passes=passes, name=name)

    def profile(self, src: str, **kwargs) -> icount.Profile:
        return icount.profile_program(self.build(src, **kwargs))

    def test_output_and_exit_code_unchanged(self):
        arts = self.build(loop_source(10))
        code, out, _err = runner.run_qemu(arts['exe'])
        p = icount.profile_program(arts)
        self.assertEqual((p.exit_code, p.stdout), (code, out))
        self.assertEqual(p.stdout, b'45\n')

    def test_hottest_instruction_runs_once_per_iteration(self):
        p = self.profile(loop_source(1000))
        mine = [n for pc, n in p.by_pc.items()
                if p.function_at[pc] in p.program_functions]
        self.assertEqual(max(mine), 1000)

    def test_cost_per_iteration_is_optimized_code(self):
        # The binary used to be linked from bitcode, which the C driver
        # recompiled at -O0: 15 instructions per iteration instead of 7.
        small = self.profile(loop_source(100), name='small').program
        big = self.profile(loop_source(200), name='big').program
        per_iteration, leftover = divmod(big - small, 100)
        self.assertEqual(leftover, 0)
        self.assertLessEqual(per_iteration, 10)

    def test_counts_are_reproducible(self):
        # Different directory depths give the guest different argv[0]s.
        first = self.profile(loop_source(50), name='a')
        second = self.profile(loop_source(50), name='a_much_longer_name')
        self.assertEqual(first.total, second.total)
        self.assertEqual(first.program, second.program)

    def test_mix_covers_program_instructions(self):
        p = self.profile(loop_source(100))
        if not p.mnemonic_at:
            self.skipTest('llvm-objdump not available')
        mix = p.program_mix()
        self.assertNotIn('?', mix)
        self.assertEqual(sum(mix.values()), p.program)
        self.assertEqual(mix['add'], 100)

    def test_timeout_stops_runaway_program(self):
        arts = self.build('x = 0\nwhile x >= 0:\n    x = x & 1\n')
        p = icount.run_counted(arts['exe'], timeout=1)
        self.assertEqual(p.exit_code, -1)
        self.assertGreater(p.total, 0)

    def test_report_names_program_functions(self):
        src = 'def f(n: int) -> int:\n    return n * 3\nprint(f(4))\n'
        text = icount.report(self.profile(src))
        mine = text.split('your functions\n')[1].split('\n\n')[0]
        self.assertIn('main', mine)
        self.assertIn('prog.f', mine)
        self.assertNotIn('printf', mine)


if __name__ == '__main__':
    unittest.main()
