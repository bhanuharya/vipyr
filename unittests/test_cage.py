"""Tests for the answer key (src/spec.py) and the cage (src/cage.py).

These need Z3, Yosys (pip install yowasp-yosys) and the RISC-V toolchain,
and are skipped without them.
"""

import random
import shutil
import tempfile
import unittest
from pathlib import Path

from unittests.helpers import oracle_output, toolchain_available

try:
    import z3  # noqa: F401
    from src import cage, spec
    cage.yosys_path()
    HAVE_TOOLS = toolchain_available()
except (ImportError, FileNotFoundError):
    HAVE_TOOLS = False

ROOT = Path(__file__).resolve().parent.parent
PID = ROOT / 'examples' / 'cage' / 'pid.py'
CANDIDATES = ROOT / 'examples' / 'cage' / 'candidates'

EDGES = [0, 1, -1, 7, -8, 127, -128, 128, -129, 32767, -32768, 65535,
         2147483647, -2147483648]


def q15mul_reference(a: int, b: int) -> int:
    a = ((a & 0xFFFF) ^ 0x8000) - 0x8000
    b = ((b & 0xFFFF) ^ 0x8000) - 0x8000
    return max(-32768, min(32767, (a * b + 16384) >> 15))


@unittest.skipUnless(HAVE_TOOLS, 'Z3, Yosys or the RISC-V toolchain not available')
class TestAnswerKey(unittest.TestCase):
    """The answer key is trusted, so it is checked against the oracle."""

    FUNCTIONS = '''
def mix(a: int, b: int) -> int:
    return ((a << 3) ^ (b >> 2)) + (a & b) - (a | 7) * 3

def division(a: int, b: int) -> int:
    return a / b + a % b + a // b

def clamp8(a: int, b: int) -> int8:
    return int8(min(max(a + b, -128), 127))

def absdiff(a: int16, b: int16) -> int16:
    return abs(a - b)

def less(a: int, b: int) -> bool:
    return a < b

def distance(a: int, b: int) -> int:
    if a > b:
        return a - b
    return b - a
'''
    PARAMS = {'mix': ('int', 'int'), 'division': ('int', 'int'), 'clamp8': ('int', 'int'),
              'absdiff': ('int16', 'int16'), 'less': ('int', 'int'), 'distance': ('int', 'int')}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='pyrv-spec-'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_q15mul_matches_reference(self):
        key = spec.from_program(str(PID), 'q15mul', str(self.tmp))
        self.assertEqual((key.arg_bits, key.ret_bits), ([16, 16], 16))
        rng = random.Random(0)
        pairs = [(a, b) for a in EDGES for b in EDGES]
        pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(200)]
        for a, b in pairs:
            self.assertEqual(key.evaluate(a, b), q15mul_reference(a, b), (a, b))

    def test_matches_oracle_on_every_supported_operation(self):
        src = self.tmp / 'ops.py'
        src.write_text(self.FUNCTIONS)
        rng = random.Random(1)
        inputs = [(a, b) for a in EDGES[::2] for b in EDGES[1::2]]
        inputs += [(rng.randrange(-2**31, 2**31), rng.randrange(-2**31, 2**31)) for _ in range(20)]
        calls = []
        for name, (ta, tb) in self.PARAMS.items():
            wrap = {'int': '{}', 'int16': 'int16({})'}
            for a, b in inputs:
                calls.append((name, a, b, f'print({name}({wrap[ta].format(a)}, {wrap[tb].format(b)}))'))
        expected, status = oracle_output(self.FUNCTIONS + '\n'.join(c[3] for c in calls) + '\n')
        self.assertEqual(status, 0)
        keys = {name: spec.from_program(str(src), name, str(self.tmp / name)) for name in self.PARAMS}
        for (name, a, b, _), line in zip(calls, expected.splitlines()):
            with self.subTest(fn=name, a=a, b=b):
                self.assertEqual(keys[name].evaluate(a & 0xFFFFFFFF, b & 0xFFFFFFFF), int(line))

    def test_rejects_what_an_instruction_cannot_do(self):
        cases = {
            'loop': 'def loop(a: int, b: int) -> int:\n    s = 0\n    for i in range(a):\n        s += b\n    return s\n',
            'listy': 'def listy(a: int, b: int) -> int:\n    x = [a, b]\n    return x[a]\n',
            'three': 'def three(a: int, b: int, c: int) -> int:\n    return a + b + c\n',
            'floaty': 'def floaty(a: float, b: float) -> float:\n    return a * b\n',
        }
        for name, body in cases.items():
            with self.subTest(fn=name):
                src = self.tmp / f'{name}.py'
                src.write_text(body)
                with self.assertRaises(spec.SpecError):
                    spec.from_program(str(src), name, str(self.tmp / name))


