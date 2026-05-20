from __future__ import annotations

import ast
import logging
import sys
from dataclasses import dataclass, field
from enum import Enum

from isa import AddrMode, Opcode, Registers, from_bytes
from const import VECTOR_TRAP, OUT_INT, OUT_CHAR, TRAP_BUFFER

WORD_MASK = 0xFFFFFFFF
MEMORY_SIZE = 1024
DEFAULT_TICK_LIMIT = 50_000

INT_OPCODE = 0xFF


def to_word(value: int) -> int:
    return value & WORD_MASK


def to_signed(value: int) -> int:
    value &= WORD_MASK
    if value & 0x80000000:
        return value - 0x100000000
    return value


class PcSel(Enum):
    INC = "pc+1"
    DEC = "pc-1"
    ALU_OUT = "alu_out"
    TRAP_VECTOR = "trap_vector"


class IRQ(Enum):
    MEM = "mem"
    INT = "int"


class StepCntrSel(Enum):
    PLUS_ONE = "+1"
    ZERO = "0"


class OperandASel(Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    SP = "SP"


class OperandBSel(Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    SP = "SP"


class OpSrc1Sel(Enum):
    OPERAND_A = "operand_a"
    IMM1 = "imm1"


class OpSrc2Sel(Enum):
    OPERAND_B = "operand_b"
    IMM2 = "imm2"
    DATA_OUT = "data_out"


class Operation(Enum):
    PASS_A = "pass_a"
    PASS_B = "pass_b"
    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    MOD = "mod"
    ADC = "adc"
    SBC = "sbc"


class SpSaveSel(Enum):
    PLUS_ONE = "+1"
    MINUS_ONE = "-1"
    FROM_ALU = "from_alu"


class MemWrSel(Enum):
    ALU_OUT = "alu_out"
    FLAGS = "flags"


@dataclass
class InputEvent:
    tick: int
    char: int


class IOExternal:
    def __init__(self, events: list[InputEvent]):
        self.events = sorted(events, key=lambda e: e.tick)
        self._pos = 0
        self.irq = IRQ.MEM
        self.ie = False

    def _get_char(self, char_code: int) -> str:
        return chr(char_code & 0xFF)

    def update(self, tick: int, data_path: "DataPath") -> None:
        while self._pos < len(self.events) and self.events[self._pos].tick <= tick:
            event = self.events[self._pos]
            self._pos += 1
            if self.ie:
                logging.info(
                    "TICK:   %d char=%r DROPPED (IE=1)",
                    event.tick,
                    self._get_char(event.char),
                )
                continue
            data_path.memory[TRAP_BUFFER] = to_word(event.char)
            self.irq = IRQ.INT
            self.ie = True
            logging.info(
                "TICK:   %d char=%r IRQ=1 IE=1",
                event.tick,
                self._get_char(event.char),
            )

    def ack_irq(self) -> None:
        self.irq = IRQ.MEM

    def reset_ie(self) -> None:
        self.ie = False


class DataPath:
    def __init__(self, memory_image: list[int]):
        self.memory: list[int] = [0] * MEMORY_SIZE
        for addr, word in enumerate(memory_image):
            self.memory[addr] = to_word(word)

        self.registers: dict[Registers, int] = {
            Registers.R0: 0,
            Registers.R1: 0,
            Registers.R2: 0,
            Registers.R3: 0,
            Registers.SP: MEMORY_SIZE - 1,
        }
        self.addr_reg: int = 0
        self.alu_out: int = 0
        self.flag_n = False
        self.flag_z = False
        self.flag_c = False
        self.flag_v = False
        self.op_a_sel: OperandASel = OperandASel.R0
        self.op_b_sel: OperandBSel = OperandBSel.R0
        self.imm1: int = 0
        self.imm2: int = 0
        self.output_buffer: list[str] = []
        self._last_signals: dict[str, object] = {}

    def _read_via_a(self) -> int:
        s = self.op_a_sel
        if s == OperandASel.R0:
            return self.registers[Registers.R0]
        if s == OperandASel.R1:
            return self.registers[Registers.R1]
        if s == OperandASel.R2:
            return self.registers[Registers.R2]
        if s == OperandASel.R3:
            return self.registers[Registers.R3]
        if s == OperandASel.SP:
            return self.registers[Registers.SP]
        return 0

    def _read_via_b(self) -> int:
        s = self.op_b_sel
        if s == OperandBSel.R0:
            return self.registers[Registers.R0]
        if s == OperandBSel.R1:
            return self.registers[Registers.R1]
        if s == OperandBSel.R2:
            return self.registers[Registers.R2]
        if s == OperandBSel.R3:
            return self.registers[Registers.R3]
        if s == OperandBSel.SP:
            return self.registers[Registers.SP]
        return 0

    @property
    def operand_a(self) -> int:
        return self._read_via_a()

    @property
    def operand_b(self) -> int:
        return self._read_via_b()

    @property
    def data_out(self) -> int:
        if 0 <= self.addr_reg < MEMORY_SIZE:
            return self.memory[self.addr_reg]
        return 0

    def read_instr(self, addr: int) -> int:
        addr = to_word(addr) % MEMORY_SIZE
        return self.memory[addr]

    def begin_tick(self) -> None:
        self._last_signals = {}

    def signal_select_operand_a(self, sel: OperandASel) -> None:
        self.op_a_sel = sel
        self._last_signals["op_a_sel"] = sel.value

    def signal_select_operand_b(self, sel: OperandBSel) -> None:
        self.op_b_sel = sel
        self._last_signals["op_b_sel"] = sel.value

    def signal_set_imm(self, imm1: int | None = None, imm2: int | None = None) -> None:
        if imm1 is not None:
            self.imm1 = to_word(imm1)
            self._last_signals["imm1"] = to_signed(self.imm1)
        if imm2 is not None:
            self.imm2 = to_word(imm2)
            self._last_signals["imm2"] = to_signed(self.imm2)

    def _alu_input_1(self, sel: OpSrc1Sel) -> int:
        if sel == OpSrc1Sel.OPERAND_A:
            return self.operand_a
        if sel == OpSrc1Sel.IMM1:
            return self.imm1
        return 0

    def _alu_input_2(self, sel: OpSrc2Sel) -> int:
        if sel == OpSrc2Sel.OPERAND_B:
            return self.operand_b
        if sel == OpSrc2Sel.IMM2:
            return self.imm2
        if sel == OpSrc2Sel.DATA_OUT:
            return self.data_out
        return 0

    def signal_alu(
        self,
        operation: Operation,
        src1: OpSrc1Sel | None = None,
        src2: OpSrc2Sel | None = None,
        latch_flags: bool = False,
    ) -> None:
        if operation == Operation.PASS_A:
            self.alu_out = to_word(self._alu_input_1(src1))
            self._last_signals["alu_op"] = "PASS_A"
            self._last_signals["op_src_1"] = src1.value if src1 else ""
            self._last_signals["alu_out"] = to_signed(self.alu_out)
            return

        if operation == Operation.PASS_B:
            self.alu_out = to_word(self._alu_input_2(src2))
            self._last_signals["alu_op"] = "PASS_B"
            self._last_signals["op_src_2"] = src2.value if src2 else ""
            self._last_signals["alu_out"] = to_signed(self.alu_out)
            return

        left = self._alu_input_1(src1) if src1 else 0
        right = self._alu_input_2(src2) if src2 else 0
        a, b = to_signed(left), to_signed(right)

        if operation in (Operation.ADD, Operation.ADC):
            cin = 1 if (operation == Operation.ADC and self.flag_c) else 0
            raw = (left & WORD_MASK) + (right & WORD_MASK) + cin
            result = raw & WORD_MASK
            carry = bool(raw & (1 << 32))
            signed = to_signed(result)
            overflow = (a >= 0 and b + cin >= 0 and signed < 0) or (
                a < 0 and b + cin < 0 and signed >= 0
            )
        elif operation in (Operation.SUB, Operation.SBC):
            borrow = 1 if (operation == Operation.SBC and not self.flag_c) else 0
            raw = (left & WORD_MASK) - (right & WORD_MASK) - borrow
            result = raw & WORD_MASK
            carry = raw >= 0
            signed = to_signed(result)
            overflow = (a >= 0 and b < 0 and signed < 0) or (
                a < 0 and b > 0 and signed > 0
            )
        elif operation == Operation.MUL:
            result = to_word(a * b)
            carry = (a * b) != to_signed(result)
            overflow = carry
        elif operation == Operation.MOD:
            result = to_word(a - (a // b) * b) if b != 0 else 0
            carry = False
            overflow = False
        else:
            result = 0
            carry = False
            overflow = False

        self.alu_out = result
        if latch_flags:
            self.flag_z = result == 0
            self.flag_n = to_signed(result) < 0
            self.flag_c = carry
            self.flag_v = overflow
            self._last_signals["latch_flags"] = True
        self._last_signals["alu_op"] = operation.name
        self._last_signals["op_src_1"] = src1.value if src1 else ""
        self._last_signals["op_src_2"] = src2.value if src2 else ""
        self._last_signals["alu_out"] = to_signed(result)

    def signal_latch_addr_reg(self) -> None:
        self.addr_reg = to_word(self.alu_out) % MEMORY_SIZE
        self._last_signals["addr_reg"] = self.addr_reg

    def signal_mem_read(self) -> None:
        self._last_signals["read_data"] = True

    def signal_mem_write(self, sel: MemWrSel = MemWrSel.ALU_OUT) -> None:
        if sel == MemWrSel.ALU_OUT:
            value = self.alu_out
        elif sel == MemWrSel.FLAGS:
            value = self._flags_to_word()
        else:
            value = 0

        word = to_word(value)
        if self.addr_reg == OUT_CHAR:
            ch = chr(word & 0xFF)
            self.output_buffer.append(ch)
        elif self.addr_reg == OUT_INT:
            self.output_buffer.append(str(to_signed(word)))
        else:
            self.memory[self.addr_reg] = word
        self._last_signals["write_data"] = True
        self._last_signals["mem_wr_sel"] = sel.value

    def signal_latch_reg(self, reg: Registers) -> None:
        self.registers[reg] = to_word(self.alu_out)
        self._last_signals[f"latch_{reg.name}"] = to_signed(self.alu_out)

    def signal_sp_save(self, sel: SpSaveSel) -> None:
        sp = self.registers[Registers.SP]
        if sel == SpSaveSel.PLUS_ONE:
            new_sp = sp + 1
        elif sel == SpSaveSel.MINUS_ONE:
            new_sp = sp - 1
        elif sel == SpSaveSel.FROM_ALU:
            new_sp = self.alu_out
        else:
            new_sp = sp
        self.registers[Registers.SP] = to_word(new_sp)
        self._last_signals["sp_save"] = sel.value

    def signal_restore_flags(self) -> None:
        word = self.data_out
        self.flag_z = bool(word & 0b0001)
        self.flag_n = bool(word & 0b0010)
        self.flag_c = bool(word & 0b0100)
        self.flag_v = bool(word & 0b1000)
        self._last_signals["restore_flags"] = True

    def _flags_to_word(self) -> int:
        return (
            int(self.flag_z)
            | (int(self.flag_n) << 1)
            | (int(self.flag_c) << 2)
            | (int(self.flag_v) << 3)
        )


@dataclass
class DecodedInstr:
    opcode: int = Opcode.NOP
    arg_count: int = 0
    modes: list[AddrMode] = field(default_factory=list)


def decode_inst(word: int) -> DecodedInstr:
    op_raw = word & 0xFF

    if op_raw == INT_OPCODE:
        return DecodedInstr(opcode=INT_OPCODE, arg_count=0, modes=[])

    cnt = (word >> 8) & 0xF

    raw_modes = [(word >> (12 + i * 4)) & 0xF for i in range(cnt)]
    modes = [AddrMode(r & 0x7) for r in raw_modes]

    if op_raw == Opcode.NADD.value and cnt > 1:
        modes = [modes[0]] + [AddrMode.IMM] * (cnt - 1)

    return DecodedInstr(opcode=op_raw, arg_count=cnt, modes=modes)


class ControlUnit:
    def __init__(self, data_path: DataPath, io_controller: IOExternal):
        self.dp = data_path
        self.io = io_controller

        self.inst_prnt: int = 0

        self.instr: DecodedInstr = DecodedInstr()
        self.operand1: int = 0
        self.operand2: int = 0
        self.operand3: int = 0

        self.latch_op1: bool = True
        self.latch_op2: bool = True
        self.latch_op3: bool = True

        self.step_counter: int = 0

        self.pc: int = 0

        self._tick: int = 0
        self._halted: bool = False
        self._last_op: str = "start"

    def signal_latch_sc(self, sel: StepCntrSel) -> None:
        if sel == StepCntrSel.PLUS_ONE:
            self.step_counter += 1
        elif sel == StepCntrSel.ZERO:
            self.step_counter = 0

    def signal_latch_pc(self, sel: PcSel) -> None:
        if sel == PcSel.INC:
            value = self.pc + 1
        elif sel == PcSel.DEC:
            value = self.pc - 1
        elif sel == PcSel.ALU_OUT:
            value = self.dp.alu_out
        elif sel == PcSel.TRAP_VECTOR:
            value = VECTOR_TRAP
        else:
            value = self.pc
        self.pc = to_word(value) % MEMORY_SIZE

    def signal_latch_instr(self, sel: IRQ) -> None:
        if sel == IRQ.MEM:
            self.inst_prnt = to_word(self.dp.read_instr(self.pc))
            self.instr = decode_inst(self.inst_prnt)
            self._last_op = "FETCH"
        elif sel == IRQ.INT:
            self.inst_prnt = INT_OPCODE
            self.instr = DecodedInstr(opcode=INT_OPCODE, arg_count=0)
            self._last_op = "FETCH"

    def current_tick(self) -> int:
        return self._tick

    def is_halted(self) -> bool:
        return self._halted

    def process_next_tick(self) -> None:
        if self._halted:
            return
        self.io.update(self._tick, self.dp)
        self.dp.begin_tick()
        self.latch_all_op()

        if self.step_counter == 0:
            self._fetch_inst()
        else:
            args_done_at = self._fetch_phase_length()
            if self.step_counter <= args_done_at:
                self._fetch_argument(self.step_counter)
            else:
                self._execute()

        self._tick += 1

    def _fetch_phase_length(self) -> int:
        if self.instr.opcode == Opcode.NADD:
            return min(self.instr.arg_count, 3)
        return self.instr.arg_count

    def write_op(self) -> None:
        self.dp.signal_mem_read()
        word = to_word(self.dp.read_instr(self.pc))

        if not self.latch_op1:
            self.operand1 = word
        if not self.latch_op2:
            self.operand2 = word
        if not self.latch_op3:
            self.operand3 = word

    def _fetch_inst(self) -> None:
        self.dp.signal_mem_read()

        self.signal_latch_instr(self.io.irq)

        self.signal_latch_pc(PcSel.INC)
        self.signal_latch_sc(StepCntrSel.PLUS_ONE)

    def _fetch_argument(self, step: int) -> None:
        if step == 1:
            self.latch_op1 = False
        elif step == 2:
            self.latch_op2 = False
        elif step == 3:
            self.latch_op3 = False

        self.write_op()

        _word = to_word(self.dp.read_instr(self.pc))

        op_name = (
            Opcode(self.instr.opcode).name if self.instr.opcode != INT_OPCODE else "INT"
        )
        self._last_op = f"{op_name} OPERAND{step} = {to_signed(_word)}"

        self.signal_latch_pc(PcSel.INC)
        self.signal_latch_sc(StepCntrSel.PLUS_ONE)

    def latch_all_op(self) -> None:
        self.latch_op1 = True
        self.latch_op2 = True
        self.latch_op3 = True

    def _execute(self) -> None:
        local_step = self.step_counter - (1 + self._fetch_phase_length())
        op_raw = self.instr.opcode

        if op_raw == INT_OPCODE:
            self._exec_int(self.step_counter)
            return

        op = Opcode(op_raw)
        if op == Opcode.NOP:
            self._last_op = "NOP"
            self._finish()
            return
        if op == Opcode.HALT:
            self._halted = True
            self._last_op = "HALT"
            self.signal_latch_sc(StepCntrSel.ZERO)
            return
        if op == Opcode.IRET:
            self._exec_iret(self.step_counter)
            return
        if op == Opcode.MOV:
            self._exec_mov(local_step)
            return
        if op in (
            Opcode.ADD,
            Opcode.SUB,
            Opcode.MUL,
            Opcode.MOD,
            Opcode.ADC,
            Opcode.SBC,
        ):
            self._exec_alu3(op, local_step)
            return
        if op == Opcode.CMP:
            self._exec_cmp(local_step)
            return
        if op == Opcode.JMP:
            self._exec_jmp(local_step, taken=True)
            return
        if op == Opcode.JEQ:
            self._exec_jmp(local_step, taken=self.dp.flag_z)
            return
        if op == Opcode.JGT:
            taken = (not self.dp.flag_z) and (self.dp.flag_n == self.dp.flag_v)
            self._exec_jmp(local_step, taken=taken)
            return
        if op == Opcode.CALL:
            self._exec_call(local_step)
            return
        if op == Opcode.RET:
            self._exec_ret(local_step)
            return
        if op == Opcode.PUSH:
            self._exec_push(local_step)
            return
        if op == Opcode.POP:
            self._exec_pop(local_step)
            return
        if op == Opcode.NADD:
            self._exec_nadd(local_step)
            return

    def _finish(self) -> None:
        self.signal_latch_sc(StepCntrSel.ZERO)

    @staticmethod
    def _reg_from_operand(value: int) -> Registers:
        v = value & 0x7
        if v <= 4:
            return Registers(v)
        return Registers.R0

    @staticmethod
    def _op_a_sel(reg: Registers) -> OperandASel:
        return {
            Registers.R0: OperandASel.R0,
            Registers.R1: OperandASel.R1,
            Registers.R2: OperandASel.R2,
            Registers.R3: OperandASel.R3,
            Registers.SP: OperandASel.SP,
        }[reg]

    @staticmethod
    def _op_b_sel(reg: Registers) -> OperandBSel:
        return {
            Registers.R0: OperandBSel.R0,
            Registers.R1: OperandBSel.R1,
            Registers.R2: OperandBSel.R2,
            Registers.R3: OperandBSel.R3,
            Registers.SP: OperandBSel.SP,
        }[reg]

    def _stage_addr_for_operand(self, mode: AddrMode, operand: int) -> None:
        if mode == AddrMode.MEM:
            self.dp.signal_set_imm(imm1=operand)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
            self.dp.signal_latch_addr_reg()
        elif mode == AddrMode.REG_INDIRECT:
            self.dp.signal_select_operand_a(
                self._op_a_sel(self._reg_from_operand(operand))
            )
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()

    def _stage_value_pass(self, mode: AddrMode, operand: int) -> None:
        if mode == AddrMode.IMM:
            self.dp.signal_set_imm(imm1=operand)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
        elif mode == AddrMode.REG:
            self.dp.signal_select_operand_a(
                self._op_a_sel(self._reg_from_operand(operand))
            )
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)

    def _stage_src1_to_alu_input(self, mode: AddrMode, arg: int) -> OpSrc1Sel | None:
        if mode == AddrMode.REG:
            self.dp.signal_select_operand_a(self._op_a_sel(self._reg_from_operand(arg)))
            return OpSrc1Sel.OPERAND_A
        if mode == AddrMode.IMM:
            self.dp.signal_set_imm(imm1=arg)
            return OpSrc1Sel.IMM1
        return None

    def _stage_src2_to_alu_input(self, mode: AddrMode, arg: int) -> OpSrc2Sel | None:
        if mode == AddrMode.REG:
            self.dp.signal_select_operand_b(self._op_b_sel(self._reg_from_operand(arg)))
            return OpSrc2Sel.OPERAND_B
        if mode == AddrMode.IMM:
            self.dp.signal_set_imm(imm2=arg)
            return OpSrc2Sel.IMM2
        return None

    @staticmethod
    def _opcode_to_alu(op: Opcode) -> Operation:
        return {
            Opcode.ADD: Operation.ADD,
            Opcode.SUB: Operation.SUB,
            Opcode.MUL: Operation.MUL,
            Opcode.MOD: Operation.MOD,
            Opcode.ADC: Operation.ADC,
            Opcode.SBC: Operation.SBC,
            Opcode.CMP: Operation.SUB,
        }[op]

    def _exec_mov(self, step: int) -> None:
        dst_mode, src_mode = self.instr.modes
        dst_arg, src_arg = self.operand1, self.operand2
        src_is_mem = src_mode in (AddrMode.MEM, AddrMode.REG_INDIRECT)
        dst_is_mem = dst_mode in (AddrMode.MEM, AddrMode.REG_INDIRECT)

        if not src_is_mem:
            if dst_mode == AddrMode.REG and step == 0:
                self._stage_value_pass(src_mode, src_arg)
                self.dp.signal_latch_reg(self._reg_from_operand(dst_arg))
                self._last_op = (
                    f"MOV {self._reg_from_operand(dst_arg).name} <- {src_mode.name}"
                )
                self._finish()
                return
            if dst_is_mem:
                if step == 0:
                    self._stage_addr_for_operand(dst_mode, dst_arg)
                    self._last_op = f"MOV stage dst addr ({dst_mode.name})"
                    self.signal_latch_sc(StepCntrSel.PLUS_ONE)
                    return
                if step == 1:
                    self._stage_value_pass(src_mode, src_arg)
                    self.dp.signal_mem_write()
                    self._last_op = "MOV -> mem"
                    self._finish()
                    return

        if src_is_mem:
            if step == 0:
                self._stage_addr_for_operand(src_mode, src_arg)
                self._last_op = f"MOV stage src addr ({src_mode.name})"
                self.signal_latch_sc(StepCntrSel.PLUS_ONE)
                return
            if dst_mode == AddrMode.REG and step == 1:
                self.dp.signal_mem_read()
                self.dp.signal_alu(Operation.PASS_B, src2=OpSrc2Sel.DATA_OUT)
                reg = self._reg_from_operand(dst_arg)
                self.dp.signal_latch_reg(reg)
                self._last_op = f"MOV {reg.name} <- mem"
                self._finish()
                return

    def _exec_alu3(self, opcode: Opcode, step: int) -> None:
        dst_mode, m1, m2 = self.instr.modes
        a_dst, a1, a2 = self.operand1, self.operand2, self.operand3
        alu_op = self._opcode_to_alu(opcode)
        latch_flags = True

        if step == 0 and dst_mode == AddrMode.REG:
            src1_sel = self._stage_src1_to_alu_input(m1, a1)
            src2_sel = self._stage_src2_to_alu_input(m2, a2)
            self.dp.signal_alu(alu_op, src1_sel, src2_sel, latch_flags=latch_flags)
            self.dp.signal_latch_reg(self._reg_from_operand(a_dst))
            self._last_op = f"{opcode.name} {self._reg_from_operand(a_dst).name} <- alu"
            self._finish()
            return

    def _exec_cmp(self, step: int) -> None:
        m1, m2 = self.instr.modes
        a1, a2 = self.operand1, self.operand2

        if step == 0:
            src1_sel = self._stage_src1_to_alu_input(m1, a1)
            src2_sel = self._stage_src2_to_alu_input(m2, a2)
            self.dp.signal_alu(Operation.SUB, src1_sel, src2_sel, latch_flags=True)
            self._last_op = "CMP"
            self._finish()
            return

    def _exec_jmp(self, step: int, taken: bool) -> None:
        mode = self.instr.modes[0]
        arg = self.operand1
        if step != 0:
            return
        if not taken:
            self._last_op = "Jcc not taken"
            self._finish()
            return
        if mode == AddrMode.IMM:
            self.dp.signal_set_imm(imm1=arg)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
        elif mode == AddrMode.REG:
            self.dp.signal_select_operand_a(self._op_a_sel(self._reg_from_operand(arg)))
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
        self.signal_latch_pc(PcSel.ALU_OUT)
        self._last_op = f"JMP -> {self.pc:#06X}"
        self._finish()

    def _exec_push(self, step: int) -> None:
        mode = self.instr.modes[0]
        arg = self.operand1
        if step == 0:
            self.dp.signal_sp_save(SpSaveSel.MINUS_ONE)
            self._last_op = "PUSH SP--"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 1:
            self.dp.signal_select_operand_a(OperandASel.SP)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()
            self._last_op = "PUSH ADDR_REG <- SP"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 2:
            self._stage_value_pass(mode, arg)
            self.dp.signal_mem_write()
            self._last_op = "PUSH write"
            self._finish()
            return

    def _exec_pop(self, step: int) -> None:
        mode = self.instr.modes[0]
        arg = self.operand1
        if step == 0:
            self.dp.signal_select_operand_a(OperandASel.SP)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()
            self._last_op = "POP ADDR_REG <- SP"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 1:
            self.dp.signal_mem_read()
            self.dp.signal_alu(Operation.PASS_B, src2=OpSrc2Sel.DATA_OUT)
            if mode == AddrMode.REG:
                reg = self._reg_from_operand(arg)
                self.dp.signal_latch_reg(reg)
                self._last_op = f"POP -> {reg.name}"
            self.dp.signal_sp_save(SpSaveSel.PLUS_ONE)
            self._finish()
            return

    def _fetch_next_into_op3(self, src_idx: int, total_srcs: int) -> None:
        self.latch_op3 = False
        self.write_op()
        word = to_word(self.dp.read_instr(self.pc))
        self._last_op = f"NADD load src{src_idx}/{total_srcs} = {to_signed(word)}"
        self.signal_latch_pc(PcSel.INC)
        self.signal_latch_sc(StepCntrSel.PLUS_ONE)

    def _exec_nadd(self, step: int) -> None:
        dst_mode = self.instr.modes[0]
        dst_arg = self.operand1
        n_sources = self.instr.arg_count - 1

        if dst_mode != AddrMode.REG:
            return

        dst_reg = self._reg_from_operand(dst_arg)

        if n_sources == 1:
            if step == 0:
                self.dp.signal_set_imm(imm1=self.operand2)
                self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
                self.dp.signal_latch_reg(dst_reg)
                self._last_op = f"NADD {dst_reg.name} <- {to_signed(self.operand2)}"
                self._finish()
                return

        if step == 0:
            self.dp.signal_set_imm(imm1=self.operand2, imm2=self.operand3)
            self.dp.signal_alu(
                Operation.ADD, OpSrc1Sel.IMM1, OpSrc2Sel.IMM2, latch_flags=False
            )
            self.dp.signal_latch_reg(dst_reg)
            self._last_op = f"NADD {dst_reg.name} <- src1+src2"
            if n_sources == 2:
                self._finish()
            else:
                self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return

        if step % 2 == 1:
            src_idx = (step - 1) // 2 + 3
            self._fetch_next_into_op3(src_idx, n_sources)
            return

        self.dp.signal_select_operand_a(self._op_a_sel(dst_reg))
        self.dp.signal_set_imm(imm2=self.operand3)
        self.dp.signal_alu(
            Operation.ADD,
            OpSrc1Sel.OPERAND_A,
            OpSrc2Sel.IMM2,
            latch_flags=False,
        )
        self.dp.signal_latch_reg(dst_reg)
        src_idx = step // 2 + 2
        self._last_op = f"NADD {dst_reg.name} += src{src_idx}"
        if src_idx == n_sources:
            self._finish()
        else:
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)

    def _exec_call(self, step: int) -> None:
        mode = self.instr.modes[0]
        arg = self.operand1
        if step == 0:
            self.dp.signal_sp_save(SpSaveSel.MINUS_ONE)
            self._last_op = "CALL SP--"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 1:
            self.dp.signal_select_operand_a(OperandASel.SP)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()
            self._last_op = "CALL ADDR_REG <- SP"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 2:
            self.dp.signal_set_imm(imm1=self.pc)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
            self.dp.signal_mem_write()
            self._last_op = f"CALL push pc={self.pc:#06X}"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 3:
            if mode == AddrMode.IMM:
                self.dp.signal_set_imm(imm1=arg)
                self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
            elif mode == AddrMode.REG:
                self.dp.signal_select_operand_a(
                    self._op_a_sel(self._reg_from_operand(arg))
                )
                self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.signal_latch_pc(PcSel.ALU_OUT)
            self._last_op = "CALL -> target"
            self._finish()
            return

    def _exec_ret(self, step: int) -> None:
        if step == 0:
            self.dp.signal_select_operand_a(OperandASel.SP)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()
            self._last_op = "RET ADDR_REG <- SP"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if step == 1:
            self.dp.signal_mem_read()
            self.dp.signal_alu(Operation.PASS_B, src2=OpSrc2Sel.DATA_OUT)
            self.signal_latch_pc(PcSel.ALU_OUT)
            self.dp.signal_sp_save(SpSaveSel.PLUS_ONE)
            self._last_op = "RET pop pc"
            self._finish()
            return

    def _exec_int(self, sc: int) -> None:
        save_seq = [
            ("PC", "pc"),
            ("R0", OperandASel.R0),
            ("R1", OperandASel.R1),
            ("R2", OperandASel.R2),
            ("R3", OperandASel.R3),
        ]
        per_value = 3
        regs_total = per_value * len(save_seq)

        if sc == 1:
            self.io.ack_irq()
            self.signal_latch_pc(PcSel.DEC)

        if 1 <= sc <= regs_total:
            local = sc - 1
            idx = local // per_value
            phase = local % per_value
            name, marker = save_seq[idx]
            if phase == 0:
                self.dp.signal_sp_save(SpSaveSel.MINUS_ONE)
                self._last_op = f"INT SP-- (for {name})"
            elif phase == 1:
                self.dp.signal_select_operand_a(OperandASel.SP)
                self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
                self.dp.signal_latch_addr_reg()
                self._last_op = f"INT ADDR_REG <- SP (for {name})"
            else:
                if marker == "pc":
                    self.dp.signal_set_imm(imm1=self.pc)
                    self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.IMM1)
                else:
                    self.dp.signal_select_operand_a(marker)
                    self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
                self.dp.signal_mem_write()
                self._last_op = f"INT push {name}"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return

        flags_base = regs_total
        if sc == flags_base + 1:
            self.dp.signal_sp_save(SpSaveSel.MINUS_ONE)
            self._last_op = "INT SP-- (for FLAGS)"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if sc == flags_base + 2:
            self.dp.signal_select_operand_a(OperandASel.SP)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()
            self._last_op = "INT ADDR_REG <- SP (for FLAGS)"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if sc == flags_base + 3:
            self.dp.signal_mem_write(MemWrSel.FLAGS)
            self._last_op = "INT push FLAGS"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return

        if sc == flags_base + 4:
            self.signal_latch_pc(PcSel.TRAP_VECTOR)
            self._last_op = f"INT vector -> {VECTOR_TRAP:#06X}"
            self.signal_latch_sc(StepCntrSel.ZERO)
            return

    def _exec_iret(self, sc: int) -> None:
        if sc == 1:
            self.dp.signal_select_operand_a(OperandASel.SP)
            self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
            self.dp.signal_latch_addr_reg()
            self._last_op = "IRET ADDR_REG <- SP (for FLAGS)"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return
        if sc == 2:
            self.dp.signal_mem_read()
            self.dp.signal_restore_flags()
            self.dp.signal_sp_save(SpSaveSel.PLUS_ONE)
            self._last_op = "IRET pop FLAGS"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return

        restore_seq = [
            ("R3", Registers.R3),
            ("R2", Registers.R2),
            ("R1", Registers.R1),
            ("R0", Registers.R0),
            ("PC", None),
        ]
        per_value = 2
        regs_base = 2
        regs_total = per_value * len(restore_seq)

        if regs_base + 1 <= sc <= regs_base + regs_total:
            local = sc - regs_base - 1
            idx = local // per_value
            phase = local % per_value
            name, target = restore_seq[idx]
            if phase == 0:
                self.dp.signal_select_operand_a(OperandASel.SP)
                self.dp.signal_alu(Operation.PASS_A, OpSrc1Sel.OPERAND_A)
                self.dp.signal_latch_addr_reg()
                self._last_op = f"IRET ADDR_REG <- SP (for {name})"
            else:
                self.dp.signal_mem_read()
                self.dp.signal_alu(Operation.PASS_B, src2=OpSrc2Sel.DATA_OUT)
                if target is None:
                    self.signal_latch_pc(PcSel.ALU_OUT)
                else:
                    self.dp.signal_latch_reg(target)
                self.dp.signal_sp_save(SpSaveSel.PLUS_ONE)
                self._last_op = f"IRET pop {name}"
            self.signal_latch_sc(StepCntrSel.PLUS_ONE)
            return

        if sc == regs_base + regs_total + 1:
            self.io.reset_ie()
            self._last_op = "IRET (IE=0)"
            self.signal_latch_sc(StepCntrSel.ZERO)
            return

    def __repr__(self) -> str:
        irq = 1 if self.io.irq == IRQ.INT else 0

        flags = (
            ("N" if self.dp.flag_n else "-")
            + ("Z" if self.dp.flag_z else "-")
            + ("C" if self.dp.flag_c else "-")
            + ("V" if self.dp.flag_v else "-")
        )
        regs = " ".join(
            f"{r.name}={to_signed(self.dp.registers[r])}"
            for r in (
                Registers.R0,
                Registers.R1,
                Registers.R2,
                Registers.R3,
                Registers.SP,
            )
        )
        return (
            f"TICK: {self._tick:4d} SC: {self.step_counter} "
            f"PC={self.pc:04X} OP1={self.operand1:08X} OP2={self.operand2:08X} OP3={self.operand3:08X} "
            f"IE={int(self.io.ie)} IRQ={irq} FLAGS={flags} {regs} | OP={self._last_op}"
        )


def simulation(
    code: list[int],
    input_schedule: list[InputEvent],
    limit: int = DEFAULT_TICK_LIMIT,
) -> tuple[str, int]:
    dp = DataPath(code)
    io = IOExternal(input_schedule)
    cu = ControlUnit(dp, io)

    while cu.current_tick() < limit and not cu.is_halted():
        cu.process_next_tick()

        if cu.current_tick() <= 500:
            logging.info(cu)

    output = "".join(dp.output_buffer)
    ticks = cu.current_tick()

    logging.info("\n--- OUTPUT ---")
    logging.info(output)
    logging.info(f"--- ticks: {ticks} ---")


def parse_input_schedule(path: str | None) -> list[InputEvent]:
    if path is None or path == "none":
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return []
    raw = ast.literal_eval(text)
    events: list[InputEvent] = []
    for item in raw:
        tick, ch = item
        value = ord(ch) if isinstance(ch, str) else int(ch)
        events.append(InputEvent(tick=int(tick), char=value))
    return events


def main(
    code_file: str, input_file: str | None = None, limit: int = DEFAULT_TICK_LIMIT
) -> None:
    with open(code_file, "rb") as f:
        code = from_bytes(f.read())
    schedule = parse_input_schedule(input_file)
    simulation(code, schedule, limit=limit)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", filemode="w", filename="machine.log"
    )
    if len(sys.argv) not in (2, 3):
        print("Usage: python machine.py <code.bin> [<input_schedule.txt>]")
        sys.exit(1)
    code_path = sys.argv[1]
    input_path = sys.argv[2] if len(sys.argv) == 3 else None
    main(code_path, input_path)
