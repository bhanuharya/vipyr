from typing import List
from .error import raiseError, ErrorInfo
import ast
from .checked_nodes import *


# Binary operators defined only for int operands (see docs/language-spec.md).
INT_ONLY_OPS = {
    ast.FloorDiv: '//',
    ast.Pow: '**',
    ast.BitAnd: '&',
    ast.BitOr: '|',
    ast.BitXor: '^',
    ast.LShift: '<<',
    ast.RShift: '>>',
}

"""
The TypeChecker class goes through Python's AST (that we get from calling ast.parse)
and assigns a type to each node, if there is an invalid/incompattible type or an unsupported node we raise an error and quit
"""
# Builtins whose result type depends on the argument types.
POLYMORPHIC_BUILTINS = ('abs', 'min', 'max', 'str', 'bool', 'int', 'float', 'int8', 'int16')


def literalInt(node: ast.AST) -> int | None:
    """The value of an integer literal, optionally negated, else None."""
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return sign * node.value
    return None


class TypeChecker():
    types: dict[str, Class]
    symbols: List[dict[str, Class]]
    intrinsics: set[str]
    errorInfo: ErrorInfo
    root: CheckedNode
    tail: CheckedNode
    currentFnReturn: Class
    breakableCtx: bool
    bodyTerminated: bool
    def __init__(self, fName: str):
        self.types = {}
        self.symbols = [dict[str, Class]()]
        self.errorInfo = ErrorInfo(0, fName)
        self.root = CheckedNode(None, noneType()) # type: ignore
        self.tail = self.root
        self.currentFnReturn = noneType()
        self.breakableCtx = False
        self.bodyTerminated = False


        # Create all the base types
        # currently methods and custom types are not supported so this is more future-proofing
        obj = Class('object')
        obj.methods['__init__'] = defaultInit()

        intType = Class("int")
        intType.methods['__init__'] = defaultInit()
        intType.parent = obj

        floatType = Class("float")
        floatType.methods['__init__'] = defaultInit()
        floatType.parent = obj

        narrowInts = list[Class]()
        for name in ('int8', 'int16'):
            narrow = Class(name)
            narrow.methods['__init__'] = defaultInit()
            narrow.parent = obj
            narrowInts.append(narrow)

        genericSlice = ListType('__generic__', objectType())
        genericSlice.methods['__init__'] = defaultInit()
        genericSlice.parent = obj

        charType = Class('char')
        charType.methods['__init__'] = defaultInit()
        charType.parent = obj

        stringType = ListType("str", charType)
        stringType.methods['__init__'] = defaultInit()
        stringType.parent = genericSlice

        boolType = BoolType()
        boolType.methods['__init__'] = defaultInit()
        boolType.parent = obj
        
        # Base types
        self.types['object'] = obj
        self.types['int'] = intType
        self.types['float'] = floatType
        for narrow in narrowInts:
            self.types[narrow.name] = narrow
        self.types['str'] = stringType
        self.types['bool'] = boolType
        self.types['<None>'] = noneType()

        # Global builtin symbols
        self.symbols[0]['len'] = FnType([genericSlice], intType)
        self.symbols[0]['print'] = FnType([objectType()], noneType())
        self.symbols[0]['input'] = FnType([objectType()], stringType)
        self.symbols[0]['range'] = FnType([intType], ListType(None, intType))

        # Conversions
        self.symbols[0]['float'] = FnType([intType], floatType)
        self.symbols[0]['int'] = FnType([floatType], intType)
        self.symbols[0]['str'] = FnType([objectType()], stringType)
        self.symbols[0]['bool'] = FnType([intType], boolType)
        for narrow in narrowInts:
            self.symbols[0][narrow.name] = FnType([intType], narrow)

        # Numeric
        self.symbols[0]['abs'] = FnType([objectType()], intType)
        self.symbols[0]['min'] = FnType([objectType(), objectType()], intType)
        self.symbols[0]['max'] = FnType([objectType(), objectType()], intType)

        # Each of these functions have a seperate implementation in the backend
        # and are not just normal calls
        self.intrinsics = set()
        self.intrinsics.add('print')
        self.intrinsics.add('input')
        self.intrinsics.add('len')
        self.intrinsics.add('range')
        self.intrinsics.add('float')
        self.intrinsics.add('int')
        self.intrinsics.add('str')
        self.intrinsics.add('bool')
        self.intrinsics.add('abs')
        self.intrinsics.add('min')
        self.intrinsics.add('max')
        self.intrinsics.add('int8')
        self.intrinsics.add('int16')

    def findSymbol(self, name: str, errroIfNotFound=True) -> Class | None:
        """
        Loop through the scopes in reverse looking for an identifier
        it's done in reverse so it always gets the innermost symbol
        but this currently doesn't matter because symbol redifinition is not allowed
        might want to think about allowing it, for example:
            a = 10
            def main():
                a = "string" # different symbol than global a
                print(a) # prints string
        """
        for scope in reversed(self.symbols):
            if name in scope:
                return scope[name]
        
        if errroIfNotFound:
            raiseError(self.errorInfo, f'Undefined identifier {name}')

        return None

    def evalNode(self, node: ast.AST) -> CheckedNode:
        """
        This is the base function for type-checking a node
        it checks the node's type and goes to the appropriate function for it
        """
        match node:
            case ast.Module():
                statements = list[CheckedNode]()
                for stmt in node.body:
                    statements.append(self.evalNode(stmt))
                return CheckedModuleNode(node, statements)
            case ast.BinOp():
                return self.evalBinOp(node)
            case ast.UnaryOp():
                return self.evalUnaryOp(node)
            case ast.Expr():
                return self.evalExpr(node)
            case ast.Assign():
                return self.evalAssign(node)
            case ast.AnnAssign():
                return self.evalAnnAssign(node)
            case ast.Constant():
                return self.evalConstant(node)
            case ast.AugAssign():
                return self.evalAugAssign(node)
            case ast.Call():
                return self.evalCall(node)
            case ast.Name():
                return self.evalName(node)
            case ast.FunctionDef():
                return self.evalFnDef(node)
            case ast.Return():
                return self.evalReturn(node)
            case ast.If():
                return self.evalIf(node)
            case ast.Compare():
                return self.evalCompare(node)
            case ast.While():
                return self.evalWhile(node)
            case ast.For():
                return self.evalFor(node)
            case ast.Break():
                return self.evalBreak(node)
            case ast.Continue():
                return self.evalContinue(node)
            case ast.Pass():
                return CheckedNode(node, noneType())
            case ast.BoolOp():
                return self.evalBoolOp(node)
            case ast.List():
                return self.evalList(node)
            case ast.Subscript():
                return self.evalSubscript(node)
            case _:
                print(f'Unknown node type: {node}')
                assert False

    def evalSubscript(self, node: ast.Subscript) -> CheckedSubscriptNode:
        """
        Indexing a list:
            my_list[10]
            Subscript Node:
                value=Name(id='my_list'),
                slice=Constant(value=10)
        """
        op = self.evalNode(node.value)
        self.errorInfo.lineNo = node.lineno
        if not op.t.isList():
            raiseError(self.errorInfo, f'operand of subscript is not a list, it is of type {op.t}')

        idx = self.evalNode(node.slice)
        if not idx.t.isInt():
            raiseError(self.errorInfo, f'subscript index is not an integer, it is of type {idx.t}')

        return CheckedSubscriptNode(node, op, idx)



    def evalList(self, node: ast.List) -> CheckedListNode:
        """
        Creating a list:
            [1, 2, 3]
            List Node:
                elts=[
                        Constant(value=1),
                        Constant(value=2),
                        Constant(value=3)]
        """
        vals = list[CheckedNode]()
        for v in node.elts:
            vals.append(self.evalNode(v))

        if len(vals) == 0:
            return CheckedListNode(node, noneType(), vals)

        self.errorInfo.lineNo = node.lineno
        narrow = next((v.t for v in vals if v.t.isNarrowInt()), None)
        if narrow is not None:
            for v in vals:
                self.coerceLiterals(v, narrow)
        t = vals[0].t
        for i in range(1, len(vals)):
            if not t.isAssignable(vals[i].t):
                raiseError(self.errorInfo, f'Element of type {vals[i].t} not allowed in list of type {t}')

        return CheckedListNode(node, t, vals)


    def evalBoolOp(self, node: ast.BoolOp):
        """
        Boolean op, and/or:
            x or y
            BoolOp Node:
                op=Or(),
                values=[
                    Name(id='x'),
                    Name(id='y')]
        """
        values = []
        self.errorInfo.lineNo = node.lineno
        for v in node.values:
            evaled = self.evalNode(v)
            if not evaled.t.isBool():
                raiseError(self.errorInfo, f'Expected a boolean type, got {evaled.t}')
            
            values.append(evaled)
        
        return CheckedBoolOpNode(node, values, 'and' if type(node.op) is ast.And else 'or')

    def evalBody(self, body: list[ast.AST] | list[ast.stmt]) -> list[CheckedNode]:
        """
        Helper function for evaluating inner body of a node,
        for example:
            if x:
                # body starts
                do_something()
                # body ends
            return
        """
        res = []

        self.bodyTerminated = False
        self.symbols.append(dict())
        for stmt in body:
            res.append(self.evalNode(stmt))
            if self.bodyTerminated:
                break
        self.symbols.pop()

        return res

    def evalBreak(self, node: ast.Break):
        """
        Break Node:
            Doesn't contain any information
            just check if it's in a valid location to break
        """
        self.errorInfo.lineNo = node.lineno
        if self.breakableCtx is False:
            raiseError(self.errorInfo, f'break outside of a loop is not valid')

        self.bodyTerminated = True
        return CheckedNode(node, noneType())

    def evalContinue(self, node: ast.Continue):
        """
        Continue Node:
            Like break, only valid inside a loop
        """
        self.errorInfo.lineNo = node.lineno
        if self.breakableCtx is False:
            raiseError(self.errorInfo, f'continue outside of a loop is not valid')

        self.bodyTerminated = True
        return CheckedNode(node, noneType())

    def checkIntOnlyOp(self, op, left: Class, right: Class):
        symbol = INT_ONLY_OPS.get(type(op))
        if symbol is not None and not (left.isInteger() and right.isInteger()):
            raiseError(self.errorInfo, f'Operator {symbol} requires integer operands, got {left} and {right}')

    def evalWhile(self, node: ast.While) -> CheckedIfNode:
        """
        While Node:
            while x:
                ...
            else:
                ...
            test=Name(x)
            body=...
            orelse=...
        """
        expr = self.evalNode(node.test)

        self.errorInfo.lineNo = node.lineno
        if not expr.t.isBool():
            raiseError(self.errorInfo, f'Condition is of non boolean type {expr.t}')

        wasBreakable = self.breakableCtx
        self.breakableCtx = True
        iftrue = self.evalBody(node.body)
        self.breakableCtx = wasBreakable

        orelse = self.evalBody(node.orelse)

        # A while loop may run zero iterations and 'break' continues
        # after the loop, so control flow never provably terminates here.
        self.bodyTerminated = False

        return CheckedIfNode(node, expr, iftrue, orelse)

    def evalFor(self, node: ast.For) -> CheckedForNode:
        """
        For Node:
            for x in y:
                ...
            else:
                ...
            target=Name(x)
            iter=Name(y)
            body=...
            orelse=...
        """
        
        self.errorInfo.lineNo = node.lineno
        if type(node.target) is not ast.Name:
            raiseError(self.errorInfo, f'Unknown iterator variable kind {node.target}')
            return CheckedForNode(node, [], CheckedNode(node, noneType()), [], [])

        id = node.target.id
        if self.findSymbol(id, False) is not None:
            raiseError(self.errorInfo, f'Redifinition of {id} as iterator variable')
            return CheckedForNode(node, [], CheckedNode(node, noneType()), [], [])

        
        it = self.evalNode(node.iter)
        if not it.t.isIterable():
            raiseError(self.errorInfo, f'Cannot iterate over expression of type {it.t}')
            return CheckedForNode(node, [], CheckedNode(node, noneType()), [], [])

        self.symbols.append(dict())
        self.symbols[-1][id] = it.t.getIteratorType()


        wasBreakable = self.breakableCtx
        self.breakableCtx = True

        fortrue = self.evalBody(node.body)

        self.breakableCtx = wasBreakable


        self.symbols.pop()

        orelse = self.evalBody(node.orelse)

        # A for loop may run zero iterations, so control flow never
        # provably terminates here.
        self.bodyTerminated = False

        return CheckedForNode(node, [(id, it.t.getIteratorType())], it, fortrue, orelse)
        

    def evalCompare(self, node: ast.Compare) -> CheckedBinNode:
        """
        Currently we only support single comparison
        so chaining complex comps is not allowed
        Compare Node:
            a < 10
            left=Name(a)
            ops=[
                    Lt()] # Less than
            comperators=[
                    Constant(value=10)]
        """
        
        self.errorInfo.lineNo = node.lineno
        if len(node.comparators) != 1:
            raiseError(self.errorInfo, 'Multiple comperators are not supported')
        left = self.evalNode(node.left)
        right = self.evalNode(node.comparators[0])
        self.coerceLiterals(right, left.t)
        self.coerceLiterals(left, right.t)
        op = node.ops[0]
        
        self.errorInfo.lineNo = node.lineno
        if not left.t.canPerformBinOp(right.t, op):
            raiseError(self.errorInfo, f'Cannot perform binary comparison with types {left.t} and {right.t}')

        binNode = CheckedBinNode(node, left, right, op)
        binNode.t = self.types['bool']
        return binNode

    def evalIf(self, node: ast.If) -> CheckedIfNode:
        """
        Same as while
        """
        expr = self.evalNode(node.test)
        self.errorInfo.lineNo = node.lineno
        if not expr.t.isBool():
            raiseError(self.errorInfo, f'if expression of type {expr.t} is not a boolean')

        iftrue = self.evalBody(node.body)
        trueTerminated = self.bodyTerminated
        orelse = self.evalBody(node.orelse)
        elseTerminated = self.bodyTerminated

        # The statement sequence after an if only provably terminates
        # when both branches exist and both terminate.
        self.bodyTerminated = trueTerminated and elseTerminated

        return CheckedIfNode(node, expr, iftrue, orelse)


    def evalReturn(self, node: ast.Return) -> CheckedReturnNode:
        """
        return 4
        Return Node:
            value=Constant(value=4)
        """
        self.errorInfo.lineNo = node.lineno
        if(node.value is None):
            if(self.currentFnReturn.name != '<None>'):
                raiseError(self.errorInfo, f'Empty return is not allowed, function returns type {self.currentFnReturn}')
            return CheckedReturnNode(node, None)
        val = self.evalNode(node.value)
        self.coerceLiterals(val, self.currentFnReturn)
        if not self.currentFnReturn.isAssignable(val.t):
            raiseError(self.errorInfo, f'Cannot return expression of type {val.t} in function that returns {self.currentFnReturn}')
        
        self.bodyTerminated = True
        return CheckedReturnNode(node, val)

    def evalType(self, node: ast.expr) -> Class:
        """
        Get a type from a type node:
        int => intType()
        list[int] => List(intType())
        """
        self.errorInfo.lineNo = node.lineno
        match node:
            case ast.Name():
                if node.id not in self.types:
                    raiseError(self.errorInfo, f'Undefined type {node.id}')
                    return noneType()
                return self.types[node.id]
            case ast.Constant():
                # `def f() -> None:` parses `None` as ast.Constant(None).
                if node.value is None:
                    return noneType()
                raiseError(self.errorInfo, f'Unknown type node {node}')
                return noneType()
            case ast.Subscript():
                if not isinstance(node.value, ast.Name):
                    raiseError(self.errorInfo, f'Unknown subscript type {node.value}')
                    return noneType()
                if node.value.id != 'list':
                    raiseError(self.errorInfo, f'Unknown subscript type {node.value.id}')
                    return noneType()
                base = self.evalType(node.slice)
                return ListType(None, base)
            case _:
                raiseError(self.errorInfo, f'Unknown type node {node}')
                return noneType()


    def evalFnDef(self, node: ast.FunctionDef) -> CheckedFnDefNode:
        """
        def main(a: int) -> str:
            ...
        Function Definition Node:
            name=str('main')
            args=arguments(
                    args=[
                        arg(arg='a', annotation=Name(int))])
            returns=Name(str)
            body=...
        """
        retType = noneType()
        if node.returns:
            retType = self.evalType(node.returns)

        self.errorInfo.lineNo = node.lineno

        # Push up a symbol scope for the arguments
        self.symbols.append(dict())
        args = node.args

        # Name and type of each argument are stored here
        arg_defines = list[tuple[str, Class]]()
        # Just the types of the arguments are store here,
        # used to make the function type
        arg_types = list[Class]()
        for arg in args.args:
            if arg.annotation is None:
                raiseError(self.errorInfo, f'Every argument needs to have a type annotation, {arg.arg} does not')
            else:
                t = self.evalType(arg.annotation)
                if t is None:
                    raiseError(self.errorInfo, f'Unknown type {arg.type_comment}')
                if arg.arg in self.symbols[-1]:
                    raiseError(self.errorInfo, f'Redifinition of variable {arg.arg} as function argument')
                
                arg_types.append(t)
                arg_defines.append((arg.arg, t))
                self.symbols[-1][arg.arg] = t

        fnType = FnType(arg_types, retType)
        self.symbols[-2][node.name] = fnType

        statements = list[CheckedNode]()

        wasReturn = self.currentFnReturn
        self.currentFnReturn = retType

        statements = self.evalBody(node.body)
        terminated = self.bodyTerminated

        self.currentFnReturn = wasReturn

        self.errorInfo.lineNo = node.lineno
        if not terminated and retType.name != '<None>':
            raiseError(self.errorInfo, f'Function {node.name} does not return a value of type {retType} on every path')

        self.symbols.pop()
        return CheckedFnDefNode(node, arg_defines, statements, fnType)


    def evalName(self, node: ast.Name) -> CheckedNode:
        """
        a
        Name Node:
            id=str('a')

        Just look for the name using findSymbol
        """

        self.errorInfo.lineNo = node.lineno
        name = node.id
        sym = self.findSymbol(name)
        if sym is None:
            raiseError(self.errorInfo, f'Undefined symbol {name}')
            return CheckedNode(node, noneType())

        return CheckedNode(node, sym)

    def evalCall(self, node: ast.Call) -> CheckedNode:
        """
        Function Call

        main(10)
        Node:
            func=Name(main)
            args=[
                    Constant(value=10)]
        """
        intrinName = None
        if type(node.func) is ast.Name:
            if node.func.id in self.intrinsics:
                intrinName = node.func.id

        checked = self.evalNode(node.func)
        fnType = checked.t
        self.errorInfo.lineNo = node.lineno
        if not fnType.isCallable():
            raiseError(self.errorInfo, f'Cannot call expression of type {fnType}')

        # print is variadic: it accepts any number of arguments. The
        # backend emits a single space-separated printf call.
        if intrinName == 'print':
            argNodes = [self.evalNode(a) for a in node.args]
            return CheckedCallNode(node, checked, argNodes, fnType, intrinName)

        # abs/min/max/str/bool are polymorphic: their return type depends on
        # the argument types, so they are type-checked here instead of via
        # their registered (placeholder) FnType.
        if intrinName in POLYMORPHIC_BUILTINS:
            return self.evalPolymorphicBuiltin(node, checked, fnType, intrinName)
        
        params = fnType.asCallable().args
        gaveCount = len(node.args)
        expectCount = len(params)
        if gaveCount != expectCount:
            raiseError(self.errorInfo, f"Function expects {expectCount} arguemnt{'s' if expectCount != 1 else ''} but {gaveCount} {'were' if gaveCount != 1 else 'was'} provided")

        argNodes = list[CheckedNode]()
        for i, param in enumerate(params):
            passed = self.evalNode(node.args[i])
            self.coerceLiterals(passed, param)
            argNodes.append(passed)
            if not param.isAssignable(passed.t):
                raiseError(self.errorInfo, f'Argument #{i+1} is of type {passed.t} which is not compatible with function parameter of type {param}')
        
        return CheckedCallNode(node, checked, argNodes, fnType, intrinName)

    def evalPolymorphicBuiltin(self, node: ast.Call, checked, fnType, name: str) -> CheckedCallNode:
        """
        Type-check builtins whose result type is derived from the argument
        types and build the precise FnType for the call site.
        """
        self.errorInfo.lineNo = node.lineno
        argNodes = [self.evalNode(a) for a in node.args]

        expectCount = 2 if name in ('min', 'max') else 1
        if len(argNodes) != expectCount:
            raiseError(self.errorInfo, f"{name}() expects {expectCount} argument{'s' if expectCount != 1 else ''} but {len(argNodes)} {'were' if len(argNodes) != 1 else 'was'} provided")

        t = argNodes[0].t
        if name == 'abs':
            if not (t.isInteger() or t.isFloat()):
                raiseError(self.errorInfo, f'abs() argument must be an integer or float, got {t}')
            retT = t
        elif name == 'str':
            if not (t.isInteger() or t.isFloat()):
                raiseError(self.errorInfo, f'str() argument must be an integer or float, got {t}')
            retT = self.types['str']
        elif name == 'bool':
            if not t.isInteger():
                raiseError(self.errorInfo, f'bool() argument must be an integer, got {t}')
            retT = self.types['bool']
        elif name == 'int':
            if not (t.isInteger() or t.isFloat()):
                raiseError(self.errorInfo, f'int() argument must be an integer or float, got {t}')
            retT = self.types['int']
        elif name == 'float':
            if not t.isInteger():
                raiseError(self.errorInfo, f'float() argument must be an integer, got {t}')
            retT = self.types['float']
        elif name in ('int8', 'int16'):
            # Float to a narrow int would need its own overflow rules; go
            # through int() instead.
            if not t.isInteger():
                raiseError(self.errorInfo, f'{name}() argument must be an integer, got {t}')
            retT = self.types[name]
        else:  # min / max
            self.coerceLiterals(argNodes[1], argNodes[0].t)
            self.coerceLiterals(argNodes[0], argNodes[1].t)
            a, b = argNodes[0].t, argNodes[1].t
            if not ((a.isInteger() and a.name == b.name) or (a.isFloat() and b.isFloat())):
                raiseError(self.errorInfo, f'{name}() arguments must have the same integer type or both be float, got {a} and {b}')
            retT = a

        preciseType = FnType(args=[a.t for a in argNodes], returnType=retT)
        return CheckedCallNode(node, checked, argNodes, preciseType, name)

    def evalAugAssign(self, node: ast.AugAssign) -> CheckedNode:
        """
        x += 10
        Node:
            target=Name(x)
            op=Add()
            value=Constant(value=10)
        """
        self.errorInfo.lineNo = node.lineno
        left = self.evalNode(node.target)
        right = self.evalNode(node.value)
        self.coerceLiterals(right, left.t)
        if not left.t.isAssignable(right.t):
            raiseError(self.errorInfo, f'Cannot assign value of type {right.t} to variable of type {left.t}')
        self.checkIntOnlyOp(node.op, left.t, right.t)

        return CheckedBinNode(node, left, right, node.op)

    def evalUnaryOp(self, node: ast.UnaryOp) -> CheckedUnaryOpNode:
        """
        Unary operation:
            -x, +x, not b
        UnaryOp Node:
            op=USub()
            operand=Name(x)
        """
        self.errorInfo.lineNo = node.lineno
        operand = self.evalNode(node.operand)

        if isinstance(node.op, ast.Not):
            if not operand.t.isBool():
                raiseError(self.errorInfo, f'Operand of not is of non boolean type {operand.t}')
            return CheckedUnaryOpNode(node, operand, node.op)

        if isinstance(node.op, (ast.USub, ast.UAdd)):
            if not (operand.t.isInteger() or operand.t.isFloat()):
                raiseError(self.errorInfo, f'Operand of unary {type(node.op).__name__} is of type {operand.t}, expected an integer or float')
            return CheckedUnaryOpNode(node, operand, node.op)

        if isinstance(node.op, ast.Invert):
            if not operand.t.isInteger():
                raiseError(self.errorInfo, f'Operand of ~ is of type {operand.t}, expected an integer')
            return CheckedUnaryOpNode(node, operand, node.op)

        raiseError(self.errorInfo, f'Unsupported unary operator {type(node.op).__name__}')
        return CheckedUnaryOpNode(node, operand, node.op)
        

    def evalConstant(self, node: ast.Constant) -> CheckedNode:
        """
        10
        Node:
            value=10
        """
        self.errorInfo.lineNo = node.lineno

        t = noneType()
        valueType = type(node.value)
        if node.value is None:
            t = self.types['<None>']
            return CheckedNode(node, t)
        if valueType is bool:
            t = self.types['bool']
        elif valueType is int:
            t = self.types['int']
        elif valueType is float:
            t = self.types['float']
        elif valueType is str:
            t = self.types['str']
        else:
            raiseError(self.errorInfo, f'Constant of unsupported type {node.value}')

        return CheckedNode(node, t)

    def evalAnnAssign(self, node: ast.AnnAssign) -> CheckedNode:
        """
        w: list[int8] = [3, -1, 7]
        Declares a variable with the annotated type instead of the type of
        the value, so integer literals can be int8/int16.
        """
        self.errorInfo.lineNo = node.lineno
        if node.value is None:
            raiseError(self.errorInfo, 'An annotated variable needs a value')
        if not isinstance(node.target, ast.Name):
            raiseError(self.errorInfo, 'Only plain names can be annotated')
        declaredType = self.evalType(node.annotation)
        assign = ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)
        return self.evalAssign(assign, declaredType)

    def coerceLiterals(self, node: CheckedNode, target: Class):
        """
        Give integer literals the narrow integer type their context needs:
            x: int8 = -5
            w: list[int8] = [1, 2, 3]
            y = x + 1         # x is int8, so 1 is int8 too
        A literal that does not fit is an error. Anything else is left for
        the usual type checks to accept or reject.
        """
        if target.isNarrowInt() and node.t.isInt():
            value = literalInt(node.base)
            if value is None:
                return
            bits = target.intBits()
            if not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
                raiseError(self.errorInfo, f'Integer literal {value} does not fit in {target}')
            node.t = target
            if isinstance(node, CheckedUnaryOpNode):
                node.operand.t = target
        elif target.isList() and isinstance(node, CheckedListNode):
            base = target.asList().base
            if not node.vals:
                node.t = target
            elif base.isNarrowInt():
                for v in node.vals:
                    self.coerceLiterals(v, base)
                if all(v.t.name == base.name for v in node.vals):
                    node.t = ListType(None, base)

    def evalAssign(self, node: ast.Assign, declaredType: Class | None = None) -> CheckedNode:
        """
        a = 10
        my_list[0] = 5
        """
        self.errorInfo.lineNo = node.lineno
        if len(node.targets) > 1:
            raiseError(self.errorInfo, f'Tuples are not currently supported')

        target = node.targets[0]
        right = self.evalNode(node.value)

        if isinstance(target, ast.Subscript):
            # Element assignment: my_list[i] = value. The list already exists;
            # no new symbol is declared. The value must be assignable to the
            # element type.
            left = self.evalSubscript(target)
            self.errorInfo.lineNo = node.lineno
            self.coerceLiterals(right, left.t)
            if not left.t.isAssignable(right.t):
                raiseError(self.errorInfo, f'Cannot assign value of type {right.t} to element of type {left.t}')
            return CheckedAssignNode(node, left, right, [])

        if not isinstance(target, ast.Name):
            raiseError(self.errorInfo, f'Currently only name and subscript assignment is supported, tried to assign to {target}')

        name = target.id
        declared = list[str]()
        sym = self.findSymbol(name, False)
        if declaredType is not None:
            if sym is not None and str(sym) != str(declaredType):
                raiseError(self.errorInfo, f'{name} is already declared as {sym}')
            self.coerceLiterals(right, declaredType)
        elif sym is not None:
            self.coerceLiterals(right, sym)
        if sym is None:
            self.symbols[-1][name] = declaredType if declaredType is not None else right.t
            declared.append(name)

        left = self.evalNode(target)
        self.errorInfo.lineNo = node.lineno
        if not left.t.isAssignable(right.t):
            raiseError(self.errorInfo, f'Cannot assign value of type {right.t} to left-hand side of type {left.t}')

        return CheckedAssignNode(node, left, right, declared)

    def evalExpr(self, node: ast.Expr) -> CheckedNode:
        """
        Expressions don't have anything special about them,
        just evaluate the value
        """
        # @NOTE: I think this is safe to ignore
        self.errorInfo.lineNo = node.lineno
        return self.evalNode(node.value)

    def evalBinOp(self, node: ast.BinOp) -> CheckedNode:
        """
        a + b
        Binary operation
        Node:
            left=Name(a)
            op=Add()
            right=Name(b)
        """
        self.errorInfo.lineNo = node.lineno
        left = self.evalNode(node.left)
        right = self.evalNode(node.right)
        self.coerceLiterals(right, left.t)
        self.coerceLiterals(left, right.t)

        # int * str -> str (commutative string repetition)
        if isinstance(node.op, ast.Mult) and left.t.isInt() and right.t.name == 'str':
            result = CheckedBinNode(node, left, right, node.op)
            result.t = right.t  # result is str, not int
            return result

        self.checkIntOnlyOp(node.op, left.t, right.t)
        if left.t.canPerformBinOp(right.t, node.op):
            return CheckedBinNode(node, left, right, node.op)
        
        raiseError(self.errorInfo, f'Cannot perform a binary operation with types {left.t} and {right.t}')
        return CheckedNode(node, noneType())

