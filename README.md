# vipyr

**A compiler for a statically typed subset of Python, targeting 32-bit RISC-V through LLVM.**

vipyr is an experimental compiler for a statically checked subset of Python. It lowers Python source code to LLVM IR, applies LLVM optimization passes, generates RISC-V32 code, and executes the resulting program inside a Buildroot system running under QEMU.

This repository is primarily a compiler-construction and optimization research prototype. It is not a complete Python implementation and should not be expected to compile arbitrary Python programs.

## Pipeline

```text
Python source
    │
    ▼
Python AST (`ast.parse`)
    │
    ▼
Custom type checker and checked nodes
    │
    ▼
LLVM IR generated with llvmlite (`out.ll`)
    │
    ▼
LLVM optimization passes (`opt-14` → `optimized.bc`)
    │
    ▼
RISC-V32 assembly (`llc` → `optimized.s`)
    │
    ▼
RISC-V Linux executable (`a.riscv`)
    │
    ▼
Buildroot `rootfs.ext2`
    │
    ▼
QEMU RISC-V32 system emulation
```

## Repository layout

```text
main.py                 Compiler command-line entry point
scripts/build.py        Compile, optimize, link, install, and run in QEMU
scripts/opt_build.py    Experimental build and measurement pipeline
scripts/run_tests.sh    Host-oriented test runner
scripts/run_regression_tests.sh  Full regression test runner
build.py                Compatibility launcher for scripts/build.py
opt_build.py            Compatibility launcher for scripts/opt_build.py
run_tests.sh             Compatibility launcher for scripts/run_tests.sh
run_regression_tests.sh  Compatibility launcher for scripts/run_regression_tests.sh
src/compiler.py         Frontend/type-checker/backend orchestration
src/type_check.py       Static type checking and checked-node construction
src/llvm.py              LLVM IR generation and RISC-V target setup
src/type.py             Type and LLVM representation definitions
src/checked_nodes.py    Checked AST node definitions
src/reference_evaluator.py  Independent reference evaluator (oracle)
src/runner.py           Reproducible build/run harness (env-configurable)
docs/language-spec.md   Normative language specification
docs/runtime-model.md   Memory layout / calling convention / runtime model
unittests/              Regression test suite (unittest)
tests/                  Language and benchmark programs
hasil/                  Saved optimization experiment results
compiler.drawio         High-level compiler flow diagram
```

## Language semantics

The language deliberately differs from CPython in several ways (truncating
integer division, no implicit numeric promotion, `bool` printed as `0`/`1`,
static typing, 32-bit `int`). The normative specification is
`docs/language-spec.md`; the runtime model is documented in
`docs/runtime-model.md`.

## Quick start

