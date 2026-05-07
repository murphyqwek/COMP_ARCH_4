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


class Instruction:
    def __init__(self, opcode, modes, args, pc):
        self.opcode = opcode
        self.modes = modes
        self.args = args
        self.pc = pc
        self.state = InstrState.FETCHING_OPERANDS
        self.result = None
        self.operands = None
        self.cycles_left = 0
        for m in modes:
            if m in [AddrMode.MEM, AddrMode.REG_INDIRECT]:
                self.cycles_left += 1

    def modifies_sp(self):
        return self.opcode in [
            Opcode.PUSH,
            Opcode.POP,
            Opcode.CALL,
            Opcode.RET,
            Opcode.IRET,
        ]

    def writes_to_reg(self):
        if not self.args or not self.modes or self.modes[0] != AddrMode.REG:
            return False
        if self.opcode in [
            Opcode.CMP,
            Opcode.PUSH,
            Opcode.JMP,
            Opcode.JEQ,
            Opcode.JGT,
            Opcode.CALL,
            Opcode.HALT,
        ]:
            return False
        return True

    def get_target_reg(self):
        if self.writes_to_reg():
            return self.args[0]
        if self.modifies_sp():
            return Registers.SP
        return None

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
        if op in [Opcode.MOV, Opcode.PUSH, Opcode.POP]:
            return a
        res = 0
        if op in [Opcode.ADD, Opcode.ADC]:
            res = a + b + carry
        elif op in [Opcode.SUB, Opcode.SBC, Opcode.CMP]:
            res = a - b - carry
        elif op == Opcode.MUL:
            res = a * b
        elif op == Opcode.MOD:
            res = a % b if b != 0 else 0

        res32 = res & 0xFFFFFFFF
        self.z = res32 == 0
        self.n = bool(res32 & 0x80000000)
        if op in [Opcode.ADD, Opcode.ADC, Opcode.SUB, Opcode.SBC, Opcode.CMP]:
            self.c = res > 0xFFFFFFFF or res < 0
        if op in [Opcode.ADD, Opcode.ADC]:
            self.v = bool((~(a ^ b) & (a ^ res32) & 0x80000000))
        elif op in [Opcode.SUB, Opcode.SBC, Opcode.CMP]:
            self.v = bool(((a ^ b) & (a ^ res32) & 0x80000000))
        return res32


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
        self.rs = []
        self.superscalar = superscalar
        self.current_executing = []

    def pipeline_flush(self, new_pc):
        self.fetch_pc = new_pc
        self.fetch_buffer.clear()
        self.decode_queue.clear()
        self.rs.clear()

    def _read_forward(self, mode, val, current_instr):
        if mode == AddrMode.IMM:
            return val

        if mode == AddrMode.REG:
            try:
                idx = self.rs.index(current_instr)
                for prev in reversed(self.rs[:idx]):
                    if prev.writes_to_reg() and prev.args[0] == val:
                        return prev.result
            except ValueError:
                pass
            return self.dp.regs[val]

        if mode == AddrMode.MEM:
            return self.dp.mem[val]

        if mode == AddrMode.REG_INDIRECT:
            addr = self._read_forward(AddrMode.REG, val, current_instr)
            if addr is None:
                return None
            return self.dp.mem[addr]

        return 0

    def _check_hazard(self, instr):
        try:
            idx = self.rs.index(instr)
            older = self.rs[:idx]
        except ValueError:
            return False

        for i, (m, a) in enumerate(zip(instr.modes, instr.args)):
            is_read = (i > 0) or (instr.opcode in [Opcode.CMP, Opcode.PUSH])
            if is_read and m == AddrMode.REG:
                for prev in older:
                    if prev.get_target_reg() == a:
                        if prev.result is None and prev.state != InstrState.RETIRED:
                            return True

        curr_target = instr.get_target_reg()
        if curr_target is None:
            for prev in older:
                if prev.get_target_reg() == curr_target:
                    if prev.state != InstrState.RETIRED:
                        return True

        curr_uses_mem = any(
            mod in [AddrMode.MEM, AddrMode.REG_INDIRECT] for mod in instr.modes
        )
        curr_uses_mem |= instr.modifies_sp()

        if curr_uses_mem:
            for prev in older:
                prev_uses_mem = any(
                    mod in [AddrMode.MEM, AddrMode.REG_INDIRECT] for mod in prev.modes
                )
                prev_uses_mem |= prev.modifies_sp()
                if prev_uses_mem and prev.state != InstrState.RETIRED:
                    return True

        if instr.opcode in [Opcode.JEQ, Opcode.JGT, Opcode.ADC, Opcode.SBC]:
            for prev in older:
                if prev.opcode in [
                    Opcode.ADD,
                    Opcode.SUB,
                    Opcode.MUL,
                    Opcode.CMP,
                    Opcode.ADC,
                    Opcode.SBC,
                    Opcode.MOD,
                ]:
                    if prev.state != InstrState.RETIRED:
                        return True

        return False

    def step_fetch(self):
        fetch_width = 4 if self.superscalar else 1
        limit = 12 if self.superscalar else 5
        for _ in range(fetch_width):
            if len(self.fetch_buffer) < limit and not self.halted:
                self.fetch_buffer.append((self.fetch_pc, self.dp.mem[self.fetch_pc]))
                self.fetch_pc += 1

    def step_decode(self):
        decode_width = 2 if self.superscalar else 1
        for _ in range(decode_width):
            if self.fetch_buffer:
                pc, h = self.fetch_buffer[0]
                try:
                    op, cnt = Opcode(h & 0xFF), (h >> 8) & 0xF
                    if len(self.fetch_buffer) >= 1 + cnt:
                        self.fetch_buffer.pop(0)
                        ms = [AddrMode((h >> (12 + i * 4)) & 0xF) for i in range(cnt)]
                        args = [self.fetch_buffer.pop(0)[1] for _ in range(cnt)]
                        self.decode_queue.append(Instruction(op, ms, args, pc))
                    else:
                        break
                except ValueError:
                    self.fetch_buffer.pop(0)

    def step_dispatch(self):
        width = 2 if self.superscalar else 1
        rs_limit = 6 if self.superscalar else 1
        for _ in range(width):
            if len(self.rs) < rs_limit and self.decode_queue:
                self.rs.append(self.decode_queue.pop(0))

    def step_execute(self):
        exec_limit = 2 if self.superscalar else 1
        executed_this_tick = 0

        for instr in list(self.rs):
            if executed_this_tick >= exec_limit:
                break

            if instr.state == InstrState.FETCHING_OPERANDS:
                if self._check_hazard(instr):
                    continue
                if instr.cycles_left > 0:
                    instr.cycles_left -= 1
                    executed_this_tick += 1
                    self.current_executing.append(f"{instr.opcode.name}:MEM@{instr.pc}")
                    continue
                else:
                    instr.operands = [
                        self._read_forward(m, a, instr)
                        for m, a in zip(instr.modes, instr.args)
                    ]
                    instr.state = InstrState.EXECUTING

            if instr.state == InstrState.EXECUTING:
                if executed_this_tick >= exec_limit:
                    break
                executed_this_tick += 1
                self.current_executing.append(f"{instr.opcode.name}:ALU@{instr.pc}")

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
                        try:
                            idx = self.rs.index(instr)
                            self.fetch_pc = instr.args[0]
                            self.fetch_buffer.clear()
                            self.decode_queue.clear()
                            self.rs = self.rs[: idx + 1]
                        except ValueError:
                            pass
                    instr.state = InstrState.RETIRED

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
                        instr.result = vals[1]
                    else:
                        v1, v2 = (
                            (vals[1], vals[2]) if len(vals) > 2 else (vals[0], vals[1])
                        )
                        instr.result = self.dp.alu(op, v1, v2, carry)
                    instr.state = InstrState.WRITING_BACK

                elif op == Opcode.PUSH:
                    instr.result = vals[0]
                    instr.state = InstrState.WRITING_BACK

                elif op == Opcode.POP:
                    instr.result = self.dp.mem[self.dp.regs[Registers.SP]]
                    instr.state = InstrState.WRITING_BACK

                elif op in [Opcode.HALT, Opcode.IRET, Opcode.CALL, Opcode.RET]:
                    instr.state = InstrState.WRITING_BACK

    def step_retire(self):
        while self.rs and self.rs[0].state in [
            InstrState.WRITING_BACK,
            InstrState.RETIRED,
        ]:
            instr = self.rs.pop(0)
            if instr.state == InstrState.RETIRED:
                continue

            if instr.opcode == Opcode.PUSH:
                self.dp.regs[Registers.SP] -= 1
                self.dp.mem[self.dp.regs[Registers.SP]] = instr.result
            elif instr.opcode == Opcode.POP:
                self.dp.regs[instr.args[0]] = instr.result
                self.dp.regs[Registers.SP] += 1

            elif instr.opcode == Opcode.HALT:
                self.halted = True
                break
            elif instr.opcode == Opcode.IRET:
                self.cu_state = CUState.IRET_SEQ
                self.seq_step = 0
                return
            elif instr.opcode == Opcode.CALL:
                self.cu_state = CUState.CALL_SEQ
                self.seq_step = 0
                self.seq_data = {
                    "target": instr.args[0],
                    "ret_pc": instr.pc + 1 + len(instr.args),
                }
                return
            elif instr.opcode == Opcode.RET:
                self.cu_state = CUState.RET_SEQ
                self.seq_step = 0
                return

            elif instr.opcode != Opcode.CMP and instr.result is not None:
                m, a = instr.modes[0], instr.args[0]
                if m == AddrMode.REG:
                    self.dp.regs[a] = instr.result
                else:
                    addr = a if m == AddrMode.MEM else self.dp.regs[a]
                    self.dp.mem[addr] = instr.result
                    if addr == OUT_CHAR:
                        self.output.append(chr(instr.result & 0xFF))
                    elif addr == OUT_INT:
                        self.output.append(str(instr.result))

            instr.state = InstrState.RETIRED

    def process_call_sequence(self):
        self.seq_step += 1
        if self.seq_step == 1:
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.seq_data["ret_pc"]
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
            sym = self.seq_data["char"].replace("\n", "\\n").replace("\0", "\\0")
            logging.info(
                f"TICK {self.tick:4} | [TRAP-SEQ] IE=0, Save '{sym}' to [{TRAP_BUFFER}]"
            )
        elif s == 2:
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.seq_data["ret_pc"]
            logging.info(
                f"TICK {self.tick:4} | [TRAP-SEQ] Stack Push RetPC:{self.seq_data['ret_pc']} (SP={self.dp.regs[Registers.SP]})"
            )
        elif s == 3:
            self.dp.regs[Registers.SP] -= 1
            f = self.dp.get_flags()
            self.dp.mem[self.dp.regs[Registers.SP]] = f
            logging.info(
                f"TICK {self.tick:4} | [TRAP-SEQ] Stack Push Flags:{bin(f)} (SP={self.dp.regs[Registers.SP]})"
            )
        elif s <= 7:
            reg = s - 4
            self.dp.regs[Registers.SP] -= 1
            self.dp.mem[self.dp.regs[Registers.SP]] = self.dp.regs[reg]
            logging.info(
                f"TICK {self.tick:4} | [TRAP-SEQ] Stack Push R{reg}:{self.dp.regs[reg]} (SP={self.dp.regs[Registers.SP]})"
            )
        elif s == 8:
            logging.info(
                f"TICK {self.tick:4} | [TRAP-SEQ] Jump to VECTOR_TRAP (0x{VECTOR_TRAP:02X})"
            )
            self.pipeline_flush(VECTOR_TRAP)
            self.cu_state = CUState.NORMAL

    def process_iret_sequence(self):
        self.seq_step += 1
        s = self.seq_step
        if s <= 4:
            reg = 4 - s
            self.dp.regs[reg] = self.dp.mem[self.dp.regs[Registers.SP]]
            logging.info(
                f"TICK {self.tick:4} | [IRET-SEQ] Pop R{reg}:{self.dp.regs[reg]} (SP now {self.dp.regs[Registers.SP] + 1})"
            )
            self.dp.regs[Registers.SP] += 1
        elif s == 5:
            f = self.dp.mem[self.dp.regs[Registers.SP]]
            self.dp.set_flags(f)
            logging.info(
                f"TICK {self.tick:4} | [IRET-SEQ] Pop Flags:{bin(f)} (SP now {self.dp.regs[Registers.SP] + 1})"
            )
            self.dp.regs[Registers.SP] += 1
        elif s == 6:
            ret_pc = self.dp.mem[self.dp.regs[Registers.SP]]
            self.dp.regs[Registers.SP] += 1
            self.ie = True
            logging.info(
                f"TICK {self.tick:4} | [IRET-SEQ] Pop RetPC:{ret_pc}, IE=1. Returning..."
            )
            self.pipeline_flush(ret_pc)
            self.cu_state = CUState.NORMAL

    def tick_machine(self):
        self.tick += 1
        self.current_executing = []

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
                    ret_pc = (
                        self.rs[0].pc
                        if self.rs
                        else (
                            self.decode_queue[0].pc
                            if self.decode_queue
                            else self.fetch_pc
                        )
                    )

                    sym = event["char"].replace("\n", "\\n").replace("\0", "\\0")
                    logging.info(
                        f"TICK {self.tick:4} | [!] INTERRUPT TRIGGERED: '{sym}' (Return to PC:{ret_pc})"
                    )

                    self.cu_state = CUState.INTERRUPT_SEQ
                    self.seq_step = 0
                    self.seq_data = {"char": event["char"], "ret_pc": ret_pc}
                    return

            self.step_retire()
            self.step_execute()
            self.step_dispatch()
            self.step_decode()
            self.step_fetch()

        if self.tick < 500:
            exec_s = (
                " + ".join(self.current_executing) if self.current_executing else "IDLE"
            )
            logging.info(
                f"TICK {self.tick:4} | "
                f"EXEC: {exec_s:25} | "
                f"RS: {[str(i) for i in self.rs]}"
            )


def main(target, sched_file=None, superscalar_str="True"):
    logging.basicConfig(level=logging.INFO, format="%(message)s")

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
    mode_name = "SS (Superscalar)" if is_ss else "SC (Scalar)"
    logging.info(f"--- STARTING SIMULATION IN {mode_name} MODE ---")

    cu = ControlUnit(DataPath(mem), sch, superscalar=is_ss)

    try:
        while cu.tick < 500000 and not cu.halted:
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
