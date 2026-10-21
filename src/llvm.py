import opcode
import types
from .constants import REGISTER_SIZE
from .checked_nodes import *
from .error import CompileError
from pathlib import Path

import llvmlite.ir as ir
import llvmlite.binding as llvm

PRINT_IR = False
#PRINT_IR = True

class LLVMSymbol():
    t: Class
    ptr: ir.Value
    def __init__(self, t: Class, ptr: ir.Value):
        self.t = t
        self.ptr = ptr

class LLVMBackend():
    module: ir.Module
    fn: ir.Function
    builder: ir.IRBuilder
    symbols: list[dict[str, LLVMSymbol]]
    target_machine: llvm.TargetMachine
    triple: str
    def __init__(self, fileName: str, outFile: str = 'out.ll', boundsCheck: bool = False):
        self.outFile = outFile
        # When enabled every list/string subscript emits a runtime bounds
        # check (pyr_bounds_check) before the element access.
        self.boundsCheck = boundsCheck
        self.outFile = outFile
        llvm.initialize()
        llvm.initialize_all_targets()
        llvm.initialize_all_asmprinters()
        self.triple = 'riscv32-unknown-linux-gnu'
        target = llvm.Target.from_triple(self.triple)
        self.target_machine = target.create_target_machine()
        self.module = ir.Module(Path(fileName).stem)
        self.module.triple = self.triple
        self.module.data_layout = 'e-m:e-p:32:32-i32:32-i16:16-i8:8-n32-S32'

        #self.module.triple = llvm.get_default_triple()
        self.fn = ir.Function(module=self.module, ftype=ir.FunctionType(return_type=ir.IntType(32), args=()), name='main')
        entry = self.fn.append_basic_block('entry')
        self.builder = ir.IRBuilder(entry)
        
        self.symbols = list[dict[str, LLVMSymbol]]()
        self.symbols.append(dict[str, LLVMSymbol]())

        int8: ir.IntType = ir.IntType(8)
        voidPtr = ir.PointerType(int8)
        memcpy = ir.Function(self.module, ir.FunctionType(ir.VoidType(), [
            voidPtr,
            voidPtr,
            ir.IntType(REGISTER_SIZE),
            ir.IntType(1)
        ]), 'llvm.memcpy.p0.p0.i32')

        printf = ir.Function(self.module, ir.FunctionType(ir.IntType(REGISTER_SIZE), [
            ir.IntType(8).as_pointer(), # Format
        ], True), 'printf')

        readline = ir.Function(self.module, ir.FunctionType(ir.IntType(8).as_pointer(), [
            ir.IntType(8).as_pointer(), # Prompt
        ], True), 'readline')


        strlen = ir.Function(self.module, ir.FunctionType(ir.IntType(REGISTER_SIZE), [
            ir.IntType(8).as_pointer(), # Format
        ], True), 'strlen')

        memcmp = ir.Function(self.module, ir.FunctionType(ir.IntType(REGISTER_SIZE//2), [
            voidPtr,
            voidPtr,
            ir.IntType(REGISTER_SIZE),
        ]), 'memcmp')

        # pyr_alloc_init() -> void
        pyr_alloc_init = ir.Function(self.module, ir.FunctionType(ir.VoidType(), []), 'pyr_alloc_init')
        # pyr_alloc(i32 size) -> i8*
        pyr_alloc = ir.Function(self.module, ir.FunctionType(voidPtr, [
            ir.IntType(REGISTER_SIZE),
        ]), 'pyr_alloc')

        # sprintf(i8* buf, i8* fmt, ...) -> i32  (used by str())
        sprintf = ir.Function(self.module, ir.FunctionType(ir.IntType(REGISTER_SIZE), [
            ir.IntType(8).as_pointer(),
            ir.IntType(8).as_pointer(),
        ], True), 'sprintf')

        # pyr_bounds_check(i32 index, i32 count) -> void
        pyr_bounds_check = ir.Function(self.module, ir.FunctionType(ir.VoidType(), [
            ir.IntType(REGISTER_SIZE),
            ir.IntType(REGISTER_SIZE),
        ]), 'pyr_bounds_check')

        # pyr_ipow(i32 base, i32 exp) -> i32  (int ** int)
        pyr_ipow = ir.Function(self.module, ir.FunctionType(ir.IntType(REGISTER_SIZE), [
            ir.IntType(REGISTER_SIZE),
            ir.IntType(REGISTER_SIZE),
        ]), 'pyr_ipow')

        self.symbols[0]['memcpy'] = LLVMSymbol( FnType([], noneType()),  memcpy)
        self.symbols[0]['printf'] = LLVMSymbol( FnType([], noneType()),  printf)
        self.symbols[0]['readln'] = LLVMSymbol( FnType([], noneType()),  readline)
        self.symbols[0]['memcmp'] = LLVMSymbol( FnType([], noneType()),  memcmp)
        self.symbols[0]['pyr_alloc'] = LLVMSymbol( FnType([], noneType()), pyr_alloc)
        self.symbols[0]['pyr_alloc_init'] = LLVMSymbol( FnType([], noneType()), pyr_alloc_init)
        self.symbols[0]['sprintf'] = LLVMSymbol( FnType([], noneType()), sprintf)
        self.symbols[0]['pyr_bounds_check'] = LLVMSymbol( FnType([], noneType()), pyr_bounds_check)
        self.symbols[0]['pyr_ipow'] = LLVMSymbol( FnType([], noneType()), pyr_ipow)
        self.symbols[0]['strlen'] = LLVMSymbol( FnType([], noneType()),  strlen)

        self.breakBlock: list[ir.Block] = list()
        self.continueBlock: list[ir.Block] = list()

        self.globalCount = 0

        # Initialize the heap allocator before any other code runs.
        # pyr_alloc_init is idempotent and cheap, so this is always safe.
        self.builder.call(pyr_alloc_init, [])

    def emitFile(self):
        self.builder.ret(self.getConstant(0, 32))
        llvm_ir = str(self.module)

        if PRINT_IR:
            print(llvm_ir)

        parsed = llvm.parse_assembly(llvm_ir)
        parsed.data_layout = self.module.data_layout
        parsed.triple = self.triple
        parsed.verify()

        #pass_builder = llvm.create_pass_manager_builder()

        #pass_builder.opt_level = 3
        #module_manager = llvm.create_module_pass_manager()
        #self.target_machine.add_analysis_passes(module_manager)
        #pass_builder.populate(module_manager)
        #module_manager.run(parsed)
        #print(parsed)

        with open(self.outFile, 'w+') as f:
            f.write(str(parsed))

    def getConstString(self, string: str) -> ir.Value:
        strBytes = bytearray(string.encode()) + b'\x00'
        strT = ir.ArrayType(ir.IntType(8), len(strBytes))
        strConstant = ir.Constant(strT, strBytes)

        # A bare numeric name like "0" would print as `%hi(0)` in assembly,
        # which an assembler reads as address zero.
        globalV = ir.GlobalVariable(
            self.module, strT, f'.str.{self.globalCount}')
        globalV.global_constant = True
        globalV.initializer = strConstant # type: ignore
        globalV.linkage = 'internal'
        self.globalCount += 1

        zero = self.getConstant(0, 32)
        return self.builder.gep(globalV, [zero, zero])
    
    def doMemcpy(self, dst: ir.Value, src: ir.Value, size: ir.Value):
        voidPtr = ir.PointerType(ir.IntType(8))
        p1 = self.builder.bitcast(dst, voidPtr)
        p2 = self.builder.bitcast(src, voidPtr)
        assert p1 is not None
        assert p2 is not None
        dst = p1
        src = p2
        memcpy = self.symbols[0]['memcpy']
        self.builder.call(memcpy.ptr, [dst, src, size, ir.Constant(ir.IntType(1), 0)])

    def doStringConcat(self, left: ir.Value, right: ir.Value) -> ir.Value:
        """
        Implements `left + right` for two strings.
        `left` and `right` are `%str*` (pointers to {i32 count, i8* data}).
        Returns a new `%str*` pointing to a fresh, null-terminated buffer.
        """
        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)

        # Load count and data from left
        leftCountPtr = self.builder.gep(left, [zero, zero])
        leftCount = self.builder.load(leftCountPtr)
        leftDataPtr = self.builder.gep(left, [zero, one])
        leftData = self.builder.load(leftDataPtr)

        # Load count and data from right
        rightCountPtr = self.builder.gep(right, [zero, zero])
        rightCount = self.builder.load(rightCountPtr)
        rightDataPtr = self.builder.gep(right, [zero, one])
        rightData = self.builder.load(rightDataPtr)

        # Total length
        total = self.builder.add(leftCount, rightCount)
        # Allocate buffer (total + 1 for null terminator) on the heap
        totalPlusOne = self.builder.add(total, self.getConstant(1, 32))
        buffer = self.heapAlloc(ir.IntType(8), totalPlusOne)

        # Copy left
        self.doMemcpy(buffer, leftData, leftCount)
        # Copy right at offset leftCount
        offsetBuffer = self.builder.gep(buffer, [leftCount])
        self.doMemcpy(offsetBuffer, rightData, rightCount)
        # Null-terminate
        endPtr = self.builder.gep(buffer, [total])
        self.builder.store(ir.Constant(ir.IntType(8), 0), endPtr)

        # Build result string descriptor; use left's type for the LLVM struct
        return self.makeStringFromPointer(buffer, total, left.type.pointee)

    def doStringRepeat(self, strVal: ir.Value, n: ir.Value) -> ir.Value:
        """
        Implements `strVal * n` where strVal is `%str*` and n is `i32`.
        Matches CPython: negative n yields an empty string.
        Returns a new `%str*` pointing to a fresh, null-terminated buffer.
        """
        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)

        # Load count and data
        countPtr = self.builder.gep(strVal, [zero, zero])
        count = self.builder.load(countPtr)
        dataPtr = self.builder.gep(strVal, [zero, one])
        data = self.builder.load(dataPtr)

        # Clamp n to >= 0 (CPython: "a" * -1 == "")
        isNeg = self.builder.icmp_signed('<', n, zero)
        nClamped = self.builder.select(isNeg, zero, n)

        # Total length
        total = self.builder.mul(count, nClamped)
        # Allocate buffer (total + 1 for null terminator) on the heap
        totalPlusOne = self.builder.add(total, self.getConstant(1, 32))
        buffer = self.heapAlloc(ir.IntType(8), totalPlusOne)

        # Loop: for i in 0..nClamped-1, copy `data` into buffer at offset i*count
        idx = self.builder.alloca(ir.IntType(32))
        self.builder.store(self.getConstant(0, 32), idx)

        condB = self.fn.append_basic_block('repeat-cond')
        bodyB = self.fn.append_basic_block('repeat-body')
        afterB = self.fn.append_basic_block('repeat-after')

        self.builder.branch(condB)

        self.builder.position_at_end(condB)
        i = self.builder.load(idx)
        cond = self.builder.icmp_signed('<', i, nClamped)
        self.builder.cbranch(cond, bodyB, afterB)

        self.builder.position_at_end(bodyB)
        offset = self.builder.mul(i, count)
        destPtr = self.builder.gep(buffer, [offset])
        self.doMemcpy(destPtr, data, count)
        newI = self.builder.add(i, self.getConstant(1, 32))
        self.builder.store(newI, idx)
        self.builder.branch(condB)

        self.builder.position_at_end(afterB)
        # Null-terminate
        endPtr = self.builder.gep(buffer, [total])
        self.builder.store(ir.Constant(ir.IntType(8), 0), endPtr)

        return self.makeStringFromPointer(buffer, total, strVal.type.pointee)

    def doStore(self, ptr: ir.Value, val: ir.Value, t: Class):
        if t.isLoadable():
            self.builder.store(val, ptr)
        else:
            size = self.getConstant(t.getTypeSize())
            self.doMemcpy(ptr, val, size)

    def _llvmTypeSize(self, t: ir.Type) -> int:
        """Return the byte size of a primitive LLVM type for heap allocation."""
        if isinstance(t, ir.IntType):
            return max(1, t.width // 8)
        if isinstance(t, ir.PointerType):
            return 4   # 32-bit target
        if isinstance(t, ir.FloatType):
            return 4
        if isinstance(t, ir.DoubleType):
            return 8
        # For struct/array types we don't allocate; fall back to 1.
        return 1

    def heapAlloc(self, elemType: ir.Type, count: ir.Value) -> ir.Value:
        """
        Allocate `count` elements of `elemType` on the heap and return a
        typed pointer (`elemType*`). The buffer survives function returns.

        `count` may be a compile-time int or a runtime `ir.Value` (i32).
        """
        elemSize = self._llvmTypeSize(elemType)
        if isinstance(count, int):
            size = self.getConstant(count * elemSize, 32)
        else:
            size = self.builder.mul(count, self.getConstant(elemSize, 32))
        raw = self.builder.call(self.symbols[0]['pyr_alloc'].ptr, [size])
        # Bitcast i8* -> elemType*
        return self.builder.bitcast(raw, ir.PointerType(elemType))


    def evalNode(self, node: CheckedNode, lhs: bool) -> ir.Value | None:
        match node.base:
            case ast.Module():
                assert isinstance(node, CheckedModuleNode)
                mod: CheckedModuleNode
                mod = node
                for stmt in mod.statements:
                    self.evalNode(stmt, False)
            case ast.BinOp():
                assert isinstance(node, CheckedBinNode)
                return self.evalBinOp(node, lhs)
            case ast.Expr():
                return self.evalExpr(node, lhs)
            case ast.Assign():
                assert isinstance(node, CheckedAssignNode)
                return self.evalAssign(node, lhs)
            case ast.Constant():
                return self.evalConstant(node)
            case ast.AugAssign():
                assert isinstance(node, CheckedBinNode)
                return self.evalAugAssign(node, lhs)
            case ast.UnaryOp():
                assert isinstance(node, CheckedUnaryOpNode)
                return self.evalUnaryOp(node)
            case ast.Call():
                assert isinstance(node, CheckedCallNode)
                return self.evalCall(node, lhs)
            case ast.Name():
                return self.evalName(node, lhs)
            case ast.FunctionDef():
                assert isinstance(node, CheckedFnDefNode)
                return self.evalFnDef(node)
            case ast.Return():
                assert isinstance(node, CheckedReturnNode)
                return self.evalReturn(node)
            case ast.If():
                assert isinstance(node, CheckedIfNode)
                return self.evalIf(node)
            case ast.Compare():
                assert isinstance(node, CheckedBinNode)
                return self.evalCompare(node)
            case ast.List():
                assert isinstance(node, CheckedListNode)
                return self.evalList(node)
            case ast.While():
                assert isinstance(node, CheckedIfNode)
                return self.evalWhile(node)
            case ast.Break():
                return self.evalBreak(node)
            case ast.Continue():
                return self.evalContinue(node)
            case ast.Pass():
                return None
            case ast.BoolOp():
                assert isinstance(node, CheckedBoolOpNode)
                return self.evalBoolOp(node)
            case ast.Subscript():
                assert isinstance(node, CheckedSubscriptNode)
                return self.evalSubscript(node, lhs)
            case ast.For():
                assert isinstance(node, CheckedForNode)
                return self.evalFor(node)
            case _:
                print(f'LLVM unknown node type: {node.base}')
                assert False
        return None

    def isTerminated(self) -> bool:
        assert self.builder.block is not None
        return self.builder.block.is_terminated

    def evalSubscript(self, node: CheckedSubscriptNode, lhs: bool):
        op = self.evalNode(node.op, True)
        idx = self.evalNode(node.subscript, False)
        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)

        # Optional runtime bounds check (enabled with --bounds-check).
        # Both lists and strings share the {count, data} descriptor layout,
        # so the count is always at field 0. Negative indices are rejected
        # because the language does not support them.
        if self.boundsCheck:
            countPtr = self.builder.gep(op, [zero, zero])
            count = self.builder.load(countPtr)
            self.builder.call(self.symbols[0]['pyr_bounds_check'].ptr, [idx, count])

        dataPtr = self.builder.gep(op, [zero, one])
        data = self.builder.load(dataPtr)
        elem = self.builder.gep(data, [idx])

        # If we are on the left-hand side (ex. list[3] = x) we don't load the result
        # also if it's not loadble, (ex. foo = ['str1', 'str2'] y = foo[0]) we don't load the result
        if lhs or not node.t.isLoadable():
            return elem
        else:
            return self.builder.load(elem)

    def evalBody(self, body: list[CheckedNode], block: ir.Block, after: ir.Block):
        self.builder.position_at_end(block)
        for stmt in body:
            self.evalNode(stmt, False)
        if not self.isTerminated():
            self.builder.branch(after)

    def evalList(self, node: CheckedListNode) -> ir.Value:
        # Evaluate all the values in the list
        vals = list[ir.Value]()
        for v in node.vals:
            evaled = self.evalNode(v, False)
            assert evaled is not None
            vals.append(evaled)

        # I should probably save these in self
        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)

        # Allocate the list
        llvmT = node.t.toLLVM()
        listV = self.builder.alloca(llvmT)

        # Store the count in the list.count
        countPtr = self.builder.gep(listV, [zero, zero])
        self.builder.store(self.getConstant(len(vals)), countPtr)

        # if the list is empty we're done
        if len(vals) == 0:
            return listV

        # Allocate a buffer for the list data (heap-allocated so it survives
        # function returns).
        data = self.heapAlloc(node.t.asList().base.toLLVM(), len(vals))

        # list.data = allocated buffer
        dataPtr = self.builder.gep(listV, [zero, one])
        self.builder.store(data, dataPtr)

        # Walk through all the values
        # and store them in the list
        for i in range(len(node.vals)):
            iLLVM = self.getConstant(i, 32)
            ptr = self.builder.gep(data, [iLLVM])
            self.doStore(ptr, vals[i], node.t.asList().base)

        return listV
    
    def evalBoolOp(self, node: CheckedBoolOpNode) -> ir.Value:
        assert len(node.vals) >= 2
        
        suffix = node.op
        
        # Result allocation, its value is returned
        res = self.builder.alloca(ir.IntType(1))
        true = self.fn.append_basic_block('true-' + suffix)
        false = self.fn.append_basic_block('false-' + suffix)

        # One intermediate block per operand except the last.
        # For 'and' it evaluates the next operand when the current one was true,
        # for 'or' when the current one was false.
        nextBlocks = [self.fn.append_basic_block(f'{suffix}-next-{i}') for i in range(len(node.vals) - 1)]

        for i, v in enumerate(node.vals):
            expr = self.evalNode(v, False)
            if node.op == 'and':
                # If this value is true, evaluate the next operand,
                # the last one goes to the true block.
                nxt = nextBlocks[i] if i < len(node.vals) - 1 else true
                self.builder.cbranch(expr, nxt, false)
            else:
                assert node.op == 'or'
                nxt = nextBlocks[i] if i < len(node.vals) - 1 else false
                self.builder.cbranch(expr, true, nxt)
            if i < len(node.vals) - 1:
                self.builder.position_at_end(nextBlocks[i])
        
        after = self.fn.append_basic_block('after-' + suffix)
        # False block stores 0 in result allocation
        self.builder.position_at_end(false)
        self.builder.store(ir.Constant(ir.IntType(1), 0), res)
        self.builder.branch(after)

        # True block stores 1 in result allocation
        self.builder.position_at_end(true)
        self.builder.store(ir.Constant(ir.IntType(1), 1), res)
        self.builder.branch(after)

        # Reset the builder to a block where the rest of the code can be written
        # Load the result and return it
        self.builder.position_at_end(after)
        return self.builder.load(res)

    def evalBreak(self, node: CheckedNode) -> None:
        assert len(self.breakBlock) > 0
        assert node is not None
        self.builder.branch(self.breakBlock[-1])

    def evalContinue(self, node: CheckedNode) -> None:
        assert len(self.continueBlock) > 0
        assert node is not None
        self.builder.branch(self.continueBlock[-1])

    def evalIf(self, node: CheckedIfNode) -> None:
        #@TODO: ternary operators
        expr = self.evalNode(node.expr, False)

        iftrue = self.fn.append_basic_block('if-true')
        orelse = self.fn.append_basic_block('or-else')
        after = self.fn.append_basic_block('after')
        self.builder.cbranch(expr, iftrue, orelse)

        self.evalBody(node.iftrue, iftrue, after)
        self.evalBody(node.orelse, orelse, after)

        self.builder.position_at_end(after)

    def doStringCompare(self, node: CheckedBinNode) -> ir.Value:
        lhs = self.evalNode(node.left, True)
        rhs = self.evalNode(node.right, True)

        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)
        leftCountPtr = self.builder.gep(lhs, [zero, zero])
        leftDataPtr = self.builder.gep(lhs,  [zero, one])
        rightCountPtr = self.builder.gep(rhs, [zero, zero])
        rightDataPtr = self.builder.gep(rhs,  [zero, one])

        memcmpB = self.fn.append_basic_block('do_memcmp')
        trueB = self.fn.append_basic_block('true-cmp')
        afterB = self.fn.append_basic_block('after')

        # Allocate result and set it to 0
        # It will be set to 1 if the comparison is true
        result = self.builder.alloca(ir.IntType(1))
        self.builder.store(self.getConstant(0, 1), result)

        # Compare the counts of the 2 strings
        # If they are equal compare the contents
        # Otherwise go to the end
        leftCount = self.builder.load(leftCountPtr)
        rightCount = self.builder.load(rightCountPtr)
        countCond = self.builder.icmp_signed('==', leftCount, rightCount)
        self.builder.cbranch(countCond, memcmpB, afterB)

        self.builder.position_at_end(memcmpB)
        leftData = self.builder.load(leftDataPtr)
        rightData = self.builder.load(rightDataPtr)
        memcmp = self.symbols[0]['memcmp'].ptr
        memcmpResult = self.builder.call(memcmp, [leftData, rightData, rightCount])

        # memcmp returns 0 on success turn it into True or False
        # If the comparison was successful go to the true block
        # Otherwise go to the end
        zero = self.getConstant(0, REGISTER_SIZE//2)
        dataCond = self.builder.icmp_signed('==', memcmpResult, zero)
        self.builder.cbranch(dataCond, trueB, afterB)

        # Make the true block
        # it just sets 1 to the result and goes to the end
        self.builder.position_at_end(trueB)
        self.builder.store(self.getConstant(1, 1), result)
        self.builder.branch(afterB)

        # Finally load the result and return it
        self.builder.position_at_end(afterB)
        return self.builder.load(result)

        
    def evalCompare(self, node: CheckedBinNode):
        if node.left.t.name == 'str':
            res = self.doStringCompare(node)
            match node.op:
                case ast.Eq():
                    return res
                case ast.NotEq():
                    res = self.builder.icmp_signed('==', res, self.getConstant(0, 1))
                    return res
                case _: assert False

        return self.evalBinOp(node, False)

    def evalReturn(self, node: CheckedReturnNode) -> None:
        if node.expr is None:
            self.builder.ret_void()
            return
        
        val = self.evalNode(node.expr, False)
        self.builder.ret(val)

    def makeFnName(self, name: str):
        return self.module.name + '.' + name

    def evalFnDef(self, node: CheckedFnDefNode) -> ir.Value:
        assert isinstance(node.base, ast.FunctionDef)
        base: ast.FunctionDef
        base = node.base

        fn = ir.Function(module=self.module, ftype=node.t.toLLVM(), name=self.makeFnName(base.name))
        entry = fn.append_basic_block('entry')

        self.symbols[-1][self.makeFnName(base.name)] = LLVMSymbol(node.t, fn)
        self.symbols.append(dict())

        was_fn = self.fn
        was_builder = self.builder
        self.fn = fn
        self.builder = ir.IRBuilder(entry)

        for i, arg in enumerate(node.args):
            llvmArg = fn.args[i]
            alloca = self.builder.alloca(arg[1].toLLVM())
            self.doStore(alloca, llvmArg, arg[1])
            self.symbols[-1][arg[0]] = LLVMSymbol(arg[1], alloca)

        for stmt in node.statements:
            self.evalNode(stmt, False)

        retType = node.t.asCallable().returnType
        if not self.isTerminated():
            if retType.name == '<None>':
                self.builder.ret_void()
            else:
                # The type checker rejects functions that can fall through
                # without returning a value, so this path is defensive only.
                # Always return a defined value: the compiler must never
                # introduce undef.
                if retType.isLoadable():
                    self.builder.ret(ir.Constant(retType.toLLVM(), 0))
                else:
                    self.builder.ret(ir.Constant(retType.toLLVM(), None))

        self.fn = was_fn
        self.builder = was_builder

        return fn
    
    def evalFor(self, node: CheckedForNode):
        self.symbols.append(dict())

        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)
        vars = list[str]()

        # Allocate the iterator variables
        # that's x in this example: for x in myList:
        for name, t in node.vars:
            alloc = self.builder.alloca(t.toLLVM())
            self.symbols[-1][name] = LLVMSymbol(t, alloc)
            vars.append(name)

        # Evaluate the list
        iter = self.evalNode(node.iter, True)

        # Allocate an index to keep track of while iterating
        idx = self.builder.alloca(ir.IntType(REGISTER_SIZE))
        self.builder.store(self.getConstant(0), idx)

        # Make a block for the iterating condition (idx < myList.count)
        condB = self.fn.append_basic_block('cond')
        # Make a block for the body of the for loop
        bodyB = self.fn.append_basic_block('for-body')
        # Make a block that increments idx; `continue` jumps here
        nextB = self.fn.append_basic_block('for-next')
        # Make a block for the orelse of the for loop
        orelseB = self.fn.append_basic_block('orelse-body')
        # Make a block for after the loop is done
        afterB = self.fn.append_basic_block('after-for')

        # Jump to the condition block
        self.builder.branch(condB)

        # Write the condition for the loop
        self.builder.position_at_end(condB)
        i = self.builder.load(idx)
        countPtr = self.builder.gep(iter, [zero, zero])
        count = self.builder.load(countPtr)
        cond = self.builder.icmp_signed('<', i, count)
        self.builder.cbranch(cond, bodyB, orelseB)

        # Write the body of the loop
        self.builder.position_at_end(bodyB)

        # First, evaluate the vars
        # for right now only 1 is supported
        assert len(vars) == 1
        data = self.builder.gep(iter, [zero, one])
        data = self.builder.load(data)
        elem = self.builder.gep(data, [i])
        sym = self.symbols[-1][vars[0]]
        if sym.t.isLoadable():
            elem = self.builder.load(elem)
        self.doStore(sym.ptr, elem, sym.t)

        # Evaluate the body
        # if it's not terminated go to the increment block
        self.breakBlock.append(afterB)
        self.continueBlock.append(nextB)
        for stmt in node.body:
            self.evalNode(stmt, False)
        if not self.isTerminated():
            self.builder.branch(nextB)
        self.continueBlock.pop()
        self.breakBlock.pop()

        # Increment idx and go to the condition
        self.builder.position_at_end(nextB)
        incr = self.builder.add(i, self.getConstant(1))
        self.builder.store(incr, idx)
        self.builder.branch(condB)

        # Pop the symbol scope for the for loop
        self.symbols.pop()

        # Do the orelse block
        self.evalBody(node.orelse, orelseB, afterB)

        self.builder.position_at_end(afterB)

    
    def evalWhile(self, node: CheckedIfNode) -> None:
        
        # Create a condition branch
        # Jump to it and evaluate the condition
        # If it's true go to the loop body
        # At thend of the loop body jump back to the condition branch
        cond = self.fn.append_basic_block('cond')
        self.builder.branch(cond)
        self.builder.position_at_end(cond)
        expr = self.evalNode(node.expr, False)

        fortrue = self.fn.append_basic_block('for-true')
        orelse = self.fn.append_basic_block('or-else')
        after = self.fn.append_basic_block('after')
        self.builder.cbranch(expr, fortrue, orelse)

        self.breakBlock.append(after)
        self.continueBlock.append(cond)
        self.evalBody(node.iftrue, fortrue, cond)
        self.continueBlock.pop()
        self.breakBlock.pop()

        self.evalBody(node.orelse, orelse, after)
        self.builder.position_at_end(after)

    def getConstant(self, num: int, size=REGISTER_SIZE):
        return ir.Constant(ir.IntType(size), num)

    def doBinOp(self, t: Class, left: ir.Value, right: ir.Value, bop: Any):
        if isinstance(bop, ast.cmpop):
            op = ''
            match bop:
                case ast.Eq() | ast.Is():
                    op = '=='
                case ast.NotEq() | ast.IsNot():
                    op = '!='
                case ast.Lt():
                    op = '<'
                case ast.LtE():
                    op = '<='
                case ast.Gt():
                    op = '>'
                case ast.GtE():
                    op = '>='
                case _:
                    print(bop)
                    assert False
            
            if t.isFloat():
                return self.builder.fcmp_ordered(op, left, right)
            else:
                return self.builder.icmp_signed(op, left, right)

        
        ops = {
            ast.Add: (self.builder.fadd, self.builder.add),
            ast.Sub: (self.builder.fsub, self.builder.sub),
            ast.Mult: (self.builder.fmul, self.builder.mul),
            ast.Div: (self.builder.fdiv, self.builder.sdiv),
            ast.Mod: (self.builder.frem, self.builder.srem)
        }

        if t.isInteger():
            match bop:
                case ast.Div() | ast.Mod():
                    return self.doIntDivRem(left, right, isinstance(bop, ast.Mod))
                case ast.FloorDiv():
                    return self.doIntFloorDiv(left, right)
                case ast.Pow():
                    # The runtime works on int; narrow results are the same
                    # value modulo 2^bits.
                    intT = ir.IntType(REGISTER_SIZE)
                    res = self.builder.call(self.symbols[0]['pyr_ipow'].ptr,
                                            [self.doIntResize(left, intT),
                                             self.doIntResize(right, intT)])
                    return self.doIntResize(res, left.type)
                case ast.BitAnd():
                    return self.builder.and_(left, right)
                case ast.BitOr():
                    return self.builder.or_(left, right)
                case ast.BitXor():
                    return self.builder.xor(left, right)
                case ast.LShift() | ast.RShift():
                    # Take the count modulo the width; for int that is the
                    # low 5 bits, as RISC-V sll/sra use. This also keeps LLVM
                    # shl/ashr defined: they are poison for counts >= the
                    # width. For int the mask costs nothing on RISC-V, where
                    # the shift instructions ignore the upper bits anyway.
                    count = self.builder.and_(right, ir.Constant(right.type, right.type.width - 1))
                    if isinstance(bop, ast.LShift):
                        return self.builder.shl(left, count)
                    return self.builder.ashr(left, count)

        got = ops.get(type(bop))
        if got is None:
            print(f'Invalid binary operator {bop}')
            assert False

        fn = got[0 if t.isFloat() else 1]
        result = fn(left, right)
        assert result is not None
        return result

    def doIntDivRem(self, left: ir.Value, right: ir.Value, isRem: bool) -> ir.Value:
        # LLVM sdiv/srem are undefined behavior for a zero divisor and for
        # INT_MIN / -1, and the optimizer may exploit that. Divide by a safe
        # divisor instead and select the RISC-V M-extension results:
        # x / 0 == -1, x % 0 == x, INT_MIN / -1 == INT_MIN, INT_MIN % -1 == 0.
        # The guards fold away when the divisor is a constant.
        intT = left.type
        minusOne = ir.Constant(intT, -1)
        intMin = ir.Constant(intT, -(1 << (intT.width - 1)))
        isZero = self.builder.icmp_signed('==', right, ir.Constant(intT, 0))
        isOverflow = self.builder.and_(
            self.builder.icmp_signed('==', left, intMin),
            self.builder.icmp_signed('==', right, minusOne))
        safeRight = self.builder.select(
            self.builder.or_(isZero, isOverflow), ir.Constant(intT, 1), right)
        if isRem:
            return self.builder.select(isZero, left, self.builder.srem(left, safeRight))
        return self.builder.select(isZero, minusOne, self.builder.sdiv(left, safeRight))

    def doIntFloorDiv(self, left: ir.Value, right: ir.Value) -> ir.Value:
        # CPython floor division on top of the total truncating division:
        # step the quotient down when the remainder is non-zero and its sign
        # differs from the divisor's. x // 0 == -1, as for `/`.
        zero = ir.Constant(left.type, 0)
        quot = self.doIntDivRem(left, right, False)
        rem = self.doIntDivRem(left, right, True)
        adjust = self.builder.and_(
            self.builder.and_(self.builder.icmp_signed('!=', rem, zero),
                              self.builder.icmp_signed('!=', right, zero)),
            self.builder.icmp_signed('<', self.builder.xor(rem, right), zero))
        return self.builder.sub(quot, self.builder.zext(adjust, left.type))

    def evalUnaryOp(self, node: CheckedUnaryOpNode) -> ir.Value:
        val = self.evalNode(node.operand, False)
        assert val is not None

        match node.op:
            case ast.USub():
                if node.t.isFloat():
                    return self.builder.fneg(val)
                return self.builder.neg(val)
            case ast.UAdd():
                return val
            case ast.Not() | ast.Invert():
                # xor with all ones: logical not on i1, bitwise ~ on i32
                return self.builder.not_(val)
            case _:
                raise CompileError(f'Unsupported unary operator {type(node.op).__name__}')

    def evalBinOp(self, node: CheckedBinNode, lhs: bool) -> ir.Value:
        left = self.evalNode(node.left, False)
        right = self.evalNode(node.right, False)
        assert left is not None
        assert right is not None
        assert lhs != True

        # String concatenation: str + str
        if isinstance(node.op, ast.Add) and node.left.t.name == 'str':
            return self.doStringConcat(left, right)
        # String repetition: str * int or int * str
        if isinstance(node.op, ast.Mult):
            if node.left.t.name == 'str' and node.right.t.isInt():
                return self.doStringRepeat(left, right)
            if node.left.t.isInt() and node.right.t.name == 'str':
                return self.doStringRepeat(right, left)
        
        return self.doBinOp(node.left.t, left, right, node.op)
        

    def declareSymbol(self, name: str, t: Class) -> ir.Value:
        """Allocate storage for a newly-declared variable.

        At module scope (only the global builtins frame is on the symbol
        stack) the storage must be a LLVM global so function bodies can
        reach it.  Inside functions we still use stack allocas.
        """
        if len(self.symbols) == 1:
            llvmT = t.toLLVM()
            globalV = ir.GlobalVariable(self.module, llvmT, name=f'g_{name}')
            globalV.linkage = 'internal'
            globalV.initializer = ir.Constant(llvmT, None)
            self.symbols[-1][name] = LLVMSymbol(t, globalV)
            return globalV
        alloc = self.builder.alloca(t.toLLVM())
        self.symbols[-1][name] = LLVMSymbol(t, alloc)
        return alloc

    def evalAssign(self, node: CheckedAssignNode, lhs: bool) -> ir.Value:
        for sym in node.declaredSymbols:
            self.declareSymbol(sym, node.t)

        ptr = self.evalNode(node.left, True)
        val = self.evalNode(node.right, False)
        assert ptr is not None
        assert val is not None
        self.doStore(ptr, val, node.right.t)
        if lhs:
            return ptr
        return val
    
    def evalAugAssign(self, node: CheckedBinNode, lhs: bool) -> ir.Value:
        # Evaluate the lvalue exactly once to obtain its address, then
        # load the old value from that same address. Re-evaluating
        # `node.left` for the read would re-run any side effects in the
        # subscript/attribute path (e.g. `a[f()] += x` would call `f`
        # twice and store at the wrong index on the second call).
        assign = self.evalNode(node.left, True)
        right = self.evalNode(node.right, False)
        assert assign is not None
        assert right is not None
        if node.left.t.isLoadable():
            left = self.builder.load(assign)
        else:
            # For non-loadable types (lists/strings) the assignment
            # itself is a memcpy of the whole descriptor; the 'old
            # value' is just the source descriptor from the right-hand
            # side of the binop.
            left = right
        result = self.doBinOp(node.left.t, left, right, node.op)
        self.doStore(assign, result, node.left.t)
        if lhs:
            return assign
        else:
            return result
    
    def doPrint(self, args: List[CheckedNode], newLine = True) -> ir.Value:
        printString = ''
        for i, arg in enumerate(args):
            if i != 0:
                printString += ' '
            printString += arg.t.getPrintfFormat()

        if newLine:
            printString += '\n'

        toPass = []
        # format
        toPass.append(self.getConstString(printString))
        for arg in args:
            evaled = self.evalNode(arg, False)
            assert evaled is not None
            passed = arg.t.getPrintfArgument(self.builder, evaled)
            toPass.append(passed)
        
        # Probably shouldn't return the value but I don't think it matters
        return self.builder.call(self.symbols[0]['printf'].ptr, toPass, 'print')
        
    def doLen(self, args: list[CheckedNode]) -> ir.Value:
        assert len(args) == 1
        assert isinstance(args[0].t, ListType)
        ptr = self.evalNode(args[0], True)
        zero = self.getConstant(0, 32)
        lenPtr = self.builder.gep(ptr, [zero, zero])
        return self.builder.load(lenPtr)

    def doInput(self, args: list[CheckedNode], strT: Class) -> ir.Value:
        assert len(args) == 1

        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)
        promptStr = self.evalNode(args[0], False)
        dataPtr = self.builder.gep(promptStr, [zero, one])
        prompt = self.builder.load(dataPtr)

        readln = self.symbols[0]['readln']
        strPtr = self.builder.call(readln.ptr, [prompt])

        strlen = self.symbols[0]['strlen'].ptr
        length = self.builder.call(strlen, [strPtr])
        return self.makeStringFromPointer(strPtr, length, strT.toLLVM())

    def doRange(self, args: list[CheckedNode], retType: Class) -> ir.Value:
        assert len(args) == 1
        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)

        count = self.evalNode(args[0], False)
        elems = self.heapAlloc(intType().toLLVM(), count)
        idx =  self.builder.alloca(intType().toLLVM())
        self.builder.store(self.getConstant(0), idx)

        condB = self.fn.append_basic_block('cond')
        bodyB = self.fn.append_basic_block('range-body')
        afterB = self.fn.append_basic_block('after')
        self.builder.branch(condB)

        self.builder.position_at_end(condB)
        i = self.builder.load(idx)
        cond = self.builder.icmp_signed('<', i, count)
        self.builder.cbranch(cond, bodyB, afterB)

        self.builder.position_at_end(bodyB)
        elem = self.builder.gep(elems, [i])
        self.builder.store(i, elem)
        newI = self.builder.add(i, self.getConstant(1))
        self.builder.store(newI, idx)
        self.builder.branch(condB)

        self.builder.position_at_end(afterB)
        result = self.builder.alloca(retType.toLLVM())
        countPtr = self.builder.gep(result, [zero, zero])
        dataPtr = self.builder.gep(result, [zero, one])
        self.builder.store(count, countPtr)
        self.builder.store(elems, dataPtr)

        return result
    
    def doFloatCast(self, args: list[CheckedNode], retType: Class) -> ir.Value:
        assert len(args) == 1
        val = self.evalNode(args[0], False)

        return self.builder.sitofp(val, retType.toLLVM())
    
    def doIntCast(self, args: list[CheckedNode], retType: Class) -> ir.Value:
        """int(x), int8(x), int16(x)."""
        assert len(args) == 1
        val = self.evalNode(args[0], False)
        if args[0].t.isFloat():
            return self.builder.fptosi(val, retType.toLLVM())
        return self.doIntResize(val, retType.toLLVM())

    def doIntResize(self, val: ir.Value, intT: ir.IntType) -> ir.Value:
        """Sign-extend or wrap an integer to another width."""
        if val.type.width < intT.width:
            return self.builder.sext(val, intT)
        if val.type.width > intT.width:
            return self.builder.trunc(val, intT)
        return val

    def doStr(self, args: list[CheckedNode]) -> ir.Value:
        """str(int) or str(float): convert to string via sprintf."""
        assert len(args) == 1
        arg = args[0]
        val = self.evalNode(arg, False)
        from .type import ListType, Class
        str_t = ListType("str", Class('char'))
        strT = str_t.toLLVM()
        if arg.t.isInteger():
            # Max i32 is -2147483648 (11 chars + NUL = 12 bytes)
            buf = self.heapAlloc(ir.IntType(8), 12)
            fmt = self.getConstString('%d')
            val = arg.t.getPrintfArgument(self.builder, val)
            self.builder.call(self.symbols[0]['sprintf'].ptr, [buf, fmt, val])
            strlen = self.symbols[0]['strlen'].ptr
            length = self.builder.call(strlen, [buf])
            return self.makeStringFromPointer(buf, length, strT)
        if arg.t.isFloat():
            # Plenty of room for a double formatted with %f
            buf = self.heapAlloc(ir.IntType(8), 64)
            fmt = self.getConstString('%f')
            self.builder.call(self.symbols[0]['sprintf'].ptr, [buf, fmt, val])
            strlen = self.symbols[0]['strlen'].ptr
            length = self.builder.call(strlen, [buf])
            return self.makeStringFromPointer(buf, length, strT)
        raise CompileError(f'str() argument must be int or float, got {arg.t}')

    def doBoolCast(self, args: list[CheckedNode]) -> ir.Value:
        """bool(int): 0 -> False, non-zero -> True."""
        assert len(args) == 1
        val = self.evalNode(args[0], False)
        # icmp returns i1, which is the LLVM representation of bool
        return self.builder.icmp_signed('!=', val, ir.Constant(val.type, 0))

    def doAbs(self, args: list[CheckedNode]) -> ir.Value:
        """abs(int) or abs(float)."""
        assert len(args) == 1
        arg = args[0]
        val = self.evalNode(arg, False)
        if arg.t.isInteger():
            zero = ir.Constant(val.type, 0)
            neg = self.builder.sub(zero, val)
            is_neg = self.builder.icmp_signed('<', val, zero)
            return self.builder.select(is_neg, neg, val)
        if arg.t.isFloat():
            zero = ir.Constant(ir.DoubleType(), 0.0)
            neg = self.builder.fsub(zero, val)
            is_neg = self.builder.fcmp_ordered('<', val, zero)
            # NOTE: llc-14 miscompiles `select` on f64 for RV32 soft-float,
            # so floats use an explicit branch instead.
            return self._branchSelect(is_neg, neg, val, ir.DoubleType())
        raise CompileError(f'abs() argument must be int or float, got {arg.t}')

    def _branchSelect(self, cond: ir.Value, trueVal: ir.Value, falseVal: ir.Value, llvmT: ir.Type) -> ir.Value:
        """Select implemented with a branch + alloca instead of the LLVM
        `select` instruction, which llc-14 miscompiles on f64 for RV32."""
        tmp = self.builder.alloca(llvmT)
        self.builder.store(falseVal, tmp)
        with self.builder.if_then(cond):
            self.builder.store(trueVal, tmp)
        return self.builder.load(tmp)

    def doMinMax(self, args: list[CheckedNode], is_max: bool) -> ir.Value:
        """min(int, int), max(int, int), min(float, float), max(float, float)."""
        assert len(args) == 2
        a = self.evalNode(args[0], False)
        b = self.evalNode(args[1], False)
        if args[0].t.isInteger() and args[1].t.isInteger():
            op = '>' if is_max else '<'
            cond = self.builder.icmp_signed(op, a, b)
            return self.builder.select(cond, a, b)
        if args[0].t.isFloat() and args[1].t.isFloat():
            op = 'ogt' if is_max else 'olt'
            cond = self.builder.fcmp_ordered(op, a, b)
            # NOTE: no `select` on f64 (see _branchSelect)
            return self._branchSelect(cond, a, b, ir.DoubleType())
        raise CompileError(f'min/max arguments must both be int or both float')

    def evalCall(self, node: CheckedCallNode, lhs: bool) -> ir.Value:
        match node.fnName:
            case 'print':
                return self.doPrint(node.args)
            case 'len':
                return self.doLen(node.args)
            case 'input':
                return self.doInput(node.args, node.fnType.asCallable().returnType)
            case 'range':
                return self.doRange(node.args, node.fnType.asCallable().returnType)
            case 'float':
                return self.doFloatCast(node.args, node.fnType.asCallable().returnType)
            case 'int' | 'int8' | 'int16':
                return self.doIntCast(node.args, node.fnType.asCallable().returnType)
            case 'str':
                return self.doStr(node.args)
            case 'bool':
                return self.doBoolCast(node.args)
            case 'abs':
                return self.doAbs(node.args)
            case 'min':
                return self.doMinMax(node.args, is_max=False)
            case 'max':
                return self.doMinMax(node.args, is_max=True)

            case None:
                pass

            case _:
                print(f'Unimplemented intrinsic {node.fnName}')
                assert False

            
        operand = self.evalNode(node.operand, True)
        args = []
        for arg in node.args:
            args.append(self.evalNode(arg, False))
        
        return self.builder.call(operand, args)

    def makeStringFromPointer(self, ptr: ir.Value, count: ir.Value | int, llvmT: ir.Type) -> ir.Value:
        zero = self.getConstant(0, 32)
        one = self.getConstant(1, 32)
        strAlloc = self.builder.alloca(llvmT)
        countPtr = self.builder.gep(strAlloc, [zero, zero])
        dataPtr = self.builder.gep(strAlloc,  [zero, one])
        if type(count) is int:
            count = self.getConstant(count)

        self.builder.store(count, countPtr)
        self.builder.store(ptr, dataPtr)
        return strAlloc
    
    def evalConstant(self, node: CheckedNode) -> ir.Value:
        assert isinstance(node.base, ast.Constant)
        base: ast.Constant
        base = node.base
        if node.t.name == 'str':
            assert type(base.value) == str
            llvmT = node.t.toLLVM()
            globalV = self.getConstString(base.value)
            return self.makeStringFromPointer(globalV, len(base.value), llvmT)
        else:
            return ir.Constant(node.t.toLLVM(), base.value)
    
    def evalExpr(self, node: CheckedNode, lhs: bool) -> ir.Value | None:
        # The type checker unwraps ast.Expr, so this node kind should never
        # reach the backend. Fail with a clear diagnostic instead of a bare
        # assertion if it ever does.
        raise CompileError(f'Internal error: expression statement reached the backend: {ast.dump(node.base)}')
    
    def evalName(self, node: CheckedNode, lhs: bool) -> ir.Value:
        assert isinstance(node.base, ast.Name)
        base: ast.Name
        base = node.base
        ptr = self.findSymbol(base.id).ptr
        if lhs:
            return ptr
        else:
            if node.t.isLoadable():
                return self.builder.load(ptr)
            return ptr
    
    def findSymbol(self, name: str) -> LLVMSymbol:
        for scope in reversed(self.symbols):
            if name in scope:
                return scope[name]
            
        for scope in reversed(self.symbols):
            if self.makeFnName(name) in scope:
                return scope[self.makeFnName(name)]
        
        assert False
