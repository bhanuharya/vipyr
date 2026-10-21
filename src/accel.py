"""
Certificates that let the compiler use a proven custom instruction.

When the cage (src/cage.py) accepts a circuit for a function, it writes a
certificate: which function, which instruction encoding, which circuit (by
path, relative to the certificate, and hash), and a hash of the exact answer
key the circuit was proved against. The compiler replaces calls to the
function with the instruction only after recomputing the answer key from the
program it is compiling. If the hash differs, because the function changed
or another LLVM version prints it differently, the compiler puts the circuit
through the cage again and refuses the certificate unless it still passes.

Instructions use the custom-0 major opcode (0x0B), set aside by the RISC-V
spec for custom extensions, in R-type format:

    .insn r CUSTOM_0, funct3, funct7, rd, rs1, rs2
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

CUSTOM_0 = 0x0B


@dataclass
class Certificate:
    function: str
    funct3: int
    funct7: int
    answer_key_sha256: str
    circuit_sha256: str
    circuit: str

    def __post_init__(self):
        if not (0 <= self.funct3 < 8 and 0 <= self.funct7 < 128):
            raise ValueError(f'funct3 must be 0-7 and funct7 0-127, got {self.funct3}, {self.funct7}')

    @property
    def key(self) -> int:
        """How the runtime tells custom-0 instructions apart."""
        return self.funct7 << 3 | self.funct3

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + '\n')

    @classmethod
    def load(cls, path: str | Path) -> 'Certificate':
        return cls(**json.loads(Path(path).read_text()))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