Compiling and running programs on RISC-V needs only stock tools. The custom
LLVM 14 build, Buildroot image, and `sudo` steps under [Setup](#setup) are
needed only for the original full-system QEMU experiments.

```bash
sudo apt install qemu-user llvm          # qemu-riscv32, opt, llc (LLVM 14+)
python3.12 -m venv .venv                 # llvmlite 0.43 needs Python <= 3.12
. .venv/bin/activate
pip install -r requirements.txt          # llvmlite, ziglang
python -m src.runner tests/standard.py   # compile to RISC-V, run under QEMU
python -m src.runner tests/standard.py --count   # ...and count executed instructions
./run_regression_tests.sh                # full suite, including QEMU runs
```

`--count` reports how many RISC-V instructions the program executed and in
which functions. [docs/measuring.md](docs/measuring.md) explains how it works
and has short exercises for reading the results.

Without a glibc cross toolchain (`PYRV_CC` or
`riscv32-unknown-linux-gnu-clang` on `PATH`), the runner links with Zig's
bundled clang and musl. The result is a static rv32gc/ilp32d executable that
`qemu-riscv32` runs without a sysroot. Validated with Ubuntu 24.04, LLVM 18,
QEMU 8.2, and Zig 0.17.

## Testing

```bash
./run_regression_tests.sh
```

runs the regression suite. It always exercises the frontend, type checker,
LLVM IR structure, and the reference evaluator, and additionally compiles the
benchmarks to RISC-V and executes them under QEMU when the toolchain is
available. The suite cross-checks QEMU output against the reference
evaluator, and compares optimized vs. unoptimized binaries. Tool paths are
resolved from `PYRV_OPT`, `PYRV_LLC`, `PYRV_CC`, `PYRV_SYSROOT`, and
`PYRV_QEMU_USER` (see `src/runner.py`).

To use a glibc toolchain instead of the Zig fallback, `PYRV_CC` must be a
Linux RISC-V32 **cross-Clang** capable of consuming the generated LLVM IR. A bare RISC-V GCC cannot be used
directly because the runner passes `.ll`/`.bc` files to the link stage; a
wrapper may first compile those inputs to objects and then link them with a
Buildroot GCC driver.

## Supported language subset

The implementation currently covers a restricted, typed subset of Python:

- `int`, `float`, `bool`, `str`, and lists
- assignments and augmented assignments
- arithmetic and comparisons
- boolean `and` and `or`
- `if`, `while`, `for`, and `break`
- functions, parameters, and `return`
- `print`, `input`, `len`, and `range`
- `float()`, `int()`, `str()`, and `bool()` conversions
- `abs()`, `min()`, and `max()`
- string concatenation (`+`) and repetition (`*`)
- list construction, iteration, indexing, and element assignment

The type checker rejects unsupported or incompatible operations before LLVM IR generation. Python features such as classes, exceptions, imports, dictionaries, comprehensions, generators, and dynamic reflection are outside the current scope.

## Requirements

The tested toolchain consists of:

- Python 3
- `llvmlite==0.43.0`
- LLVM 14, including `opt-14`
- A Debug/assertions-enabled LLVM build containing `llc`
- A Linux RISC-V32 cross-toolchain with glibc
- Buildroot configured for the RISC-V32 QEMU `virt` machine
- `qemu-system-riscv32`
- `ninja` and CMake for building LLVM
- `expect` for the automated QEMU run in `opt_build.py`
- `fsck.ext2` for checking the Buildroot filesystem image

The compiler targets:

```text
Triple:       riscv32-unknown-linux-gnu
Architecture: rv32gc
ABI:          ilp32d
```

The `M`, `A`, `F`, and `D` RISC-V extensions are enabled by the current build scripts.

## Setup

### 1. Install Python dependencies

Use a virtual environment rather than modifying the system Python installation:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

The repository currently pins:

```text
llvmlite==0.43.0
```

### 2. Build LLVM 14 `llc`

Clone the LLVM 14 release branch outside this repository:

```bash
git clone --branch release/14.x --depth 1 \
  https://github.com/llvm/llvm-project.git
cd llvm-project
mkdir build
cd build
```

Configure a Debug build with assertions and the LLVM project enabled:

```bash
cmake -G Ninja \
  -DLLVM_ENABLE_PROJECTS=llvm \
  -DCMAKE_BUILD_TYPE=Debug \
  -DLLVM_ENABLE_THREADS=ON \
  ../llvm
```

Build the code generator:

```bash
ninja llc -j"$(nproc)"
```

The required executable is:

```text
llvm-project/build/bin/llc
```

A full LLVM build is large. Keep the source checkout and build directory separate from this repository.

### 3. Provide a Linux RISC-V32 toolchain

The build scripts expect a Linux/glibc toolchain with binaries such as:

```text
riscv32-unknown-linux-gnu-clang
riscv32-unknown-linux-gnu-objdump
```

A typical GNU toolchain configuration is:

```bash
./configure \
  --prefix=/opt/riscv \
  --with-arch=rv32gc \
  --with-abi=ilp32d
make linux
```

Do not substitute a bare-metal `riscv32-unknown-elf` toolchain for the Linux toolchain. The generated executable links against the Linux sysroot and runtime libraries.

### 4. Build the Buildroot target

From a Buildroot checkout:

```bash
make qemu_riscv32_virt_defconfig
make
```

The build should produce files such as:

```text
output/images/Image
output/images/fw_jump.elf
output/images/rootfs.ext2
output/images/start-qemu.sh
```

The current target configuration uses RISC-V32, glibc, and the `ilp32d` ABI. The paths in `build.py` and `opt_build.py` are local configuration values and must be changed for another machine.

### 5. Configure the build scripts

Review these variables before running a target build:

```python
BUILDROOT_DIR = ".../buildroot/output/images"
RISCV_DIR = ".../riscv-toolchain"
DEBUG_LLC = ".../llvm-project/build/bin/llc"
```

`build.py` and `opt_build.py` currently use absolute paths and invoke privileged mount commands. Treat them as local experimental scripts rather than portable installation tools.

## Usage

### Generate LLVM IR and run on the host

```bash
python main.py tests/standard.py
```

This writes:

```text
out.ll
```

Pass `--bounds-check` to emit runtime bounds checks for list/string
subscripts: an out-of-range or negative index prints an `IndexError` to
stderr and exits with status 1 instead of being undefined behavior.

To compile the generated IR with the host Clang for a quick smoke test:

```bash
clang out.ll readline.c -Wno-override-module -o a.out
./a.out
```

Host execution checks frontend/backend behavior but does not validate the RISC-V target path.

### Build and run in RISC-V QEMU

```bash
python build.py tests/standard.py
```

The script performs the following operations:

1. Generates `out.ll`.
2. Runs `opt-14` with the requested optimization pipeline.
3. Generates RISC-V assembly and bitcode outputs.
4. Links `a.riscv` with the RISC-V Linux toolchain.
5. Mounts `rootfs.ext2` and copies `a.riscv` into it.
6. Unmounts the filesystem.
7. Boots QEMU and runs the program.

`opt_build.py` additionally records executable size, disassembled instruction count, QEMU output, and timing information.

### Select optimization passes

`build.py` accepts one or more passes after `--opt` or `-o`:

```bash
python build.py tests/standard.py \
  --opt instcombine simplifycfg sroa
```

Examples of passes used in the experiments include:

```text
simplifycfg       Simplify control flow
instcombine       Combine and canonicalize instructions
sroa              Scalar replacement of aggregates
dce               Dead code elimination
dse               Dead store elimination
gvn               Global value numbering
licm              Loop-invariant code motion
loop-unroll       Loop unrolling
loop-vectorize    Loop vectorization
globalopt         Global optimization
inline            Function inlining
```

Saved experiment logs are under `hasil/`. They should be interpreted together with functional output: a lower instruction count is not a valid improvement if the generated program crashes or produces incorrect output.

## Test programs and benchmarks

The test suite is organized from small language checks to heavier workloads:

```text
tests/basic_type_prints.py
tests/variables.py
tests/math_ops.py
tests/flow_control.py
tests/functions.py
tests/for_list.py
tests/standard.py
tests/medium.py
tests/complex.py
tests/performance_test.py
```

The workloads exercise:

- strings and lists
- variables and assignments
- arithmetic and conversions
- conditional and loop control flow
- recursive Fibonacci
- functions and floating-point operations
- factorial and matrix calculations

Run the host-oriented test loop with:

```bash
./run_tests.sh
```

For target experiments, run one benchmark at a time and preserve its optimization pipeline, output, instruction count, runtime, and exit status.

## Safety warnings

The target build modifies the Buildroot filesystem image and uses `sudo` for mounting. Before running it:

- Stop any QEMU instance using the same `rootfs.ext2`.
- Do not run the compiler while the filesystem image is mounted by another process.
- Verify that `/mnt` is not being used for another operation.
- Unmount the image after copying the executable.
- Run `fsck.ext2` against the image after an interrupted or suspicious mount operation.
- Keep generated binaries and experiment results separate from source changes.

Do not run unreviewed compiler output against a real system. The intended target is the isolated Buildroot/QEMU environment.

## Known limitations

This is an experimental compiler, not a production compiler. Current limitations include:

- bounds checking is optional (`--bounds-check`); default builds remain unchecked
- incomplete runtime and memory-safety modeling
- manually declared runtime functions
- incomplete ABI and calling-convention validation
- limited alias/lifetime information for aggregates and pointers
- floating-point overflow in deliberately intensive benchmarks
- hard-coded paths in the build scripts
- coarse QEMU timing for very short programs
- target runs may report non-zero shutdown statuses through the `expect`/QEMU flow

A `SIGSEGV` or unhandled signal is a real failed run. A non-zero status caused only by the QEMU shutdown sequence must be distinguished from a compiler or runtime failure.

## Validated backend fixes

The LLVM backend now includes two correctness fixes:

1. The RISC-V32 data layout explicitly declares 32-bit pointers with `p:32:32`. Without this field, LLVM optimization passes could treat pointers as 64-bit and corrupt aggregate/list values during SROA.
2. The generated `main` function returns `i32` and explicitly returns `0`, instead of returning `void` and producing an undefined process exit status.

With `llvmlite==0.43.0`, the following optimized workloads were compiled, linked, and executed successfully with RISC-V user-mode QEMU:

```text
variables.py  exit status 0
standard.py   exit status 0
medium.py     exit status 0
complex.py    exit status 0
```

This validates the compiler, optimizer, LLVM 14 `llc`, RISC-V Linux cross-linker, and generated-program execution path. The privileged `build.py` filesystem-mount workflow remains a separate integration path and should be run only after checking its configured Buildroot paths.

### Full regression validation

On 2026-08-25, the complete regression suite was run with LLVM 14, the
Buildroot RISC-V32 Linux/glibc sysroot, and `/usr/bin/qemu-riscv32`:

```text
Ran 146 tests in 21.079s
OK
```

This run included all oracle-vs-QEMU cases, optimized-vs-unoptimized
equivalence checks, bounds-check behavior, aggregate/list/string execution,
and exit-status checks. No tests were skipped. The repository worktree stayed
clean after the run.

## Interpreting optimization results

Compare optimization pipelines only after verifying:

1. The program output is functionally equivalent.
2. The target process did not receive a signal.
3. The executable was built for the same RISC-V ABI and target features.
4. The instruction-count method is unchanged.
5. Runtime measurements are repeated enough to overcome QEMU timing noise.

Use the executed count of the program's own functions (`--count`, "your
program") rather than a static count. Two caveats apply to the historical
results in `hasil/`. The executables were linked from bitcode, which the C
compiler recompiled at `-O0`, so they did not run `llc`'s optimized output.
And `opt_build.py` counts every disassembly line of the whole static binary,
which is mostly libc. See [docs/measuring.md](docs/measuring.md).

The central research trade-off is not only code reduction. It is the relationship between:

```text
optimization
code size
static instruction count
runtime
functional correctness
```

## Project status

The repository contains a working end-to-end prototype and historical optimization results. The LLVM checkout used to build `llc` is an upstream LLVM 14 release checkout; the thesis-specific work is primarily in this repository’s frontend, type checker, LLVM backend, build scripts, benchmarks, and experiment logs.
