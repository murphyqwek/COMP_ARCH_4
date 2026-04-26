from enum import Enum
import struct


class Opcode(int, Enum):
    NOP = 0
    MOV = 1
    ADD = 2
    SUB = 3
    MUL = 4
    MOD = 5
    CMP = 6
    JMP = 7
    JEQ = 8
    JGT = 9
    CALL = 10
    RET = 11
    PUSH = 12
    POP = 13
    IRET = 14
    HALT = 15
    ADC = 16
    SBC = 17
    DIV = 18


class AddrMode(int, Enum):
    NONE = 0
    IMM = 1
    MEM = 2
    REG = 3
    REG_INDIRECT = 4


class Registers(int, Enum):
    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3
    SP = 4


def serialize_instruction(opcode, modes, args):
    header = opcode.value & 0xFF
    header |= (len(args) & 0xF) << 8
    for i, m in enumerate(modes):
        if i < 4:
            header |= (m.value & 0xF) << (12 + i * 4)
    return [header] + [a & 0xFFFFFFFF for a in args]


def to_bytes(words):
    buf = bytearray()
    for w in words:
        buf.extend(struct.pack("<I", w & 0xFFFFFFFF))
    return bytes(buf)


def from_bytes(data):
    words = []
    for i in range(0, len(data), 4):
        w = struct.unpack("<I", data[i : i + 4])[0]
        words.append(w)
    return words
