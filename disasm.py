import sys
import struct
from isa import Opcode, AddrMode, Registers

from const import OUT_INT, OUT_CHAR, TRAP_BUFFER

REG_MAP = {v: k for k, v in Registers.__members__.items()}


def from_bytes(data):
    words = []
    for i in range(0, len(data), 4):
        w = struct.unpack("<I", data[i : i + 4])[0]
        words.append(w)
    return words


def format_arg(mode, val):
    if mode == AddrMode.NONE:
        return ""
    if mode == AddrMode.IMM:
        return f"#{val}"
    if mode == AddrMode.MEM:
        if val == OUT_CHAR:
            return "[OUT_CHAR]"
        if val == OUT_INT:
            return "[OUT_INT]"
        if val == TRAP_BUFFER:
            return "[TRAP_BUFFER]"
        return f"[{val}]"
    if mode == AddrMode.REG:
        return REG_MAP.get(Registers(val), f"R{val}")
    if mode == AddrMode.REG_INDIRECT:
        return f"[{REG_MAP.get(Registers(val), f'R{val}')}]"
    return str(val)


def decode_at(words, i):
    h = words[i]
    op_val = h & 0xFF
    try:
        op = Opcode(op_val)
    except ValueError:
        return None, 1, f"{h:08X}  .word 0x{h:08X}"

    arg_count = (h >> 8) & 0xF
    modes = [AddrMode((h >> (12 + j * 4)) & 0xF) for j in range(arg_count)]

    # NADD: by ISA convention, all source operands are IMM.  The header only
    # has 4 mode slots, so when N+1 > 4 the extras would otherwise decode as
    # AddrMode.NONE (0) and render as blanks.
    if op == Opcode.NADD and arg_count > 1:
        modes = [modes[0]] + [AddrMode.IMM] * (arg_count - 1)

    if i + arg_count >= len(words):
        return None, 1, f"{h:08X}  <incomplete>"

    args = [words[i + 1 + j] for j in range(arg_count)]
    args_str = ", ".join([format_arg(m, a) for m, a in zip(modes, args)])

    if (
        op in [Opcode.JMP, Opcode.JEQ, Opcode.JGT, Opcode.CALL]
        and modes
        and modes[0] == AddrMode.IMM
    ):
        args_str = f"-> {args[0]:04d}"

    return op, 1 + arg_count, f"{h:08X}  {op.name:<6} {args_str}"


def disassemble(binary_data):
    words = from_bytes(binary_data)
    result = []
    if len(words) < 4:
        return "Binary too small"

    main_addr = words[1]
    irq_addr = words[3]
    first_code_addr = min(main_addr, irq_addr)

    i = 0
    while i < len(words):
        if i < 4:
            op, length, s = decode_at(words, i)
            label = " " if i % 2 == 0 else ""
            result.append(f"{i:04d}: {s}{label}")
            i += length
        elif i < first_code_addr:
            h = words[i]
            result.append(f"{i:04d}: {h:08X}  .word 0x{h:08X} (DATA)")
            i += 1
        else:
            op, length, s = decode_at(words, i)
            result.append(f"{i:04d}: {s}")
            i += length

    return "\n".join(result)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python disasm.py <binary_file>")
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        print(disassemble(f.read()))
