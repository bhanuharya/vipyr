from .type_check import TypeChecker
from .llvm import LLVMBackend
from .checked_nodes import CheckedNode
import typing
import ast

class Compiler():
    """
    Handles the whole compilation proccess step by step
    """

    tc: TypeChecker
    backend: LLVMBackend
    astRoot: ast.AST | None
    checkedRoot: CheckedNode | None

    def __init__(self, fileName, outFile: str = 'out.ll', boundsCheck: bool = False,
                 accel: dict[str, tuple[int, int]] | None = None):
        self.tc = TypeChecker(fileName)
        self.backend = LLVMBackend(fileName, outFile=outFile, boundsCheck=boundsCheck, accel=accel)
        self.ast = None
        self.checkedRoot = None
        return

    def parseFile(self, f: typing.TextIO, fname: str) -> bool:
        """
        Use Python's ast module to parse the given file
        """
        text = f.read()
        try:
            self.astRoot = ast.parse(text, filename=fname)
            return True
        except SyntaxError as e:
            print(f'Syntax Error: {e}')
            return False

    def typeCheck(self):
        """
        Do type checking on the output from Python's ast
        """
        if self.astRoot == None:
            raise Exception("You need to parse a file before calling .typeCheck()")
        self.checkedRoot = self.tc.evalNode(self.astRoot)

    def compileToLLVM(self):
        """
        Take the type checked ast and generate LLVM Intermediate Representation from it.
        Use that to generate an ll file that can then be compiled by clang or optimized by opt
        """
        if self.checkedRoot == None:
            raise Exception("You need to type check a file before calling .compileToLLVM()")
        self.backend.evalNode(self.checkedRoot, False)
        self.backend.emitFile()
