# Measuring what a program costs

"Faster" needs a number behind it. This page explains which number vipyr
uses, how it is measured, and two mistakes it caught in the earlier
measurements.

## Static and dynamic counts

A **static** count is how many instructions the binary contains. A
**dynamic** count is how many instructions the CPU actually executes. They
answer different questions:

```text
loop:  add  a1, a1, a2      static:  3 instructions
       addi a2, a2, 1
       blt  a2, a0, loop    dynamic: 3 x 1000 = 3000, if it loops 1000 times
```

Code size matters for small chips with little memory. The dynamic count is
what matters for speed and energy. It is also what a custom instruction
improves: replacing a hot 3-instruction sequence with 1 instruction saves 2
instructions every time it runs, not 2 instructions once.

## Counting with `--count`

```bash
python -m src.runner prog.py --count
```

runs the program and prints a report on stderr:

```text
executed RISC-V instructions
  your program         3,021   56.0%
  everything else      2,374   44.0%   (startup, printf, runtime)
  total                5,395

your functions
         3,005  fn.total
            16  main

hottest elsewhere (top 8)
           716  printf_core
           224  __init_libc
           ...

instruction mix in your program
         1,003  addi
         1,000  add
         1,000  blt
             4  li
           ...
```

That is the `fn.py` program from the [exercises](#exercises) below.

"Your program" counts the functions compiled from your `.py` file.
"Everything else" is musl's startup code, `printf`, and vipyr's runtime
(`runtime.c`, `readline.c`). When comparing optimizations, look at "your
program": libc does not change when you change the passes.

The counts are exact and the same on every machine. The program runs with an
empty environment and a fixed `argv[0]`, because libc's startup code loops
over both.

### How it works

QEMU translates guest code in chunks called translation blocks. Two
standard options turn its debug log into an instruction trace:

- `-one-insn-per-tb` makes every block exactly one instruction.
- `-d nochain,exec` logs every block as it starts running. `nochain` stops
  QEMU from jumping straight from one block to the next without logging.

So one log line means one executed instruction, and the line includes the
instruction's address and function name. `src/icount.py` reads the log
through a pipe, tallies it, and maps addresses to mnemonics with
`llvm-objdump`. This needs no QEMU plugins, which Ubuntu's `qemu-user`
package does not ship. Counting runs about 40x slower than a normal run.

You can do the same by hand:

```bash
qemu-riscv32 -one-insn-per-tb -d nochain,exec -D trace.log prog.riscv
wc -l trace.log                                   # instructions executed
awk '{print $NF}' trace.log | sort | uniq -c | sort -rn | head   # per function
```

## Two problems this found

**The executable ran unoptimized code.** `compile_program` ran `opt` and
`llc`, then gave the *bitcode* to the C compiler for linking. Clang compiles
bitcode inputs itself, at `-O0`, so `llc`'s output was written to the `.s`
file and never used. A 1000-iteration loop at the top level executed 15,024
instructions instead of 7,021. The thesis build scripts (`scripts/build.py`,
`scripts/opt_build.py`) link the same way. The runner now links the object
file that `llc` produces, and `unittests/test_icount.py` checks that the
loop costs what the optimized assembly says.

**The thesis instruction count was mostly libc.** `opt_build.py` measures
`objdump -d a.riscv | wc -l`: every line of disassembly in the whole
statically linked binary, including libc, and including blank lines and
labels. The program's own code is a small fraction of that, so the results in
`hasil/` say little about what each optimization pass did to the compiled
program.

## Exercises

Each one takes a minute. Predict the answer, then run it. Use this program as
`fn.py`:

```python
def total(n: int) -> int:
    s = 0
    i = 0
    while i < n:
        s = s + i
        i = i + 1
    return s

print(total(1000))
```

**1.** `fn.total` executes about 3,000 instructions. Open the assembly with
`--keep out/` and read `out/fn.s`. Which three instructions run 1000 times?

<details><summary>Answer</summary>

`add a1, a1, a2` (s = s + i), `addi a2, a2, 1` (i = i + 1), and `blt a2, a0`
(loop while i < n). `s`, `i`, and `n` all live in registers.

</details>

**2.** Move the loop to the top level of the file, outside any function.
Does it get faster or slower, and why?

<details><summary>Answer</summary>

Slower: 7 instructions per iteration instead of 3. Top-level variables are
globals, which live in memory, so every iteration loads `s` and `i` with `lw`
and stores them back with `sw`. The instruction mix shows it.

</details>

**3.** Run the function version with `--passes ""` (no optimization passes).
How many instructions per iteration now?

<details><summary>Answer</summary>

10. Without the `sroa` pass, each local variable stays in a stack slot, so
every use is a load or a store. `sroa` is what moves locals into registers.

</details>

**4.** Run it with `--passes sroa,indvars`. The count for `fn.total` drops
to 10 instructions in total, whatever `n` is. Read `fn.s`: where did the loop
go?

<details><summary>Answer</summary>

There is no loop left. The `indvars` pass uses scalar evolution to work out
that the loop computes `n * (n - 1) / 2`, and replaces it with that formula:
a few `mul`, shift, and `or` instructions. The compiler rediscovered Gauss's
sum formula. This is why benchmark loops need results that the compiler
cannot compute ahead of time.

</details>

**5.** In `tests/performance_test.py`, `fibonacci` executes over 100,000
`fld` (floating-point load) instructions. What is it loading?

<details><summary>Answer</summary>

Two things. First, the constants `1.0`, `-1.0`, and `-2.0` (LLVM rewrote
`n - 1.0` as `n + -1.0`). Base RISC-V has no instruction that holds a
floating-point constant, so each one is loaded from memory: `lui` builds the
address and `fld` loads the value. Second, each call that recurses saves the
registers `fs0` and `fs1` on the stack and loads them back with `fld` before
returning. The `auipc` and `jalr` pairs are the two halves of each `call`.

</details>
