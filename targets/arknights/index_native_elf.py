#!/usr/bin/env python3
"""Bounded static structure index for current ARM64 ELF inputs; no disassembly or runtime use."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_RELA = 4
SHT_DYNAMIC = 6
SHT_NOBITS = 8
SHT_REL = 9
SHT_DYNSYM = 11
SHN_UNDEF = 0
DT_NULL = 0
DT_NEEDED = 1
SHF_EXECINSTR = 0x4
SHF_ALLOC = 0x2


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def cstring(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


def scan_ascii(data: bytes, minimum: int = 4):
    start = None
    for index, byte in enumerate(data):
        printable = 32 <= byte <= 126 or byte in (9,)
        if printable and start is None:
            start = index
        elif not printable and start is not None:
            if index - start >= minimum:
                yield start, data[start:index].decode("ascii", errors="replace")[:4096]
            start = None
    if start is not None and len(data) - start >= minimum:
        yield start, data[start:].decode("ascii", errors="replace")[:4096]


class Elf64:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if len(self.data) < 64 or self.data[:4] != b"\x7fELF":
            raise ValueError("not ELF")
        if self.data[4] != 2 or self.data[5] != 1:
            raise ValueError("only ELF64 little-endian is supported")
        values = struct.unpack_from("<HHIQQQIHHHHHH", self.data, 16)
        (self.e_type, self.e_machine, self.e_version, self.e_entry, self.e_phoff, self.e_shoff,
         self.e_flags, self.e_ehsize, self.e_phentsize, self.e_phnum, self.e_shentsize,
         self.e_shnum, self.e_shstrndx) = values
        self.sections = self._sections()

    def _sections(self):
        sections = []
        for index in range(self.e_shnum):
            offset = self.e_shoff + index * self.e_shentsize
            if offset + 64 > len(self.data):
                break
            fields = struct.unpack_from("<IIQQQQIIQQ", self.data, offset)
            sections.append({
                "index": index, "name_offset": fields[0], "type": fields[1], "flags": fields[2],
                "addr": fields[3], "offset": fields[4], "size": fields[5], "link": fields[6],
                "info": fields[7], "align": fields[8], "entry_size": fields[9],
            })
        shstr = b""
        if 0 <= self.e_shstrndx < len(sections):
            sec = sections[self.e_shstrndx]
            shstr = self.slice(sec)
        for section in sections:
            section["name"] = cstring(shstr, section["name_offset"])
        return sections

    def slice(self, section: dict) -> bytes:
        if section["type"] == SHT_NOBITS:
            return b""
        start, size = section["offset"], section["size"]
        if start > len(self.data) or start + size > len(self.data):
            return b""
        return self.data[start:start + size]

    def symbols(self):
        by_section = {}
        flat = []
        for section in self.sections:
            if section["type"] not in (SHT_SYMTAB, SHT_DYNSYM) or not section["entry_size"]:
                continue
            strings = self.slice(self.sections[section["link"]]) if section["link"] < len(self.sections) else b""
            values = []
            raw = self.slice(section)
            for index in range(len(raw) // section["entry_size"]):
                base = index * section["entry_size"]
                if base + 24 > len(raw):
                    break
                name_offset, info, other, shndx, value, size = struct.unpack_from("<IBBHQQ", raw, base)
                record = {
                    "index": index, "table_section": section["index"], "table": section["name"],
                    "name": cstring(strings, name_offset), "bind": info >> 4, "symbol_type": info & 0xF,
                    "visibility": other & 0x3, "section_index": shndx, "value": value, "size": size,
                }
                values.append(record)
                flat.append(record)
            by_section[section["index"]] = values
        return flat, by_section

    def relocations(self, symbols_by_section):
        rows = []
        for section in self.sections:
            if section["type"] not in (SHT_RELA, SHT_REL) or not section["entry_size"]:
                continue
            raw = self.slice(section)
            symbols = symbols_by_section.get(section["link"], [])
            for index in range(len(raw) // section["entry_size"]):
                base = index * section["entry_size"]
                if section["type"] == SHT_RELA and base + 24 <= len(raw):
                    offset, info, addend = struct.unpack_from("<QQq", raw, base)
                elif section["type"] == SHT_REL and base + 16 <= len(raw):
                    offset, info = struct.unpack_from("<QQ", raw, base)
                    addend = None
                else:
                    break
                symbol_index, relocation_type = info >> 32, info & 0xFFFFFFFF
                symbol_name = symbols[symbol_index]["name"] if symbol_index < len(symbols) else ""
                rows.append({"section": section["name"], "offset": offset, "type": relocation_type, "symbol_index": symbol_index, "symbol": symbol_name, "addend": addend})
        return rows

    def needed_libraries(self):
        result = []
        for section in self.sections:
            if section["type"] != SHT_DYNAMIC or not section["entry_size"]:
                continue
            strings = self.slice(self.sections[section["link"]]) if section["link"] < len(self.sections) else b""
            raw = self.slice(section)
            for index in range(len(raw) // section["entry_size"]):
                tag, value = struct.unpack_from("<QQ", raw, index * section["entry_size"])
                if tag == DT_NULL:
                    break
                if tag == DT_NEEDED:
                    result.append(cstring(strings, value))
        return result


def init_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(
        """
        CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,size INTEGER,sha256 TEXT,machine INTEGER,entry_point INTEGER,status TEXT,failure_reason TEXT);
        CREATE TABLE sections(file_id INTEGER,name TEXT,section_index INTEGER,type INTEGER,flags INTEGER,address INTEGER,file_offset INTEGER,size INTEGER,alignment INTEGER);
        CREATE TABLE symbols(file_id INTEGER,name TEXT,table_name TEXT,bind INTEGER,symbol_type INTEGER,visibility INTEGER,section_index INTEGER,value INTEGER,size INTEGER,evidence TEXT);
        CREATE INDEX idx_symbols_name ON symbols(name); CREATE INDEX idx_symbols_value ON symbols(value);
        CREATE TABLE imports(file_id INTEGER,name TEXT,bind INTEGER,symbol_type INTEGER,table_name TEXT); CREATE INDEX idx_imports_name ON imports(name);
        CREATE TABLE exports(file_id INTEGER,name TEXT,bind INTEGER,symbol_type INTEGER,section_index INTEGER,value INTEGER,size INTEGER,evidence TEXT); CREATE INDEX idx_exports_name ON exports(name);
        CREATE TABLE relocations(file_id INTEGER,relocation_section TEXT,offset INTEGER,relocation_type INTEGER,symbol_name TEXT,addend INTEGER,evidence TEXT);
        CREATE INDEX idx_relocations_symbol ON relocations(symbol_name);
        CREATE TABLE needed_libraries(file_id INTEGER,name TEXT);
        CREATE TABLE strings(id INTEGER PRIMARY KEY,file_id INTEGER,section TEXT,file_offset INTEGER,virtual_address INTEGER,value TEXT,evidence TEXT);
        CREATE INDEX idx_native_strings_value ON strings(value);
        CREATE VIRTUAL TABLE string_search USING fts5(value, section UNINDEXED, source_path UNINDEXED);
        """
    )
    return db


def index_elf(db: sqlite3.Connection, path: Path) -> dict:
    file_hash = sha256(path)
    try:
        elf = Elf64(path)
    except Exception as exc:
        db.execute("INSERT INTO files(path,size,sha256,status,failure_reason) VALUES(?,?,?,?,?)", (str(path), path.stat().st_size, file_hash, "failed", str(exc)))
        return {"path": str(path), "sha256": file_hash, "status": "failed", "reason": str(exc)}
    cursor = db.execute(
        "INSERT INTO files(path,size,sha256,machine,entry_point,status) VALUES(?,?,?,?,?,?)",
        (str(path.resolve()), path.stat().st_size, file_hash, elf.e_machine, elf.e_entry, "indexed"),
    )
    file_id = cursor.lastrowid
    for section in elf.sections:
        db.execute(
            "INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?)",
            (file_id, section["name"], section["index"], section["type"], section["flags"], section["addr"], section["offset"], section["size"], section["align"]),
        )
    symbols, by_section = elf.symbols()
    import_count = export_count = function_symbol_count = 0
    for symbol in symbols:
        if not symbol["name"]:
            continue
        evidence = "current ELF symbol table"
        db.execute(
            "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?)",
            (file_id, symbol["name"], symbol["table"], symbol["bind"], symbol["symbol_type"], symbol["visibility"], symbol["section_index"], symbol["value"], symbol["size"], evidence),
        )
        if symbol["section_index"] == SHN_UNDEF:
            import_count += 1
            db.execute("INSERT INTO imports VALUES(?,?,?,?,?)", (file_id, symbol["name"], symbol["bind"], symbol["symbol_type"], symbol["table"]))
        elif symbol["bind"] in (1, 2):
            export_count += 1
            if symbol["symbol_type"] == 2:
                function_symbol_count += 1
            db.execute(
                "INSERT INTO exports VALUES(?,?,?,?,?,?,?,?)",
                (file_id, symbol["name"], symbol["bind"], symbol["symbol_type"], symbol["section_index"], symbol["value"], symbol["size"], "defined current ELF global/weak symbol"),
            )
    relocations = elf.relocations(by_section)
    for row in relocations:
        db.execute(
            "INSERT INTO relocations VALUES(?,?,?,?,?,?,?)",
            (file_id, row["section"], row["offset"], row["type"], row["symbol"], row["addend"], "current ELF relocation entry"),
        )
    needed = elf.needed_libraries()
    for name in needed:
        db.execute("INSERT INTO needed_libraries VALUES(?,?)", (file_id, name))
    string_count = 0
    for section in elf.sections:
        if not section["flags"] & SHF_ALLOC or section["flags"] & SHF_EXECINSTR or section["type"] == SHT_NOBITS:
            continue
        raw = elf.slice(section)
        for relative_offset, value in scan_ascii(raw):
            file_offset = section["offset"] + relative_offset
            address = section["addr"] + relative_offset
            db.execute(
                "INSERT INTO strings(file_id,section,file_offset,virtual_address,value,evidence) VALUES(?,?,?,?,?,?)",
                (file_id, section["name"], file_offset, address, value, "current ELF allocated non-executable section"),
            )
            db.execute("INSERT INTO string_search(value,section,source_path) VALUES(?,?,?)", (value, section["name"], str(path.resolve())))
            string_count += 1
    db.commit()
    return {
        "path": str(path.resolve()), "size": path.stat().st_size, "sha256": file_hash, "status": "indexed",
        "machine": elf.e_machine, "entry_point": hex(elf.e_entry), "section_count": len(elf.sections),
        "symbol_count": len([s for s in symbols if s["name"]]), "import_count": import_count,
        "export_count": export_count, "defined_function_symbol_count": function_symbol_count,
        "relocation_count": len(relocations), "needed_library_count": len(needed), "string_count": string_count,
    }


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-elf", type=Path, required=True)
    parser.add_argument("--elf-root", type=Path, required=True)
    parser.add_argument("--provenance-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    paths = [args.primary_elf.resolve()]
    for path in sorted(args.elf_root.rglob("*.so"), key=lambda p: str(p).lower()):
        if path.name.lower() in {args.primary_elf.name.lower(), "libil2cpp.so"}:
            continue
        paths.append(path.resolve())
    db_path = output / "native_index.sqlite"
    db = init_db(db_path)
    results = [index_elf(db, path) for path in paths]
    statuses = Counter(row["status"] for row in results)
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_with_limits" if statuses.get("failed", 0) == 0 else "partial",
        "scope": "Current APK ARM64 ELF static structures only; no Ghidra, disassembly, old address import, or runtime validation.",
        "command": {
            "executable": str(Path(sys.executable).resolve()),
            "arguments": [str(Path(__file__).resolve()), *sys.argv[1:]],
            "exit_code": 0,
        },
        "provenance": {
            "report": str(args.provenance_report.resolve()),
            "report_sha256": sha256(args.provenance_report),
            "primary_current_elf": str(args.primary_elf.resolve()),
            "decoded_native_root": str(args.elf_root.resolve()),
        },
        "coverage": {
            "input_count": len(paths), "status_counts": dict(statuses),
            "section_count": db.execute("SELECT COUNT(*) FROM sections").fetchone()[0],
            "symbol_count": db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "import_count": db.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
            "export_count": db.execute("SELECT COUNT(*) FROM exports").fetchone()[0],
            "current_defined_function_symbol_count": db.execute("SELECT COUNT(*) FROM exports WHERE symbol_type=2").fetchone()[0],
            "relocation_count": db.execute("SELECT COUNT(*) FROM relocations").fetchone()[0],
            "string_count": db.execute("SELECT COUNT(*) FROM strings").fetchone()[0],
        },
        "files": results,
        "current_function_policy": {
            "eligible_current_candidates": "Only defined STT_FUNC symbols present in the current ELF index.",
            "manual_function_boundaries_created": False,
            "legacy_addresses_used": False,
            "il2cpp_method_focus_created": False,
            "reason": "IL2CPP provenance did not produce a trusted current method mapping.",
        },
        "outputs": {"sqlite": str(db_path)},
        "limitations": [
            "Strings, relocations, and imports are structural evidence, not proof of a high-level call chain.",
            "Stripped functions without current symbols are not assigned invented boundaries or names.",
            "No runtime address was verified.",
        ],
    }
    write_json(output / "native_analysis.json", report)
    db.close()
    return 0 if report["status"] == "completed_with_limits" else 3


if __name__ == "__main__":
    raise SystemExit(main())
