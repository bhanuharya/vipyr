# Custom instructions

vipyr can compile every call to a function into one custom RISC-V
instruction, and run the result on stock QEMU by emulating the instruction
in software. It does not decide whether that is safe: nothing here checks
that a hardware circuit for the instruction computes what the function does.
[AEGIS-V](https://github.com/bhanuharya/aegis-v) is the checker built for
that; it proves a candidate circuit equal to the function and only then
calls vipyr with `--accel`.

## Using it

```bash
python -m src.runner prog.py --accel q15mul:0:0 --count
```

`--accel NAME:FUNCT3:FUNCT7` (repeatable) names a function and the encoding
of its instruction: the custom-0 major opcode (0x0B), which the RISC-V spec
sets aside for custom extensions, in R-type format. Two functions may not
share an encoding.

The function must take one or two integer or `bool` arguments and return an
integer or `bool`: an R-type instruction reads two registers and writes one.

## What the compiler emits

Each call becomes

```text
.insn r 11, FUNCT3, FUNCT7, rd, rs1, rs2
```

as LLVM inline assembly with no side effects, so LLVM can still move, merge
or drop it like an `add`. Arguments narrower than 32 bits are sign-extended
into their registers (`bool` is zero-extended), an unused second operand is
zero, and the result is truncated back to the function's return type. So the
instruction must compute

```text
rd == ext32(f(rs1[w1-1:0], rs2[w2-1:0]))
```

for argument widths `w1`, `w2`, where `ext32` sign-extends an integer and
zero-extends a `bool`.

## Running without the hardware

QEMU and most CPUs reject the instruction as illegal. For each accelerated
function the compiler also emits a software copy, `pyr_emul.<module>.<name>`,
and a table that `main` hands to `pyr_accel_init` in `runtime.c`. That
installs a SIGILL handler: it decodes the faulting instruction, and if its
encoding is in the table, computes `rd` with the software copy and resumes
after it. Any other illegal instruction still kills the program.

A run under QEMU therefore exercises the software copy, not any circuit.

`--count` counts each custom instruction once, in the program, as a CPU that
has it would. The emulation (the handler, the `pyr_emul.*` functions and the
kernel's signal-return trampoline) is reported on its own line and left out
of the totals:

```text
executed RISC-V instructions
  your program       266,279   92.3%   (6,000 of them custom instructions)
  everything else     22,230    7.7%   (startup, printf, runtime)
  total              288,509
  not counted      2,544,000           (software emulation of the custom instructions)
```

That is a fixed-point PID controller whose `q15mul` runs 6,000 times;
without `--accel` its program executes 372,274 instructions.
