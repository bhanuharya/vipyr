# Language specification

This document specifies the language implemented by the pyrv compiler as of
`feat/pyrv-foundation`. It is the normative reference for both the LLVM
backend (`src/llvm.py`) and the independent reference evaluator
(`src/reference_evaluator.py`). Where the language deliberately differs from
CPython, the difference is called out explicitly.

The compiler accepts a *statically typed subset* of Python. It uses Python's
own grammar (`ast.parse`), then applies a custom type checker. Programs are
rejected at compile time if they use anything outside this subset.

## 1. Types

The following types exist:

| Type    | Representation      | Notes                                  |
|---------|---------------------|----------------------------------------|
| `int`   | 32-bit two's-complement | Arithmetic wraps (LLVM `i32`).      |
| `float` | IEEE-754 double     | LLVM `double`.                         |
| `bool`  | 1-bit               | Distinct from `int` (unlike CPython).  |
| `str`   | length + byte buffer | Immutable-ish; see §6.                |
| `list[T]` | count + element buffer | Homogeneous element type.            |
| `<None>` | unit                | Value of `None`, empty return.         |

Type annotations are **required** on every function parameter. Return type
annotations are optional (defaults to `None`).

```
def add(a: int, b: int) -> int:   # ok
    return a + b

def greet(name: str):             # return type defaults to None
    print(name)
```

### 1.1 Type inference and compatibility

Variables are declared by assignment; the declared type is the type of the
right-hand side and is fixed thereafter.

```
x = 10          # x : int
x = 20          # ok
x = 3.5         # rejected: float not assignable to int
```

`T` is assignable to `U` if `T == U` or `T` is a transitive subtype of `U`.
Currently the only subtyping edge is `str <: list[char]`. There is **no**
implicit numeric conversion: `int` and `float` are unrelated, and `bool` is
not a subtype of `int`.

### 1.2 Conversions

Explicit conversions are provided as builtins:

```
float(3)      # 3.0   (int -> float, exact for |x| < 2^53)
int(3.9)      # 3     (float -> int, truncates toward zero)
```

`int()` on a float truncates toward zero (matches `fptosi` and CPython's
`int(float)`). Out-of-range conversion is undefined behavior.

## 2. Expressions and operators

### 2.1 Binary operators

`+`, `-`, `*`, `/`, `%` are supported. Both operands must have the same
type; there is no implicit promotion.

* **int**: `+`, `-`, `*` wrap modulo 2^32. `/` is **truncating integer
  division** and `%` is the **signed remainder** (C `sdiv`/`srem`
  semantics), *not* CPython floor division.

  ```
  -7 / 3   == -2       # CPython -7 // 3 == -3
  -7 % 3   == -1       # CPython -7 % 3  ==  2
  ```

* **float**: IEEE-754 double operations with round-to-nearest-even.

* **str**: `+` concatenates two strings (`"a" + "b" == "ab"`). `*` repeats
  a string with a non-negative `int` count; both orderings work
  (`"ab" * 3 == 3 * "ab" == "ababab"`). A negative repeat count yields
  the empty string, matching CPython (`"ab" * -1 == ""`).

Integer division is a total function. On every target it produces the
RISC-V M-extension results, with no trap:

```
x / 0                == -1
x % 0                == x
-2147483648 / -1     == -2147483648     # wraps
-2147483648 % -1     == 0
```

The backend guards LLVM `sdiv`/`srem`, which are undefined behavior for
these inputs; the guards fold away when the divisor is a constant.

For `float`, division by zero gives the IEEE result (`inf`, `-inf`, or
`nan`), and `%` is C `fmod` (LLVM `frem`): the result has the sign of the
dividend, *not* CPython's floor modulo (`-7.0 % 3.0 == -1.0`; CPython gives
`2.0`).

### 2.2 Comparisons

`==`, `!=`, `<`, `<=`, `>`, `>=` are supported on `int`, `float`, and `bool`
(operands of equal type). Chained comparisons are **not** supported.

`str` supports only `==` and `!=` (length comparison followed by byte-wise
`memcmp`).

### 2.3 Unary operators

`+x`, `-x` on `int`/`float`; `not b` on `bool`. Integer negation wraps.

### 2.4 Boolean operators

`and` and `or` short-circuit and require `bool` operands (no truthiness of
arbitrary values). Their result is `bool`.

```
if x > 0 and x < 10:    # ok: both operands are bool
    ...

if items and items[0]:  # rejected: lists have no truthiness
    ...
```

## 3. Statements and control flow

Supported: assignment, augmented assignment, subscript assignment,
`if`/`else`, `while`, `for`/`else`, `break`, `pass`, function definition,
`return`, expression statements.

* `while`/`for` may have an `else` clause, which runs when the loop
  terminates normally (condition false / sequence exhausted) and is skipped
  by `break` — matching CPython.
* `break` is only valid inside a loop.
* A function whose declared return type is not `None` must return a value on
  every path, or compilation fails.
* `a[i] = x` writes to the `i`-th element of a list; `x` must be assignable
  to the element type. Lists are reference types, so the write is visible
  through every alias of that list (see §6).

## 4. Functions

