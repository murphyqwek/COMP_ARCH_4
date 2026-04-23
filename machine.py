import sys
import logging
import ast

from isa import Opcode, AddrMode, Registers, from_bytes
from const import VECTOR_TRAP, OUT_INT, OUT_CHAR, TRAP_BUFFER


class DataPath:
    def __init__(self, memory_words):
        self.mem = memory_words + [0] * (10000 - len(memory_words))
        self.regs = [0] * 5
        self.regs[Registers.SP] = 9000
        self.output_buf = []
        self.n, self.z, self.c, self.v = False, False, False, False

    def read(self, mode, val):
        if mode == AddrMode.IMM:
            return val & 0xFFFFFFFF
        if mode == AddrMode.MEM:
            return self.mem[val] & 0xFFFFFFFF
        if mode == AddrMode.REG:
            return self.regs[val] & 0xFFFFFFFF
        if mode == AddrMode.REG_INDIRECT:
            return self.mem[self.regs[val]] & 0xFFFFFFFF
        return 0

    def write(self, mode, addr, val):
        val &= 0xFFFFFFFF
        if mode == AddrMode.REG:
            self.regs[addr] = val
        elif mode in (AddrMode.MEM, AddrMode.REG_INDIRECT):
            target_addr = self.regs[addr] if mode == AddrMode.REG_INDIRECT else addr
            self.mem[target_addr] = val
            if target_addr == OUT_CHAR:
                self.output_buf.append(chr(val & 0xFF))
            elif target_addr == OUT_INT:
                self.output_buf.append(str(val))

    def push(self, val):
        self.regs[Registers.SP] -= 1
        self.mem[self.regs[Registers.SP]] = val & 0xFFFFFFFF

    def pop(self):
        val = self.mem[self.regs[Registers.SP]]
        self.regs[Registers.SP] += 1
        return val & 0xFFFFFFFF

    def _update_flags_add(self, a, b, res):
        res32 = res & 0xFFFFFFFF
        self.c = res > 0xFFFFFFFF
        sa, sb, sr = (a >> 31) & 1, (b >> 31) & 1, (res32 >> 31) & 1
        self.v = bool((sa == sb) and (sa != sr))
        self.z = res32 == 0
        self.n = bool(sr)
        return res32

    def _update_flags_sub(self, a, b, res):
        res32 = res & 0xFFFFFFFF
        self.c = res < 0
        sa, sb, sr = (a >> 31) & 1, (b >> 31) & 1, (res32 >> 31) & 1
        self.v = bool((sa != sb) and (sa != sr))
        self.z = res32 == 0
        self.n = bool(sr)
        return res32

    def execute_mov(self, m, a):
        val = self.read(m[1], a[1])
        self.write(m[0], a[0], val)

    def execute_add(self, m, a, use_carry=False):
        ar = self.read(m[1], a[1])
        carry = int(self.c) if use_carry else 0
        for i in range(2, len(a)):
            op2 = self.read(m[i], a[i])
            ar = self._update_flags_add(ar, op2 + carry, ar + op2 + carry)
            carry = 0
        self.write(m[0], a[0], ar)

    def execute_sub(self, m, a, use_borrow=False):
        ar = self.read(m[1], a[1])
        borrow = int(self.c) if use_borrow else 0
        for i in range(2, len(a)):
            op2 = self.read(m[i], a[i])
            ar = self._update_flags_sub(ar, op2 + borrow, ar - op2 - borrow)
            borrow = 0
        self.write(m[0], a[0], ar)

    def execute_mul(self, m, a):
        ar = 1
        for i in range(1, len(a)):
            ar *= self.read(m[i], a[i])
        self.write(m[0], a[0], ar)

    def execute_cmp(self, m, a):
        op1 = self.read(m[0], a[0])
        op2 = self.read(m[1], a[1])
        self._update_flags_sub(op1, op2, op1 - op2)


