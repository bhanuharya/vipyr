"""Tests for the answer key (src/spec.py) and the cage (src/cage.py).

These need Z3, Yosys (pip install yowasp-yosys) and the RISC-V toolchain,
and are skipped without them.
"""

import contextlib
import io
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from unittests.helpers import oracle_output, toolchain_available

from src import runner

try:
    import z3  # noqa: F401
    from src import accel, cage, icount, spec
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
                self.assertIn(why, failed['no state'])
                self.assertIn('not attempted', failed['equivalence'])

    def test_extra_ports_are_rejected(self):
        self.assertIn("extra input port 'clk'", self.failed(self.judge('time_bomb'))['interface'])
        failed = self.failed(self.judge('spy_port'))
        self.assertIn("extra output port 'debug'", failed['interface'])
        self.assertNotIn('no state', failed)

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
        self.assertIn('$div', self.failed(cage.judge(str(path), self.key))['no state'])

    def test_unparseable_verilog_is_rejected(self):
        path = self.tmp / 'broken.v'
        path.write_text('module accel (input [31:0] a;\n')
        verdict = cage.judge(str(path), self.key)
        self.assertFalse(verdict.accepted)
        self.assertIn('Yosys rejected', verdict.checks[0].detail)



@unittest.skipUnless(HAVE_TOOLS, 'Z3, Yosys or the RISC-V toolchain not available')
class TestCertifiedInstruction(unittest.TestCase):
    """Using an accepted circuit: certificates, codegen, emulation, counts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='pyrv-accel-'))
        cls.cert = cls.tmp / 'q15mul.cert.json'
        assert cls.cage(CANDIDATES / 'honest.v', cls.cert) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def cage(candidate, cert):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cage.main([str(PID), 'q15mul', str(candidate), '--accept', str(cert)])

    def build(self, src=PID, certs=(), name='plain'):
        return runner.compile_program(str(src), str(self.tmp / name),
                                      passes=runner.DEFAULT_PASSES, accel=[str(c) for c in certs])

    def tampered(self, name, **changes):
        data = json.loads(self.cert.read_text())
        data.update(changes)
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return path

    def test_rejected_circuit_gets_no_certificate(self):
        cert = self.tmp / 'backdoor.cert.json'
        self.assertEqual(self.cage(CANDIDATES / 'backdoor.v', cert), 1)
        self.assertFalse(cert.exists())

    def test_certificate_names_the_proved_circuit(self):
        cert = accel.Certificate.load(self.cert)
        self.assertEqual((cert.function, cert.funct3, cert.funct7), ('q15mul', 0, 0))
        circuit = self.cert.parent / cert.circuit
        self.assertEqual(accel.sha256_file(circuit), accel.sha256_file(CANDIDATES / 'honest.v'))

    def test_accelerated_program_matches_and_runs_fewer_instructions(self):
        plain = self.build()
        fast = self.build(certs=[self.cert], name='fast')
        self.assertIn('.insn r 11, 0, 0', Path(fast['asm']).read_text())
        self.assertEqual(runner.run_qemu(fast['exe'])[:2], runner.run_qemu(plain['exe'])[:2])

        before, after = icount.profile_program(plain), icount.profile_program(fast)
        # Three calls per control step, 2000 steps.
        self.assertEqual(after.custom_instructions, 6000)
        self.assertEqual(after.by_function().get('pid.q15mul', 0), 0)
        self.assertGreater(after.emulation, 0)
        self.assertNotIn('pyr_emul.pid.q15mul', after.program_functions)
        self.assertLess(after.program, 0.8 * before.program)

    def test_changed_function_is_refused(self):
        changed = self.tmp / 'changed' / 'pid.py'
        changed.parent.mkdir()
        changed.write_text(PID.read_text().replace('(p + 16384) >> 15', '(p + 16383) >> 15'))
        with self.assertRaises(runner.ToolchainError) as cm:
            self.build(changed, [self.cert], name='changed-build')
        self.assertIn('no longer matches', str(cm.exception))

    def test_answer_key_printed_differently_is_proved_again(self):
        # As if another LLVM version printed the same function differently.
        cert = self.tampered('reprove.cert.json', answer_key_sha256='0' * 64)
        self.assertTrue(Path(self.build(certs=[cert], name='reprove')['exe']).exists())

    def test_modified_circuit_is_refused(self):
        cert = self.tampered('modified.cert.json', answer_key_sha256='0' * 64,
                             circuit_sha256='0' * 64)
        with self.assertRaises(runner.ToolchainError) as cm:
            self.build(certs=[cert], name='modified')
        self.assertIn('missing or modified', str(cm.exception))

    def test_one_encoding_per_instruction(self):
        with self.assertRaises(runner.ToolchainError) as cm:
            self.build(certs=[self.cert, self.cert], name='twice')
        self.assertIn('same encoding', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