* Parameters must be annotated; arguments must match parameter types exactly
  (assignable).
* Recursion is supported (the function name is bound before the body is
  type-checked).
* Mutual recursion / forward references are **not** supported.
* Scalars are passed by value; `list`/`str` are passed by reference (the
  slice descriptor is copied, not the buffer).
* Assigning to a name that already exists in an enclosing scope rebinds that
  existing binding (C-like), it does **not** create a fresh local like in
  CPython. A name is only a fresh local if the type checker recorded it as a
  first declaration in the current scope (this is what gives recursive
  activations their own locals).

## 5. Builtins

| Builtin  | Signature        | Semantics                                      |
|----------|------------------|------------------------------------------------|
| `print`  | `print(a, b, …)` | Space-separated, newline-terminated.           |
| `input`  | `input(prompt: str) -> str` | Writes prompt, reads one line.         |
| `len`    | `len(x) -> int`  | Element count of a list or string.             |
| `range`  | `range(n: int) -> list[int]` | List `[0, 1, …, n-1]`.                 |
| `float`  | `float(int) -> float` | See §1.2.                                 |
| `int`    | `int(float) -> int`   | See §1.2.                                 |
| `str`    | `str(int) -> str`, `str(float) -> str` | See §5.2.                    |
| `bool`   | `bool(x: int) -> bool` | `0 → False`, non-zero → `True`.           |
| `abs`    | `abs(int) -> int`, `abs(float) -> float` | Absolute value.          |
| `min`    | `min(a, b)` (both `int` or both `float`) | Returns the smaller; result type matches the arguments. On a tie returns `a`. |
| `max`    | `max(a, b)` (both `int` or both `float`) | Returns the larger; result type matches the arguments. On a tie returns `a`. |

### 5.1 `print` formatting

The backend emits a `printf` call with the following conversions:

| Type    | Conversion |
|---------|------------|
| `int`   | `%d`       |
| `float` | `%f` (6 decimals) |
| `bool`  | `%d` (prints `1`/`0`, *not* `True`/`False`) |
| `str`   | `%s`       |
| `char`  | `%c`       |

Consequently `print(True)` prints `1`, and `print(3 / 2)` (int division)
prints `1`, both unlike CPython.

### 5.2 String conversions (`str`)

`str()` accepts `int` or `float` and produces a heap-allocated string:

- `str(42)` → `"42"`; formatted exactly as `printf("%d")` would.
- `str(3.14)` → `"3.140000"`; formatted with **6 decimal places**, matching
  `printf("%f")` — unlike CPython's shortest-round-trip repr.

The result is written via libc `sprintf` into a buffer from the runtime
heap (see `docs/runtime-model.md`), so it may be returned from functions,
concatenated, or stored like any other string.

## 6. Strings and lists

* A string is a length + `i8*` buffer; a list is a count + element buffer.
* List literals are homogeneous; element type is the type of the first
  element.
* `list[i]` reads/writes an element. By default there is **no bounds
  checking**: an out-of-range index is undefined behavior. When the program
  is compiled with `--bounds-check`, every list/string subscript emits a
  runtime check (`pyr_bounds_check`): an out-of-range or negative index
  prints `IndexError: index N out of range for size M` to stderr and exits
  with status 1 (see §7). Writing through one alias is visible through all
  other aliases, because lists are reference types.
* Indexing a `str` yields a `char`.
* Iteration: `for x in items:` where `items` is `list[T]` or `str`.
* Assignment of a list/string copies the slice descriptor, so both names
  refer to the same element buffer (reference semantics).
* String concatenation (`+`) and repetition (`*`) produce a **new** string
  whose buffer is independently allocated; the input strings are not
  modified. The result is null-terminated so it can be passed to C's
  `printf("%s", ...)`.
* Empty list literals `[]` have element type `<None>` and are not useful.

## 7. Scoping summary

* Module scope is global and visible everywhere.
* Function bodies open a new scope for parameters and locals.
* Loop bodies open a new scope (loop variable).
* There is no name shadowing: a `for` loop variable may not collide with an
  existing name; function parameters may not collide within one parameter
  list.

## 8. Deliberate deviations from CPython

1. **Truncating integer division/remainder** (`/`, `%` on `int`) — C
   `sdiv`/`srem`, not floor. Division by zero returns the RISC-V results
   instead of raising `ZeroDivisionError`. `%` on `float` is C `fmod`.
2. **No implicit numeric promotion** — `1 + 1.0` is a compile error.
3. **`bool` is not `int`** and prints as `0`/`1`.
4. **Static typing** — a variable's type is fixed at first assignment.
5. **Assignment to an existing outer name rebinds it** — no CPython-style
   local shadowing on assignment.
6. **No truthiness** — conditions and `and`/`or` operands must be `bool`.
7. **32-bit `int`** with wraparound, not arbitrary precision.
8. **No runtime errors for** out-of-range indexing or out-of-range
   `int(float)` — these are undefined behavior.
   Exception: with `--bounds-check`, out-of-range/negative subscripts are a
   checked runtime error (stderr diagnostic + exit status 1).

## 9. Runtime model

See `docs/runtime-model.md` for the memory layout, calling convention, and
execution model of the generated RISC-V code.