@unittest.skipUnless(HAVE_TOOLS, 'Z3, Yosys or the RISC-V toolchain not available')
class TestCage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='pyrv-cage-'))
        cls.key = spec.from_program(str(PID), 'q15mul', str(cls.tmp))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def judge(self, name):
        return cage.judge(str(CANDIDATES / f'{name}.v'), self.key)

    def failed(self, verdict):
        return {c.rule: c.detail for c in verdict.checks if not c.passed}

    def test_honest_circuits_are_accepted(self):
        for name in ('honest', 'honest_wide'):
            with self.subTest(candidate=name):
                verdict = self.judge(name)
                self.assertTrue(verdict.accepted, cage.report(verdict))

    def test_subtle_bug_is_caught_at_its_one_input(self):
        verdict = self.judge('no_saturation')
        self.assertFalse(verdict.accepted)
        self.assertEqual(list(self.failed(verdict)), ['equivalence'])
        cex = verdict.counterexample
        # -1 * -1 in Q15: the only int16 pair where rounding overflows.
        self.assertEqual((cex['a'] & 0xFFFF, cex['b'] & 0xFFFF), (0x8000, 0x8000))
        self.assertEqual(cex['required'], q15mul_reference(cex['a'], cex['b']))
        self.assertNotEqual(cex['got'], cex['required'])

    def test_backdoor_trigger_is_found(self):
        verdict = self.judge('backdoor')
        self.assertEqual(list(self.failed(verdict)), ['equivalence'])
        cex = verdict.counterexample
        self.assertEqual((cex['a'] & 0xFFFF, cex['b'] & 0xFFFF), (0x1337, 0x0042))
        self.assertEqual(cex['got'], 32767)

    def test_memory_is_rejected(self):
        cases = {'time_bomb': '$dff', 'hidden_latch': '$dlatch', 'gate_loop': 'logic loop'}
        for name, why in cases.items():
            with self.subTest(candidate=name):
                failed = self.failed(self.judge(name))
                self.assertIn(why, failed['no memory'])
                self.assertIn('not attempted', failed['equivalence'])

    def test_extra_ports_are_rejected(self):
        self.assertIn("extra input port 'clk'", self.failed(self.judge('time_bomb'))['interface'])
        failed = self.failed(self.judge('spy_port'))
        self.assertIn("extra output port 'debug'", failed['interface'])
        self.assertNotIn('no memory', failed)

    def test_undefined_bits_cannot_pass(self):
        # An output left as x must not count as matching the answer key.
        path = self.tmp / 'undefined.v'
        path.write_text('module accel (input [31:0] a, input [31:0] b, output [31:0] result);\n'
                        "  assign result = 32'bx;\nendmodule\n")
        verdict = cage.judge(str(path), self.key)
        self.assertEqual(list(self.failed(verdict)), ['equivalence'])

    def test_division_is_rejected(self):
        # Verilog leaves x / 0 undefined, so division is outside the cage.
        path = self.tmp / 'divides.v'
        path.write_text('module accel (input [31:0] a, input [31:0] b, output [31:0] result);\n'
                        '  assign result = a / b;\nendmodule\n')
        self.assertIn('$div', self.failed(cage.judge(str(path), self.key))['no memory'])

    def test_unparseable_verilog_is_rejected(self):
        path = self.tmp / 'broken.v'
        path.write_text('module accel (input [31:0] a;\n')
        verdict = cage.judge(str(path), self.key)
        self.assertFalse(verdict.accepted)
        self.assertIn('Yosys rejected', verdict.checks[0].detail)


if __name__ == '__main__':
    unittest.main()
