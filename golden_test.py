import contextlib
import io
import logging
import os
import tempfile
import pytest

import machine
import translator_lisp


@pytest.mark.golden_test("golden/*.yml")
def test_translator_and_machine(golden, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.handler.setFormatter(logging.Formatter("%(message)s"))

    with tempfile.TemporaryDirectory() as tmpdirname:
        source = os.path.join(tmpdirname, "source.lisp")
        schedule = os.path.join(tmpdirname, "schedule.txt")
        target = os.path.join(tmpdirname, "target.bin")

        with open(source, "w", encoding="utf-8") as file:
            file.write(golden["in_source"])

        with open(schedule, "w", encoding="utf-8") as file:
            file.write(golden.get("in_schedule", ""))

        with contextlib.redirect_stdout(io.StringIO()):
            translator_lisp.main(source, target)
            print("============================================================")

            machine.main(target, schedule)

        with open(target + ".log", encoding="utf-8") as file:
            code_log = file.read()

        assert code_log == golden.out["out_code_log"]
        assert caplog.text.strip() == golden.out["out_log"]
