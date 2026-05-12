import sys
import logging
import ast
from enum import Enum
from isa import Opcode, AddrMode, Registers, from_bytes
from const import VECTOR_TRAP, OUT_INT, OUT_CHAR, TRAP_BUFFER


class CUState(Enum):
    NORMAL = 0
    INTERRUPT_SEQ = 1
    IRET_SEQ = 2
    CALL_SEQ = 3
    RET_SEQ = 4


class InstrState(Enum):
    FETCHING_OPERANDS = 1
    EXECUTING = 2
    WRITING_BACK = 3
    RETIRED = 4


FLAG_WRITERS = {
    Opcode.CMP,
    Opcode.ADD,
    Opcode.SUB,
    Opcode.ADC,
    Opcode.SBC,
    Opcode.MUL,
    Opcode.MOD,
}
FLAG_READERS = {Opcode.JEQ, Opcode.JGT}


class Instruction:
    def __init__(self, opcode, modes, args, pc):
        self.opcode = opcode
        self.modes = modes
        self.args = args
        self.pc = pc
        self.state = InstrState.FETCHING_OPERANDS
        self.result = None
        self.operands = None
        self.cycles_left = 1
        self.f_n, self.f_z, self.f_c, self.f_v = False, False, False, False
        for m in modes:
            if m in [AddrMode.MEM, AddrMode.REG_INDIRECT]:
                self.cycles_left += 1

    def __repr__(self):
        return f"{self.opcode.name}:{self.state.name[0]}@{self.pc}"


class DataPath:
    def __init__(self, mem_words):
        self.mem = mem_words + [0] * (10000 - len(mem_words))
        self.regs = [0] * 5
        self.regs[Registers.SP] = 9000
        self.n, self.z, self.c, self.v = False, False, False, False

    def get_flags(self):
        return (
            (int(self.n) << 3) | (int(self.z) << 2) | (int(self.c) << 1) | int(self.v)
        )

    def set_flags(self, word):
        word = int(word)
        self.n = bool(word & 8)
        self.z = bool(word & 4)
        self.c = bool(word & 2)
        self.v = bool(word & 1)

    def alu(self, op, a, b=0, carry=0):
        a &= 0xFFFFFFFF
        b &= 0xFFFFFFFF
        res = 0
        if op in [Opcode.MOV, Opcode.PUSH, Opcode.POP]:
            res = a
        elif op in [Opcode.ADD, Opcode.ADC]:
            res = a + b + carry
        elif op in [Opcode.SUB, Opcode.SBC, Opcode.CMP]:
            res = a - b - carry
        elif op == Opcode.MUL:
            res = a * b
        elif op == Opcode.MOD:
            res = a % b if b != 0 else 0

        res32 = res & 0xFFFFFFFF

        z = res32 == 0
        n = bool(res32 & 0x80000000)
        c = res > 0xFFFFFFFF or res < 0
        v = False
        if op in [Opcode.ADD, Opcode.ADC]:
            v = bool((~(a ^ b) & (a ^ res32) & 0x80000000))
        elif op in [Opcode.SUB, Opcode.SBC, Opcode.CMP]:
            v = bool(((a ^ b) & (a ^ res32) & 0x80000000))

        return res32, z, n, c, v


