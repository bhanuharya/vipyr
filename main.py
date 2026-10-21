import argparse
import sys
from src.compiler import Compiler
from src.error import CompileError
import src.constants as constants


def main():
    """
    Parse the commandline arguments and create the Compiler

    Currently the only commandline argument is the single python file to compile
    """
    parser = argparse.ArgumentParser(prog='vipyr')
    parser.add_argument('filein')
    parser.add_argument('--bits', '-b', type=int, default=32)
    parser.add_argument('--output', '-o', default='out.ll',
                        help='Output LLVM IR file (default: out.ll)')
    parser.add_argument('--accel', action='append', default=[], metavar='NAME:FUNCT3:FUNCT7',
                        help='replace calls to function NAME with a custom-0 instruction; '
                             'src.runner passes this only for a valid certificate')
    parser.add_argument('--bounds-check', action='store_true', default=False,
                        help='Emit runtime bounds checks for list/string subscripts; '
                             'an out-of-range index prints an IndexError to stderr '
                             'and exits with status 1')
    args = parser.parse_args()

    constants.REGISTER_SIZE = args.bits
    accel = {}
    for item in args.accel:
        name, funct3, funct7 = item.split(':')
        accel[name] = (int(funct3), int(funct7))
    compiler = Compiler(args.filein, outFile=args.output,
                        boundsCheck=args.bounds_check, accel=accel)

    try:
        with open(args.filein) as f:
            if not compiler.parseFile(f, args.filein):
                return 1

        compiler.typeCheck()
        compiler.compileToLLVM()
        return 0
    except FileNotFoundError:
        print(f'File not found: {args.filein}', file=sys.stderr)
        return 1
    except PermissionError:
        print(f'Could not open file: {args.filein}', file=sys.stderr)
        return 1
    except CompileError as e:
        print(e.message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
