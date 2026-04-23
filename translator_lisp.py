import sys
import re
from isa import Opcode, AddrMode, Registers, serialize_instruction, to_bytes
import disasm

from const import VECTOR_TRAP, OUT_INT, OUT_CHAR, TRAP_BUFFER


def tokenize(chars):
    chars = re.sub(r";.*", "", chars)
    return re.findall(r'"[^"]*"|\(|\)|[^\s()]+', chars)


def parse(tokens):
    if not tokens:
        raise SyntaxError("Unexpected EOF")
    token = tokens.pop(0)
    if token == "(":
        L = []
        while tokens and tokens[0] != ")":
            L.append(parse(tokens))
        if not tokens:
            raise SyntaxError("Missing )")
        tokens.pop(0)
        return L
    elif token == ")":
        raise SyntaxError("Unexpected )")
    else:
        try:
            return int(token)
        except ValueError:
            if token.startswith('"') and token.endswith('"'):
                return token[1:-1]
            return token


class Compiler:
    def __init__(self):
        self.code = []
        self.data = []
        self.data_base = 7
        self.variables = {}
        self.functions = {}
        self.string_constants = {}
        self.loop_results = {}

    def alloc_data(self, name, value_words):
        addr = self.data_base + len(self.data)
        if name:
            self.variables[name] = addr
        self.data.extend([w & 0xFFFFFFFF for w in value_words])
        return addr

    def preallocate_resources(self, node):
        if isinstance(node, list):
            if not node:
                return
            op = node[0]

            # 1. Ищем строки в print-pstr
            if op == "print-pstr" and len(node) > 1:
                string_content = node[1]
                if (
                    isinstance(string_content, str)
                    and string_content not in self.string_constants
                ):
                    raw_chars = string_content.replace("\\n", "\n")
                    pstr_data = [len(raw_chars)] + [ord(c) for c in raw_chars]
                    addr = self.alloc_data(None, pstr_data)
                    self.string_constants[string_content] = addr

            elif op == "loop":
                var_name = node[1]
                if var_name not in self.variables:
                    self.alloc_data(var_name, [0])

                res_addr = self.alloc_data(None, [0])
                self.loop_results[id(node)] = res_addr

            for item in node:
                self.preallocate_resources(item)

    def emit(self, opcode, modes, args):
        ins = serialize_instruction(opcode, modes, args)
        addr = len(self.code)
        self.code.extend(ins)
        return addr

    def read_var(self, name, dest_reg, local_scope):
        if local_scope is not None and name in local_scope:
            offset = local_scope[name]
            self.emit(
                Opcode.MOV, [AddrMode.REG, AddrMode.REG], [dest_reg, Registers.SP]
            )
            self.emit(
                Opcode.ADD,
                [AddrMode.REG, AddrMode.REG, AddrMode.IMM],
                [dest_reg, dest_reg, offset],
            )
            self.emit(
                Opcode.MOV, [AddrMode.REG, AddrMode.REG_INDIRECT], [dest_reg, dest_reg]
            )
        else:
            self.emit(
                Opcode.MOV,
                [AddrMode.REG, AddrMode.MEM],
                [dest_reg, self.variables[name]],
            )

    def write_var(self, name, source_reg, local_scope):
        if local_scope is not None and name in local_scope:
            offset = local_scope[name]
            self.emit(
                Opcode.MOV, [AddrMode.REG, AddrMode.REG], [Registers.R1, Registers.SP]
            )
            self.emit(
                Opcode.ADD,
                [AddrMode.REG, AddrMode.REG, AddrMode.IMM],
                [Registers.R1, Registers.R1, offset],
            )
            self.emit(
                Opcode.MOV,
                [AddrMode.REG_INDIRECT, AddrMode.REG],
                [Registers.R1, source_reg],
            )
        else:
            self.emit(
                Opcode.MOV,
                [AddrMode.MEM, AddrMode.REG],
                [self.variables[name], source_reg],
            )

    def compile_expr(self, expr, dest_reg=Registers.R0, local_scope=None):
        if local_scope is None:
            local_scope = {}
        if isinstance(expr, int):
            self.emit(
                Opcode.MOV, [AddrMode.REG, AddrMode.IMM], [dest_reg, expr & 0xFFFFFFFF]
            )
        elif isinstance(expr, str):
            if expr in self.variables or expr in local_scope:
                self.read_var(expr, dest_reg, local_scope)
        elif isinstance(expr, list):
            op = expr[0]

            if op == "print-pstr":
                addr = self.string_constants[expr[1]]
                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.IMM], [Registers.R1, addr + 1]
                )
                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.MEM], [Registers.R2, addr]
                )
                loop_start = len(self.code)
                self.emit(Opcode.CMP, [AddrMode.REG, AddrMode.IMM], [Registers.R2, 0])
                jz = len(self.code) + 1
                self.emit(Opcode.JEQ, [AddrMode.IMM], [0])
                self.emit(
                    Opcode.MOV,
                    [AddrMode.REG, AddrMode.REG_INDIRECT],
                    [Registers.R0, Registers.R1],
                )
                self.emit(
                    Opcode.MOV, [AddrMode.MEM, AddrMode.REG], [OUT_CHAR, Registers.R0]
                )
                self.emit(
                    Opcode.ADD,
                    [AddrMode.REG, AddrMode.REG, AddrMode.IMM],
                    [Registers.R1, Registers.R1, 1],
                )
                self.emit(
                    Opcode.SUB,
                    [AddrMode.REG, AddrMode.REG, AddrMode.IMM],
                    [Registers.R2, Registers.R2, 1],
                )
                self.emit(Opcode.JMP, [AddrMode.IMM], [loop_start])
                self.code[jz] = len(self.code)

            elif op == "loop":
                var_name = expr[1]
                temp_var = self.variables[var_name]

                temp_res = self.loop_results[id(expr)]

                self.compile_expr(expr[2], Registers.R0, local_scope)
                self.emit(
                    Opcode.MOV, [AddrMode.MEM, AddrMode.REG], [temp_var, Registers.R0]
                )
                start = len(self.code)
                self.compile_expr(expr[3], Registers.R1, local_scope)
                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.MEM], [Registers.R0, temp_var]
                )
                self.emit(
                    Opcode.CMP,
                    [AddrMode.REG, AddrMode.REG],
                    [Registers.R0, Registers.R1],
                )
                je = len(self.code) + 1
                self.emit(Opcode.JGT, [AddrMode.IMM], [0])
                for b in expr[4:-1]:
                    self.compile_statement(b, local_scope)
                if len(expr) > 4:
                    self.compile_expr(expr[-1], Registers.R0, local_scope)
                    self.emit(
                        Opcode.MOV,
                        [AddrMode.MEM, AddrMode.REG],
                        [temp_res, Registers.R0],
                    )
                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.MEM], [Registers.R0, temp_var]
                )
                self.emit(
                    Opcode.ADD,
                    [AddrMode.REG, AddrMode.REG, AddrMode.IMM],
                    [Registers.R0, Registers.R0, 1],
                )
                self.emit(
                    Opcode.MOV, [AddrMode.MEM, AddrMode.REG], [temp_var, Registers.R0]
                )
                self.emit(Opcode.JMP, [AddrMode.IMM], [start])
                self.code[je] = len(self.code)
                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.MEM], [dest_reg, temp_res]
                )

            elif op in ["+", "-", "*", "mod", "adc", "sbc"]:
                op_map = {
                    "+": Opcode.ADD,
                    "-": Opcode.SUB,
                    "*": Opcode.MUL,
                    "mod": Opcode.MOD,
                    "adc": Opcode.ADC,
                    "sbc": Opcode.SBC,
                }

                self.compile_expr(expr[1], Registers.R1, local_scope)
                self.emit(Opcode.PUSH, [AddrMode.REG], [Registers.R1])
                self.compile_expr(expr[2], Registers.R2, local_scope)
                self.emit(Opcode.POP, [AddrMode.REG], [Registers.R1])

                self.emit(
                    op_map[op],
                    [AddrMode.REG, AddrMode.REG, AddrMode.REG],
                    [dest_reg, Registers.R1, Registers.R2],
                )

            elif op == "in-char":
                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.MEM], [dest_reg, TRAP_BUFFER]
                )

            elif op == "print-char":
                self.compile_expr(expr[1], Registers.R0, local_scope)
                self.emit(
                    Opcode.MOV, [AddrMode.MEM, AddrMode.REG], [OUT_CHAR, Registers.R0]
                )

            elif op == "setq":
                self.compile_expr(expr[2], Registers.R0, local_scope)
                self.write_var(expr[1], Registers.R0, local_scope)

            elif op == "if":
                self.compile_expr(expr[1], Registers.R0, local_scope)
                self.emit(Opcode.CMP, [AddrMode.REG, AddrMode.IMM], [Registers.R0, 0])
                jz = len(self.code) + 1
                self.emit(Opcode.JEQ, [AddrMode.IMM], [0])
                self.compile_expr(expr[2], dest_reg, local_scope)
                jmp = len(self.code) + 1
                self.emit(Opcode.JMP, [AddrMode.IMM], [0])
                self.code[jz] = len(self.code)
                if len(expr) > 3:
                    self.compile_expr(expr[3], dest_reg, local_scope)
                self.code[jmp] = len(self.code)

            elif op == "print":
                self.compile_expr(expr[1], Registers.R0, local_scope)
                self.emit(
                    Opcode.MOV, [AddrMode.MEM, AddrMode.REG], [OUT_INT, Registers.R0]
                )

            elif op == "progn":
                for b in expr[1:]:
                    self.compile_statement(b, local_scope)

            elif op in ["=", ">", "<"]:
                self.compile_expr(expr[1], Registers.R1, local_scope)
                self.compile_expr(expr[2], Registers.R2, local_scope)
                self.emit(
                    Opcode.CMP,
                    [AddrMode.REG, AddrMode.REG],
                    [Registers.R1, Registers.R2],
                )
                self.emit(Opcode.MOV, [AddrMode.REG, AddrMode.IMM], [dest_reg, 1])
                skip = len(self.code) + 1
                if op == "=":
                    self.emit(Opcode.JEQ, [AddrMode.IMM], [0])
                elif op == ">":
                    self.emit(Opcode.JGT, [AddrMode.IMM], [0])
                elif op == "<":
                    self.emit(
                        Opcode.CMP,
                        [AddrMode.REG, AddrMode.REG],
                        [Registers.R2, Registers.R1],
                    )
                    self.emit(Opcode.JGT, [AddrMode.IMM], [0])
                self.emit(Opcode.MOV, [AddrMode.REG, AddrMode.IMM], [dest_reg, 0])
                self.code[skip] = len(self.code)

            elif op == "read-mem":
                self.compile_expr(expr[1], Registers.R1, local_scope)
                self.emit(
                    Opcode.MOV,
                    [AddrMode.REG, AddrMode.REG_INDIRECT],
                    [dest_reg, Registers.R1],
                )

            elif op == "write-mem":
                self.compile_expr(expr[1], Registers.R1, local_scope)
                self.compile_expr(expr[2], Registers.R0, local_scope)
                self.emit(
                    Opcode.MOV,
                    [AddrMode.REG_INDIRECT, AddrMode.REG],
                    [Registers.R1, Registers.R0],
                )
            elif op in self.functions:
                argc = len(expr[1:])

                self.emit(Opcode.PUSH, [AddrMode.REG], [Registers.R1])
                self.emit(Opcode.PUSH, [AddrMode.REG], [Registers.R2])

                for arg in expr[1:]:
                    self.compile_expr(arg, Registers.R0, local_scope)
                    self.emit(Opcode.PUSH, [AddrMode.REG], [Registers.R0])

                self.emit(Opcode.CALL, [AddrMode.IMM], [self.functions[op]])

                self.emit(
                    Opcode.MOV,
                    [AddrMode.REG, AddrMode.REG],
                    [Registers.R3, Registers.R0],
                )

                for _ in range(argc):
                    self.emit(Opcode.POP, [AddrMode.REG], [Registers.R0])

                self.emit(Opcode.POP, [AddrMode.REG], [Registers.R2])
                self.emit(Opcode.POP, [AddrMode.REG], [Registers.R1])

                self.emit(
                    Opcode.MOV, [AddrMode.REG, AddrMode.REG], [dest_reg, Registers.R3]
                )

            else:
                raise SyntaxError(f"Unknown function or operator: {op}")

    def compile_statement(self, stmt, local_scope=None):
        if not isinstance(stmt, list):
            return
        op = stmt[0]
        if op == "defvar":
            if isinstance(stmt[2], list):
                self.compile_expr(stmt[2], Registers.R0, local_scope)
                self.write_var(stmt[1], Registers.R0, local_scope)
        elif op == "defun":
            skip = len(self.code) + 1
            self.emit(Opcode.JMP, [AddrMode.IMM], [0])
            self.functions[stmt[1]] = len(self.code)
            scope = {name: i + 1 for i, name in enumerate(stmt[2])}
            for s in stmt[3:]:
                self.compile_statement(s, scope)
            self.emit(Opcode.RET, [], [])
            self.code[skip] = len(self.code)
        else:
            self.compile_expr(stmt, Registers.R0, local_scope)

    def collect_defvars(self, node):
        if isinstance(node, list):
            if not node:
                return

            if node[0] == "defvar":
                name = node[1]
                if name not in self.variables:
                    init_val = node[2] if isinstance(node[2], int) else 0
                    self.alloc_data(name, [init_val])

            for item in node:
                self.collect_defvars(item)

    def compile(self, ast):
        self.variables, self.functions, self.data, self.string_constants = (
            {},
            {},
            [],
            {},
        )
        for s in ast:
            self.collect_defvars(s)

        for s in ast:
            if isinstance(s, list) and s[0] == "defun":
                self.functions[s[1]] = 0

        self.preallocate_resources(ast)

        self.code = [0] * (self.data_base + len(self.data))

        irq_ast = [s for s in ast if isinstance(s, list) and s[0] == "def-interrupt"]
        irq_addr = len(self.code)
        if irq_ast:
            for b in irq_ast[0][1:]:
                self.compile_statement(b)
        self.emit(Opcode.IRET, [], [])

        main_addr = len(self.code)
        for s in ast:
            if not (isinstance(s, list) and s[0] == "def-interrupt"):
                self.compile_statement(s)
        self.emit(Opcode.HALT, [], [])

        v_main = serialize_instruction(Opcode.JMP, [AddrMode.IMM], [main_addr])
        for i, val in enumerate(v_main):
            self.code[i] = val
        v_irq = serialize_instruction(Opcode.JMP, [AddrMode.IMM], [irq_addr])
        for i, val in enumerate(v_irq):
            self.code[VECTOR_TRAP + i] = val
        for i, val in enumerate(self.data):
            self.code[self.data_base + i] = val
        return self.code


def main(source_file, target_file):
    with open(source_file, "r", encoding="utf-8") as f:
        tokens = tokenize(f.read())

    ast = []
    while tokens:
        ast.append(parse(tokens))

    binary_data = to_bytes(Compiler().compile(ast))

    with open(target_file, "wb") as f:
        f.write(binary_data)

    listing = disasm.disassemble(binary_data)

    with open(target_file + ".log", "w", encoding="utf-8") as f:
        f.write(listing)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(1)

    source_file = sys.argv[1]
    target_file = sys.argv[2]
    main(source_file, target_file)
