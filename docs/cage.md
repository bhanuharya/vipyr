# The cage: checking AI-proposed instructions

An AI agent can propose a circuit for a custom RISC-V instruction, but
nothing it writes is trusted. The compiler supplies the answer key, and the
cage accepts a circuit only if three mechanical checks pass. The agent may
be wrong, or it may have been tricked by a prompt injection into planting a
backdoor; either way, a bad circuit is rejected before it reaches the chip.

## Try it

`examples/cage/pid.py` is a fixed-point PID controller for one robot joint.
Its hot function is `q15mul`, a Q15 multiply that rounds and saturates. The
`candidates/` directory holds circuits standing in for agent proposals: two
correct ones and six that misbehave.

```bash
python -m src.cage examples/cage/pid.py q15mul examples/cage/candidates/*.v
```

```text
answer key: pid.q15mul(i16, i16) -> i16, from the compiler's IR

backdoor.v: REJECTED
  pass  interface    inputs a, b and output result, 32 bits each
  pass  no memory    combinational logic only, no loops
  FAIL  equivalence  differs at a=0x00001337, b=0x00000042: required 0x0000000a, got 0x00007fff
...
honest.v: ACCEPTED
  pass  interface    inputs a, b and output result, 32 bits each
  pass  no memory    combinational logic only, no loops
  pass  equivalence  equal to the answer key for all 2^64 input pairs
```

All eight candidates are checked in a few seconds.

## The three rules

| Rule | What it stops | Candidate it rejects |
| --- | --- | --- |
| **Interface**: exactly inputs `a`, `b` and output `result`, 32 bits each | Leaking data through extra wires; depending on a clock or anything else | `spy_port.v` (an extra `debug` output), `time_bomb.v` (a `clk` input) |
| **No memory**: only combinational cells, and no logic loops | Remembering earlier inputs: time bombs, covert channels | `time_bomb.v` (a flip-flop), `hidden_latch.v` (a latch), `gate_loop.v` (two gates wired into a latch) |
| **Equivalence**: equal to the answer key for every input | Wrong results, including a backdoor that fires on one secret input | `no_saturation.v` (wrong only for -1 × -1), `backdoor.v` (full torque on one trigger value) |

The rules support each other. Because the circuit has no memory, its output
depends only on `a` and `b`, so checking every pair of inputs covers every
behaviour it can have. That makes the equivalence proof complete, not
bounded by a number of cycles.

Testing would not catch the two equivalence failures. `no_saturation.v` is
wrong for one int16 input pair out of 2^32; `backdoor.v` is wrong for one
trigger value. The proof finds both, and reports the exact inputs.

## What is proved

For a function `f` with argument widths `w1`, `w2` and result width `wr`, an
accepted circuit satisfies

```text
result[31:0] == ext32(f(a[w1-1:0], b[w2-1:0]))   for every 32-bit a and b
```

where `ext32` sign-extends an integer result and zero-extends a `bool`. For
`q15mul` that is `result == sext32(q15mul(a[15:0], b[15:0]))`.

## Where the answer key comes from

`src/spec.py` compiles the program, asks LLVM's `opt` to print the function
as IR, and translates that IR into a Z3 formula. The agent never writes or
sees this step. The translator accepts only pure, single-block integer
functions of one or two arguments (the shape of an R-type instruction) and
rejects anything else. Flags that only add poison, such as `nsw`, are
ignored; that makes the answer key stricter than the IR, never looser.

The translator is part of the trusted base, so `unittests/test_cage.py`
checks it against the independent reference evaluator on every operation it
supports.

## Using an accepted instruction

`--accept` writes a certificate for an accepted circuit; nothing else does:

```bash
python -m src.cage examples/cage/pid.py q15mul examples/cage/candidates/honest.v \
    --accept examples/cage/q15mul.cert.json
python -m src.runner examples/cage/pid.py --accel examples/cage/q15mul.cert.json --count
```

The certificate names the function, the instruction encoding (custom-0
opcode, funct3, funct7), the circuit (path and hash) and a hash of the answer
key it was proved against. `--accel` recomputes the answer key from the
program being compiled. If it differs, because the function changed or
another LLVM version prints it differently, the circuit goes through the
cage again, and the certificate is refused unless it still passes. A proof
never outlives the code it was about.

Calls to the function then compile to one instruction,
`.insn r 11, 0, 0, rd, rs1, rs2`, marked free of side effects so LLVM can
still move or merge it. Stock QEMU does not know the instruction, so the
runtime emulates it: a SIGILL handler decodes it, calls a software copy of
the function, writes `rd` and resumes. `--count` counts each custom
instruction once, as a CPU that has it would, and reports the emulation
separately:

| PID controller, 2000 steps | Without | With the certified instruction |
| --- | ---: | ---: |
| Instructions in the program | 372,274 | 266,279 (6,000 of them custom) |
| In `q15mul` and its caller `control` | 166,000 | 60,000 |
| Output | identical | identical |

That is 28% fewer executed instructions for the whole controller. Executed
instructions are not cycles: the next measurements are cycles on a real core
and the circuit's area and delay from synthesis.

## What is trusted, and what is not covered

The guarantee rests on these tools being correct: LLVM's `opt`, the IR
translator in `src/spec.py`, Yosys (which reads the Verilog), and Z3 (which
does the proof). When an instruction is used, the trusted base also includes
the compiler's replacement of each call (`doCustomInstruction` in
`src/llvm.py`) and, under QEMU, the emulation handler in `runtime.c`. The
agent's output is not among them.

Not covered yet:

- **Integration into a core.** The plan is to place accepted circuits behind
  a fixed coprocessor port (CV-X-IF on an open core such as CV32E40X) that is
  checked once by people, so the agent never edits the processor itself.
- **Physical side channels**, such as power draw. A circuit with no memory
  and a fixed latency has no timing channel of its own, but power analysis
  is out of scope.

## How this differs from Janus

Janus (*Automated Generation of RISC-V Extensions with Formal Correctness
Guarantees*, Alvanaki, Wang, Muscinelli and Carloni, MLArchSys 2026) also keeps
correctness outside the agent, with a three-stage chain on the Ibex core.
Two differences matter. Janus lets the agent write the patch that integrates
the instruction into the core, and checks the integrated core with bounded
model checking; the cage never lets the agent touch the core, and its "no
memory" rule makes the equivalence proof unbounded. Janus also treats the
agent as possibly wrong; the cage's candidates assume it may be malicious.

## Writing a candidate

A candidate is one Verilog file whose top module is called `accel`:

```verilog
module accel (input [31:0] a, input [31:0] b, output [31:0] result);
  // combinational logic only
endmodule
```

Division and out-of-range part-selects are not allowed: Verilog leaves
`x / 0` and an out-of-range shift undefined, so their proof model might not
match the hardware that gets built.

Yosys reads it inside an empty directory. The YoWASP build of Yosys (`pip
install yowasp-yosys`) is sandboxed to that directory, so the candidate
cannot pull in other files with `include` or `$readmemh`.
