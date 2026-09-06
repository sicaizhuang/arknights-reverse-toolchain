#@category Arknights
#@runtime PyGhidra
"""Import reconstructed IL2CPP labels into a Ghidra program."""

import csv
import re

from ghidra.program.model.symbol import SourceType


def safe(value):
    return re.sub(r"[^A-Za-z0-9_.$<>]", "_", value)[:220]


def import_csv(path, prefix):
    if not path:
        return
    with open(path, "r", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            try:
                address = toAddr(int(row[0], 0))
                currentProgram.getSymbolTable().createLabel(
                    address, prefix + safe(row[1]), SourceType.IMPORTED
                )
            except Exception:
                pass
            if monitor.isCancelled():
                break


args = getScriptArgs()
if len(args) > 0:
    import_csv(args[0], "il2cpp_method_")
if len(args) > 1:
    import_csv(args[1], "il2cpp_type_")
if len(args) > 2:
    import_csv(args[2], "il2cpp_string_")
