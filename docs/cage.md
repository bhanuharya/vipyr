# The cage: checking AI-proposed instructions

An AI agent can propose a circuit for a custom RISC-V instruction, but
nothing it writes is trusted. The compiler supplies the answer key, and the
cage accepts a circuit only if three mechanical checks pass. A circuit that
fails a check is rejected whether its fault is a mistake or a planted
backdoor: an equivalence proof does not depend on intent.

The cage restricts what the agent controls to one thing, the arithmetic
function. It checks that function; it does not yet place it in a processor
(see [What is trusted, and what is not covered](#what-is-trusted-and-what-is-not-covered)).

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
  pass  no state     combinational logic only, no loops
  FAIL  equivalence  differs at a=0x00001337, b=0x00000042: required 0x0000000a, got 0x00007fff
...
honest.v: ACCEPTED
  pass  interface    inputs a, b and output result, 32 bits each
  pass  no state     combinational logic only, no loops
  pass  equivalence  equal to the answer key for all 2^64 input pairs
```

All eight candidates are checked in a few seconds.

## The three rules

| Rule | What it stops | Candidate it rejects |
| --- | --- | --- |
| **Interface**: exactly inputs `a`, `b` and output `result`, 32 bits each | Leaking data through extra wires; depending on a clock or anything else | `spy_port.v` (an extra `debug` output), `time_bomb.v` (a `clk` input) |
| **No state**: only allowlisted combinational cells, and no logic loops | Remembering earlier inputs: hidden counters, time bombs, covert channels | `time_bomb.v` (a flip-flop), `hidden_latch.v` (a latch), `gate_loop.v` (two gates wired into a latch) |
| **Equivalence**: equal to the answer key for every input | Wrong results, including a backdoor that fires on one secret input | `no_saturation.v` (wrong only for -1 × -1), `backdoor.v` (full torque on one trigger value) |

The rules support each other. Together, the first two mean the circuit is
purely combinational, with no persistent state and no access beyond its
declared inputs and output. Its result then depends only on `a` and `b`, so
checking every pair of inputs covers every behaviour it can have: the
guarantee is not limited to a finite execution horizon. The solver can still
time out, and a candidate without a proof is rejected.

The output separates structural rejections (interface, no state) from proof
failures (equivalence), and the examples are evidence for the checks, not a
proof that every attack is caught.

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
key it was proved against. It binds those files together, but it is not
evidence that a proof happened: it is an unsigned file anyone could write.
So `--accel` trusts none of it. On every build it checks the circuit's hash,
recomputes the answer key from the program being compiled, and runs the cage
again, which takes a few seconds. A certificate is refused if the function
changed, if the circuit was edited, or if the circuit never passed in the
first place.

Calls to the function then compile to one instruction,
`.insn r 11, 0, 0, rd, rs1, rs2`, marked free of side effects so LLVM can
still move or merge it. Stock QEMU does not know the instruction, so the
runtime emulates it: a SIGILL handler decodes it, calls a software copy of
the function, writes `rd` and resumes. That run exercises the software copy,
not the circuit; that the circuit computes the same results is what the
equivalence proof establishes, separately. `--count` counts each custom
instruction once, as a CPU that has it would, and reports the emulation
separately:

| PID controller, 2000 steps | Without | With the certified instruction |
| --- | ---: | ---: |
| Instructions in the program | 372,274 | 266,279 (6,000 of them custom) |
| In `q15mul` and its caller `control` | 166,000 | 60,000 |
| Output | identical | identical |

That is 28% fewer executed instructions for the whole controller. Executed
instructions are not cycles, and the controller's simulated joint advances
once per loop iteration, so fewer instructions do not yet show better control
or fewer missed deadlines. That needs cycles on a real core, the circuit's
area and delay from synthesis, and a plant model with physical time.

## What is trusted, and what is not covered

The guarantee rests on these tools being correct: LLVM's `opt`, the IR
translator in `src/spec.py`, Yosys (which reads the Verilog), and Z3 (which
does the proof). When an instruction is used, the trusted base also includes
the compiler's replacement of each call (`doCustomInstruction` in
`src/llvm.py`) and, under QEMU, the emulation handler in `runtime.c`. The
agent's output is not among them.

The agent cannot weaken acceptance through these: it supplies one Verilog
file, and the answer key, the cage's rules, the proof setup and the
certificate checks all run in the toolchain. An agent loop must keep it that
way: the agent returns text and gets no write access to the repository.

Not covered yet:

- **Integration into a core.** A coprocessor interface such as CV-X-IF (on
  CV32E40X) provides connectivity, not containment: it carries register
  reads, memory transactions and results that can signal exceptions. The
  plan is a trusted wrapper that owns instruction decoding, transaction
  identifiers, destination registers, write enables, handshakes and
  cancellation, and passes the generated function only its operands. Per-
  proposal proofs can stay local only once a reusable proof shows how any
  circuit that passes the cage composes with that wrapper and the core. The
  core's own correctness would remain an explicit assumption.
- **Timing and physical side channels.** The wrapper would enforce operand-
  independent interface timing, under stated assumptions. Timing, power and
  electromagnetic leakage of the circuit itself are separate questions and
  out of scope.

## How this differs from Janus

Janus (*Automated Generation of RISC-V Extensions with Formal Correctness
Guarantees*, Alvanaki, Wang, Muscinelli and Carloni, MLArchSys 2026) also keeps
correctness outside the agent, with a three-stage chain on the Ibex core. It
already treats every agent artifact as untrusted, and it already restricts
instructions to single-cycle combinational operations, so neither is a
difference here.

The difference is who integrates the instruction. In Janus the agent writes
the patch that wires it into Ibex's decode, routing and control, and a
bounded RVFI check verifies the integrated core. Here the agent would write
only the arithmetic function, and integration would be a fixed wrapper
verified once. The open question is whether that restriction keeps useful
speed-ups while enabling stronger, reusable guarantees. A fair answer needs
both kinds of integration on the same core: comparing Janus's Ibex numbers
with another core would mix up interface overhead with processor
differences.

Using LLMs to attack hardware is not new either: researchers at Georgia Tech
used a general-purpose LLM to design a hardware Trojan for a RISC-V CPU
([Harnessing the Power of General-Purpose LLMs in Hardware Trojan
Design](https://astrolavos.gatech.edu/2024/03/30/AIHWS2024/), AIHWS 2024).

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