class ControlUnit:
    def __init__(self, dp, schedule=None, superscalar=True):
        self.dp = dp
        self.fetch_pc = 0
        self.tick = 0
        self.ie = True
        self.halted = False
        self.cu_state = CUState.NORMAL
        self.seq_step = 0
        self.seq_data = None
        self.schedule = schedule or []
        self.output = []
        self.decode_queue = []
        self.fetch_buffer = []
        self.retire_buffer = []
        self.rs = []
        self.superscalar = superscalar

    def pipeline_flush(self, new_pc):
        self.fetch_pc = new_pc
        self.fetch_buffer.clear()
        self.decode_queue.clear()
        self.rs.clear()

    def _read_forward(self, mode, val, current_instr):
        idx = self.decode_queue.index(current_instr)
        older_instrs = self.decode_queue[:idx]

        if mode == AddrMode.REG:
            for prev in reversed(older_instrs):
                if prev.modes and prev.modes[0] == AddrMode.REG and prev.args[0] == val:
                    if prev.result is not None:
                        return prev.result
            return self.dp.regs[val]
        if mode == AddrMode.IMM:
            return val
        if mode == AddrMode.MEM:
            return self.dp.mem[val]
        if mode == AddrMode.REG_INDIRECT:
            addr = self._read_forward(AddrMode.REG, val, current_instr)
            return self.dp.mem[addr]
        return 0

    def _is_ready_to_dispatch(self, instr):
        idx = self.decode_queue.index(instr)
        older_instrs = self.decode_queue[:idx]

        NON_WRITING_OPCODES = {
            Opcode.CMP,
            Opcode.PUSH,
            Opcode.JMP,
            Opcode.JEQ,
            Opcode.JGT,
            Opcode.CALL,
            Opcode.RET,
            Opcode.IRET,
            Opcode.HALT,
        }

        if instr.opcode in [
            Opcode.PUSH,
            Opcode.POP,
            Opcode.CALL,
            Opcode.RET,
            Opcode.IRET,
        ]:
            for prev in older_instrs:
                if prev.opcode in [
                    Opcode.PUSH,
                    Opcode.POP,
                    Opcode.CALL,
                    Opcode.RET,
                    Opcode.IRET,
                ]:
                    if prev.state != InstrState.RETIRED:
                        return False

        if instr.opcode in FLAG_READERS:
            for prev in older_instrs:
                if prev.opcode in FLAG_WRITERS:
                    if prev.state != InstrState.RETIRED:
                        return False

        for i, (m, a) in enumerate(zip(instr.modes, instr.args)):
            if m not in (AddrMode.REG, AddrMode.REG_INDIRECT):
                continue

            is_write = (
                i == 0 and m == AddrMode.REG and instr.opcode not in NON_WRITING_OPCODES
            )
            is_read = not is_write or instr.opcode != Opcode.MOV

            for prev in older_instrs:
                prev_writes = (
                    prev.modes
                    and prev.modes[0] == AddrMode.REG
                    and prev.args[0] == a
                    and prev.opcode not in NON_WRITING_OPCODES
                )

                if prev_writes and (is_read or is_write):
                    if prev.state not in [InstrState.WRITING_BACK, InstrState.RETIRED]:
                        return False

                if is_write and prev.operands is None:
                    for p_i, (p_m, p_a) in enumerate(zip(prev.modes, prev.args)):
                        if p_m == AddrMode.REG_INDIRECT and p_a == a:
                            return False
                        if p_m == AddrMode.REG and p_a == a:
                            p_is_w = p_i == 0 and prev.opcode not in NON_WRITING_OPCODES
                            if not p_is_w or prev.opcode != Opcode.MOV:
                                return False

        reads_mem = instr.modes and any(
            m in [AddrMode.MEM, AddrMode.REG_INDIRECT] for m in instr.modes[1:]
        )

        writes_mem = instr.modes and instr.modes[0] in [
            AddrMode.MEM,
            AddrMode.REG_INDIRECT,
        ]

        if reads_mem or writes_mem:
            for prev in older_instrs:
                prev_writes_mem = (
                    prev.modes
                    and prev.modes[0] in [AddrMode.MEM, AddrMode.REG_INDIRECT]
                ) or (prev.opcode == Opcode.PUSH)
                if prev_writes_mem:
                    if prev.state != InstrState.RETIRED:
                        return False
        return True

    def _check_hazard(self, instr):
        NON_WRITING_OPCODES = {
            Opcode.CMP,
            Opcode.PUSH,
            Opcode.JMP,
            Opcode.JEQ,
            Opcode.JGT,
            Opcode.CALL,
            Opcode.RET,
            Opcode.IRET,
            Opcode.HALT,
        }

        if instr.opcode in FLAG_READERS:
            for prev in self.rs:
                if prev == instr:
                    break
                if prev.opcode in FLAG_WRITERS and prev.result is None:
                    return True

        for i, (m, a) in enumerate(zip(instr.modes, instr.args)):
            if m not in (AddrMode.REG, AddrMode.REG_INDIRECT):
                continue

            is_write = (
                i == 0 and m == AddrMode.REG and instr.opcode not in NON_WRITING_OPCODES
            )

            for prev in self.rs:
                if prev == instr:
                    break

                prev_writes = (
                    prev.modes
                    and prev.modes[0] == AddrMode.REG
                    and prev.args[0] == a
                    and prev.opcode not in NON_WRITING_OPCODES
                )

                if prev_writes and prev.result is None:
                    return True

                if is_write and prev.operands is None:
                    for p_i, (p_m, p_a) in enumerate(zip(prev.modes, prev.args)):
                        if p_m == AddrMode.REG_INDIRECT and p_a == a:
                            return True
                        if p_m == AddrMode.REG and p_a == a:
                            p_is_write = (
                                p_i == 0 and prev.opcode not in NON_WRITING_OPCODES
                            )
                            if not p_is_write or prev.opcode != Opcode.MOV:
                                return True

        if instr.opcode == Opcode.POP:
            for prev in self.rs:
                if prev == instr:
                    break
                if prev.opcode == Opcode.PUSH and prev.operands is None:
                    return True

        return False

    def step_fetch(self):
        limit = 8 if self.superscalar else 5
        if len(self.fetch_buffer) < limit and not self.halted:
            self.fetch_buffer.append((self.fetch_pc, self.dp.mem[self.fetch_pc]))
            self.fetch_pc += 1

    def step_decode(self):
        limit = 8 if self.superscalar else 4
        if self.fetch_buffer and len(self.decode_queue) < limit:
            pc, h = self.fetch_buffer[0]
            try:
                op, cnt = Opcode(h & 0xFF), (h >> 8) & 0xF
                if len(self.fetch_buffer) >= 1 + cnt:
                    self.fetch_buffer.pop(0)
                    ms = [AddrMode((h >> (12 + i * 4)) & 0xF) for i in range(cnt)]
                    args = [self.fetch_buffer.pop(0)[1] for _ in range(cnt)]
                    self.decode_queue.append(Instruction(op, ms, args, pc))
            except ValueError:
                self.fetch_buffer.pop(0)

    def step_dispatch(self):
        width = 2 if self.superscalar else 1
        rs_limit = 4 if self.superscalar else 1
        dispatched = 0

        BARRIER_OPS = {
            Opcode.JMP,
            Opcode.JEQ,
            Opcode.JGT,
            Opcode.CALL,
            Opcode.RET,
            Opcode.IRET,
            Opcode.HALT,
        }

        for instr in self.decode_queue:
            if dispatched >= width or len(self.rs) >= rs_limit:
                break

            if instr in self.rs or instr.state in [
                InstrState.EXECUTING,
                InstrState.WRITING_BACK,
                InstrState.RETIRED,
            ]:
                continue

            idx = self.decode_queue.index(instr)
            older_instrs = self.decode_queue[:idx]

            if instr.opcode in BARRIER_OPS and older_instrs:
                break

            if any(p.opcode in BARRIER_OPS for p in older_instrs):
                break

            if self._is_ready_to_dispatch(instr):
                self.rs.append(instr)
                dispatched += 1

    def step_execute(self):
        exec_limit = 2 if self.superscalar else 1
        executed_this_tick = 0
        to_remove = []

        for instr in self.rs:
            if executed_this_tick >= exec_limit:
                break

            executed_this_tick += 1

            if instr.state == InstrState.FETCHING_OPERANDS:
                if instr.cycles_left > 0:
                    instr.cycles_left -= 1
                else:
                    instr.operands = [
                        self._read_forward(m, a, instr)
                        for m, a in zip(instr.modes, instr.args)
                    ]
                    instr.state = InstrState.EXECUTING

            elif instr.state == InstrState.EXECUTING:
                op, vals = instr.opcode, instr.operands
                if op in [Opcode.JMP, Opcode.JEQ, Opcode.JGT]:
                    cond = (
                        (op == Opcode.JMP)
                        or (op == Opcode.JEQ and self.dp.z)
                        or (
                            op == Opcode.JGT
                            and not self.dp.z
                            and self.dp.n == self.dp.v
                        )
                    )
                    if cond:
                        instr.result = instr.args[0]
                    else:
                        instr.result = None
                    instr.state = InstrState.WRITING_BACK
                    to_remove.append(instr)
                elif op in [Opcode.HALT, Opcode.CALL, Opcode.RET]:
                    instr.state = InstrState.WRITING_BACK
                    to_remove.append(instr)
                elif op in [
                    Opcode.MOV,
                    Opcode.ADD,
                    Opcode.SUB,
                    Opcode.MUL,
                    Opcode.CMP,
                    Opcode.ADC,
                    Opcode.SBC,
                    Opcode.MOD,
                ]:
                    carry = int(self.dp.c) if op in [Opcode.ADC, Opcode.SBC] else 0
                    if op == Opcode.MOV:
                        instr.result, _, _, _, _ = self.dp.alu(op, vals[1])
                    else:
                        v1, v2 = (
                            (vals[1], vals[2]) if len(vals) > 2 else (vals[0], vals[1])
                        )
                        instr.result, instr.f_z, instr.f_n, instr.f_c, instr.f_v = (
                            self.dp.alu(op, v1, v2, carry)
                        )
                    instr.state = InstrState.WRITING_BACK
                    to_remove.append(instr)
                elif op == Opcode.PUSH:
                    instr.result = vals[0]
                    instr.state = InstrState.WRITING_BACK
                    to_remove.append(instr)
                elif op == Opcode.POP:
                    instr.result = self._read_forward(
                        AddrMode.MEM, self.dp.regs[Registers.SP], instr
                    )
                    instr.state = InstrState.WRITING_BACK
                    to_remove.append(instr)
                elif op == Opcode.IRET:
                    instr.state = InstrState.WRITING_BACK
                    to_remove.append(instr)

                self.retire_buffer.append(instr)

        for instr in to_remove:
            if instr in self.rs:
                self.rs.remove(instr)

    def step_retire(self):
        while self.decode_queue:
            instr = self.decode_queue[0]

            if instr not in self.retire_buffer:
                break

            if not hasattr(instr, "write_lat"):
                if instr.modes and instr.modes[0] == AddrMode.MEM:
                    instr.write_lat = 2
                elif instr.modes and instr.modes[0] == AddrMode.REG_INDIRECT:
                    instr.write_lat = 3
                else:
                    instr.write_lat = 1

            if instr.write_lat > 1:
                instr.write_lat -= 1
                break

            self.decode_queue.pop(0)
            self.retire_buffer.remove(instr)

            if instr.state == InstrState.RETIRED:
                continue

            if instr.opcode in [Opcode.JMP, Opcode.JEQ, Opcode.JGT]:
                if instr.result is not None:
                    self.pipeline_flush(instr.result)
                continue

            if instr.opcode in FLAG_WRITERS:
                self.dp.z = instr.f_z
                self.dp.n = instr.f_n
                self.dp.c = instr.f_c
                self.dp.v = instr.f_v

            if instr.opcode == Opcode.HALT:
                self.halted = True
                break
            if instr.opcode == Opcode.IRET:
                self.cu_state = CUState.IRET_SEQ
                self.seq_step = 0
                return
            if instr.opcode == Opcode.CALL:
                self.cu_state = CUState.CALL_SEQ
                self.seq_step = 0
                self.seq_data = {
                    "target": instr.args[0],
                    "ret_pc": instr.pc + 1 + len(instr.args),
                }
                return
            if instr.opcode == Opcode.RET:
                self.cu_state = CUState.RET_SEQ
                self.seq_step = 0
                return

            if instr.opcode == Opcode.PUSH:
                self.dp.regs[Registers.SP] -= 1
                self.dp.mem[self.dp.regs[Registers.SP]] = instr.result
            elif instr.opcode == Opcode.POP:
                self.dp.regs[Registers.SP] += 1
                self.dp.regs[instr.args[0]] = instr.result
            elif instr.opcode != Opcode.CMP and instr.result is not None:
                m, a = instr.modes[0], instr.args[0]
                val = instr.result & 0xFFFFFFFF
                if m == AddrMode.REG:
                    self.dp.regs[a] = val
                else:
                    addr = a if m == AddrMode.MEM else self.dp.regs[a]
                    self.dp.mem[addr] = val
                    if addr == OUT_CHAR:
                        self.output.append(chr(val & 0xFF))
                    elif addr == OUT_INT:
                        self.output.append(str(val))
            instr.state = InstrState.RETIRED

    def process_call_sequence(self):
        self.seq_step += 1
        if self.seq_step == 1:
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.seq_data["ret_pc"]
            logging.info(
                f"TICK {self.tick:4} | [CALL] Push RetPC {self.seq_data['ret_pc']}"
            )
        elif self.seq_step == 2:
            self.pipeline_flush(self.seq_data["target"])
            self.cu_state = CUState.NORMAL

    def process_ret_sequence(self):
        self.seq_step += 1
        if self.seq_step == 1:
            self.seq_data = self.dp.mem[self.dp.regs[Registers.SP]]
        elif self.seq_step == 2:
            self.dp.regs[Registers.SP] += 1
        elif self.seq_step == 3:
            self.pipeline_flush(self.seq_data)
            self.cu_state = CUState.NORMAL

    def process_interrupt_sequence(self):
        self.seq_step += 1
        s = self.seq_step
        if s == 1:
            self.ie = False
            self.dp.mem[TRAP_BUFFER] = ord(self.seq_data["char"])
            logging.info(f"TICK {self.tick:4} | [TRAP] Char saved")
        elif s == 2:
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.seq_data["ret_pc"]
        elif s == 3:
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.dp.get_flags()
        elif s <= 7:
            reg = s - 4
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.dp.regs[reg]
        elif s == 8:
            self.pipeline_flush(VECTOR_TRAP)
            self.cu_state = CUState.NORMAL
            self.seq_step = 0

    def process_iret_sequence(self):
        self.seq_step += 1
        s = self.seq_step
        if s <= 4:
            reg = 4 - s
            self.dp.regs[reg] = self.dp.mem[self.dp.regs[Registers.SP]]
            self.dp.regs[Registers.SP] += 1
        elif s == 5:
            self.dp.set_flags(self.dp.mem[self.dp.regs[Registers.SP]])
            self.dp.regs[Registers.SP] += 1
        elif s == 6:
            ret_pc = self.dp.mem[self.dp.regs[Registers.SP]]
            self.dp.regs[Registers.SP] += 1
            self.ie = True
            self.pipeline_flush(ret_pc)
            self.cu_state = CUState.NORMAL
            self.seq_step = 0

    def tick_machine(self):
        self.tick += 1
        if self.cu_state != CUState.NORMAL:
            if self.cu_state == CUState.INTERRUPT_SEQ:
                self.process_interrupt_sequence()
            elif self.cu_state == CUState.IRET_SEQ:
                self.process_iret_sequence()
            elif self.cu_state == CUState.CALL_SEQ:
                self.process_call_sequence()
            elif self.cu_state == CUState.RET_SEQ:
                self.process_ret_sequence()
        else:
            if self.schedule and self.tick >= self.schedule[0]["tick"]:
                event = self.schedule.pop(0)
                if self.ie:
                    if self.decode_queue:
                        ret_pc = self.decode_queue[0].pc
                    elif self.fetch_buffer:
                        ret_pc = self.fetch_buffer[0][0]
                    else:
                        ret_pc = self.fetch_pc

                    self.pipeline_flush(VECTOR_TRAP)

                    self.cu_state = CUState.INTERRUPT_SEQ
                    self.seq_step = 0
                    self.seq_data = {"char": event["char"], "ret_pc": ret_pc}

                    sym = event["char"].replace("\n", "\\n").replace("\0", "\\0")

                    logging.info(
                        f"TICK {self.tick:4} | [INTERRUPT] Accepted sym {sym}. RetPC set to: {ret_pc}"
                    )
                    return

            self.step_execute()
            self.step_retire()
            self.step_dispatch()
            self.step_decode()
            self.step_fetch()

        if self.tick < 1000 or self.tick % 500 == 0:
            mode = "SS" if self.superscalar else "SC"
            rs_s = ",".join([str(i) for i in self.rs])
            logging.info(
                f"TICK {self.tick:4} | {mode} | PC:{self.fetch_pc:3} | RS:[{rs_s:20}] | SP:{self.dp.regs[Registers.SP]} Z:{int(self.dp.z)}"
            )


def main(target, sched_file=None, superscalar_str="True"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )
    with open(target, "rb") as f:
        mem = from_bytes(f.read())
    sch = []
    if sched_file and sched_file.lower() != "none":
        try:
            with open(sched_file, "r") as f:
                sch = [{"tick": t, "char": c} for t, c in ast.literal_eval(f.read())]
        except (FileNotFoundError, ValueError, SyntaxError):
            pass

    is_ss = superscalar_str.lower() not in ["false", "0", "off"]
    cu = ControlUnit(DataPath(mem), sch, superscalar=is_ss)
    try:
        while cu.tick < 10_000 and not cu.halted:
            cu.tick_machine()
    except StopIteration:
        pass
    logging.info(f"\nTicks: {cu.tick}\nOutput: {''.join(cu.output)}")


if __name__ == "__main__":
    args = sys.argv[1:]

    if len(args) == 1:
        main(args[0])

    elif len(args) == 2:
        if args[1].lower() in ["true", "false", "0", "off", "on"]:
            main(args[0], sched_file=None, superscalar_str=args[1])
        else:
            main(args[0], args[1])

    elif len(args) >= 3:
        main(args[0], args[1], args[2])