class ControlUnit:
    def __init__(self, dp, schedule=None):
        self.dp = dp
        self.pc, self.tick, self.sequential_tick = 0, 0, 0
        self.schedule = schedule or []
        self.ie = True

        self.i1 = None
        self.i2 = None

    def format_instruction(self, i):
        op = i["op"].name
        m = i["m"]
        a = i["args"]

        formatted_args = []
        for mode, val in zip(m, a):
            if mode == AddrMode.REG:
                formatted_args.append(Registers(val).name)
            elif mode == AddrMode.REG_INDIRECT:
                formatted_args.append(f"[{Registers(val).name}]")
            elif mode == AddrMode.IMM:
                formatted_args.append(f"#{val}")
            elif mode == AddrMode.MEM:
                formatted_args.append(f"[{val}]")
            else:
                formatted_args.append(str(val))

        return f"{op:<7} {', '.join(formatted_args)}"

    def fetch(self, pc):
        h = self.dp.mem[pc]
        op = Opcode(h & 0xFF)
        arg_count = (h >> 8) & 0xF
        m = [AddrMode((h >> (12 + i * 4)) & 0xF) for i in range(arg_count)]
        args = [self.dp.mem[pc + 1 + i] for i in range(arg_count)]
        return {"op": op, "m": m, "args": args, "len": 1 + arg_count, "pc": pc}

    def check_interrupts(self):
        steps = 0
        if self.schedule and self.tick >= self.schedule[0]["tick"] and self.ie:
            event = self.schedule.pop(0)

            if self.tick < 1500:
                symbol = event["char"]
                if symbol == "\n":
                    symbol = "\\n"
                elif symbol == "\0":
                    symbol = "\\0"
                logging.info(f"--- TRAP: {symbol} at {self.tick} ---")
            self.dp.push(self.pc)
            flags = (self.dp.n << 3) | (self.dp.z << 2) | (self.dp.c << 1) | self.dp.v
            self.dp.push(flags)
            for r in [Registers.R0, Registers.R1, Registers.R2, Registers.R3]:
                self.dp.push(self.dp.regs[r])
            self.ie = False
            self.dp.mem[TRAP_BUFFER] = ord(event["char"])
            self.pc = VECTOR_TRAP
            steps = 2 + 2 + 4 * 2

        return steps

    def count_steps_for_inscruction(self, op, modes):
        if op in [Opcode.NOP, Opcode.HALT, Opcode.JMP, Opcode.JEQ, Opcode.JGT]:
            return 1
        if op in [Opcode.PUSH, Opcode.POP]:
            return 2
        if op in [Opcode.MOV]:
            if modes[0] == AddrMode.MEM:
                if modes[1] == AddrMode.IMM:
                    return 2
                if modes[1] == AddrMode.MEM:
                    return 4
                if modes[1] == AddrMode.REG:
                    return 2
                if modes[1] == AddrMode.REG_INDIRECT:
                    return 5
            if modes[0] == AddrMode.REG:
                if modes[1] == AddrMode.IMM:
                    return 1
                if modes[1] == AddrMode.MEM:
                    return 2
                if modes[1] == AddrMode.REG:
                    return 1
                if modes[1] == AddrMode.REG_INDIRECT:
                    return 3
            if modes[0] == AddrMode.REG_INDIRECT:
                if modes[1] == AddrMode.IMM:
                    return 3
                if modes[1] == AddrMode.MEM:
                    return 5
                if modes[1] == AddrMode.REG:
                    return 3
                if modes[1] == AddrMode.REG_INDIRECT:
                    return 6
        if op in [Opcode.ADD, Opcode.SUB, Opcode.ADC, Opcode.SBC, Opcode.MUL]:
            steps = 0

            for m in modes[1:]:
                if m == AddrMode.IMM:
                    steps += 1
                elif m == AddrMode.MEM:
                    steps += 2
                elif m == AddrMode.REG:
                    steps += 1
                elif m == AddrMode.REG_INDIRECT:
                    steps += 3
                elif m == AddrMode.NONE:
                    continue

            if modes[0] == AddrMode.MEM:
                steps += 2
            elif modes[0] == AddrMode.REG:
                steps += 0
            elif modes[0] == AddrMode.REG_INDIRECT:
                steps += 3
            return steps
        if op in [Opcode.CMP]:
            if modes[0] == AddrMode.MEM:
                if modes[1] == AddrMode.IMM:
                    return 2
                if modes[1] == AddrMode.MEM:
                    return 4
                if modes[1] == AddrMode.REG:
                    return 2
                if modes[1] == AddrMode.REG_INDIRECT:
                    return 5
            if modes[0] == AddrMode.REG or modes[0] == AddrMode.IMM:
                if modes[1] == AddrMode.IMM:
                    return 1
                if modes[1] == AddrMode.MEM:
                    return 2
                if modes[1] == AddrMode.REG:
                    return 1
                if modes[1] == AddrMode.REG_INDIRECT:
                    return 3
            if modes[0] == AddrMode.REG_INDIRECT:
                if modes[1] == AddrMode.IMM:
                    return 3
                if modes[1] == AddrMode.MEM:
                    return 5
                if modes[1] == AddrMode.REG:
                    return 3
                if modes[1] == AddrMode.REG_INDIRECT:
                    return 6
        if op in [Opcode.CALL]:
            return 3
        if op in [Opcode.RET]:
            return 3
        if op in [Opcode.IRET]:
            return 5 * 2 + 2 + 2

    def dispatch(self, i):
        op, m, a = i["op"], i["m"], i["args"]
        if op == Opcode.MOV:
            self.dp.execute_mov(m, a)
        elif op == Opcode.ADD:
            self.dp.execute_add(m, a, False)
        elif op == Opcode.ADC:
            self.dp.execute_add(m, a, True)
        elif op == Opcode.SUB:
            self.dp.execute_sub(m, a, False)
        elif op == Opcode.SBC:
            self.dp.execute_sub(m, a, True)
        elif op == Opcode.MUL:
            self.dp.execute_mul(m, a)
        elif op == Opcode.CMP:
            self.dp.execute_cmp(m, a)
        elif op == Opcode.PUSH:
            self.dp.push(self.dp.read(m[0], a[0]))
        elif op == Opcode.POP:
            self.dp.write(m[0], a[0], self.dp.pop())
        elif op == Opcode.JMP:
            self.pc = a[0]
        elif op == Opcode.JEQ and self.dp.z:
            self.pc = a[0]
        elif op == Opcode.JGT and (not self.dp.z and self.dp.n == self.dp.v):
            self.pc = a[0]
        elif op == Opcode.CALL:
            self.dp.push(self.pc)
            self.pc = a[0]
        elif op == Opcode.RET:
            self.pc = self.dp.pop()
        elif op == Opcode.IRET:
            for r in reversed([Registers.R0, Registers.R1, Registers.R2, Registers.R3]):
                self.dp.regs[r] = self.dp.pop()
            f = self.dp.pop()
            self.dp.n, self.dp.z, self.dp.c, self.dp.v = (
                bool(f & 8),
                bool(f & 4),
                bool(f & 2),
                bool(f & 1),
            )
            self.pc = self.dp.pop()
            self.ie = True
        elif op == Opcode.HALT:
            raise StopIteration()

        return self.count_steps_for_inscruction(op, m)

    def has_dep(self, i1, i2):
        def get_rw(inst):
            w, r = [], []
            op, m, a = inst["op"], inst["m"], inst["args"]
            wf = op in [
                Opcode.ADD,
                Opcode.SUB,
                Opcode.MUL,
                Opcode.ADC,
                Opcode.SBC,
                Opcode.CMP,
            ]
            rf = op in [Opcode.ADC, Opcode.SBC, Opcode.JEQ, Opcode.JGT]
            if op in [
                Opcode.ADD,
                Opcode.SUB,
                Opcode.MUL,
                Opcode.ADC,
                Opcode.SBC,
                Opcode.MOV,
            ]:
                w.append((m[0], a[0]))
                for idx in range(1, len(a)):
                    r.append((m[idx], a[idx]))
            elif op == Opcode.CMP:
                r.append((m[0], a[0]))
                r.append((m[1], a[1]))
            elif op in [Opcode.PUSH, Opcode.POP]:
                w.append((AddrMode.REG, Registers.SP))
                r.append((AddrMode.REG, Registers.SP))
                if op == Opcode.PUSH:
                    r.append((m[0], a[0]))
                else:
                    w.append((m[0], a[0]))
            return w, r, wf, rf

        w1, r1, wf1, rf1 = get_rw(i1)
        w2, r2, wf2, rf2 = get_rw(i2)
        if (wf1 and (wf2 or rf2)) or (rf1 and wf2):
            return True
        if (set(w1) & set(w2)) or (set(w1) & set(r2)) or (set(r1) & set(w2)):
            return True
        if any(m == AddrMode.MEM for m, _ in w1 + r1) and any(
            m == AddrMode.MEM for m, _ in w2 + r2
        ):
            if any(m == AddrMode.MEM for m, _ in w1 + w2):
                return True
        return False

    def step(self):
        steps = self.check_interrupts()

        nxt = None

        if self.i1 is None:
            self.i1 = self.fetch(self.pc)
            steps += 1 + self.i1["len"]
            self.pc += self.i1["len"]
        else:
            self.sequential_tick += 1 + self.i1["len"]

        i1_str = self.format_instruction(self.i1)
        instr_debug = f"TICK: {self.tick:7} | PC: {self.pc:4} | {i1_str}"

        safe = [
            Opcode.ADD,
            Opcode.SUB,
            Opcode.MUL,
            Opcode.MOV,
            Opcode.ADC,
            Opcode.SBC,
            Opcode.CMP,
            Opcode.PUSH,
            Opcode.POP,
        ]
        if self.i1["op"] in safe:
            nxt = self.fetch(self.pc)
            steps += 1 + nxt["len"]
            if nxt["op"] in safe and not self.has_dep(self.i1, nxt):
                self.i2 = nxt

        if nxt is not None:
            self.pc += nxt["len"]

        if self.i2:
            i2_str = self.format_instruction(self.i2)
            if self.tick < 1500:
                logging.info(f"{instr_debug:<40} & {i2_str:<25} (SUPERSCALAR)")
            step1 = self.dispatch(self.i1)
            step2 = self.dispatch(self.i2)
            steps += max(step1, step2)

            self.sequential_tick += min(step1, step2)
        else:
            if self.tick < 1500:
                logging.info(instr_debug)
            steps += self.dispatch(self.i1)

        self.i1 = None

        if self.i2 is None and nxt is not None:
            self.i1 = nxt

        self.i2 = None

        self.tick += steps
        self.sequential_tick += steps


def main(target, schedule):
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    if len(sys.argv) < 2:
        sys.exit("Usage: python machine.py <bin> [sched]")

    with open(target, "rb") as f:
        dp = DataPath(from_bytes(f.read()))

    schd = []
    if len(sys.argv) == 3:
        with open(schedule, "r", encoding="utf-8") as f:
            raw_data = ast.literal_eval(f.read())
            schd = [{"tick": t, "char": c} for t, c in raw_data]

    cu = ControlUnit(dp, schd)

    try:
        while cu.tick < 10_000_000:
            cu.step()
    except StopIteration:
        logging.info("--- HALTED ---")

    logging.info(f"\nTicks (Opt/Seq): {cu.tick} / {cu.sequential_tick}")
    logging.info(f"Output: {''.join(dp.output_buf)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
