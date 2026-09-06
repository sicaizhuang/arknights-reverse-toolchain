#!/usr/bin/env python3
"""Bounded AArch64 function/call evidence from current ELF symbols only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import struct
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def load_elf_parser(path: Path):
    spec = importlib.util.spec_from_file_location("phase1_native_parser", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def init_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,size INTEGER,sha256 TEXT,index_sha256 TEXT,status TEXT,machine INTEGER);
        CREATE TABLE functions(
          id INTEGER PRIMARY KEY,file_id INTEGER,name TEXT,aliases TEXT,address INTEGER,size INTEGER,
          section TEXT,file_offset INTEGER,boundary_source TEXT,status TEXT,instruction_count INTEGER,
          direct_call_count INTEGER,indirect_call_count INTEGER,return_count INTEGER,pseudocode_path TEXT,
          pseudocode_sha256 TEXT,failure_reason TEXT
        );
        CREATE TABLE calls(
          id INTEGER PRIMARY KEY,file_id INTEGER,caller_function_id INTEGER,call_address INTEGER,
          instruction_word INTEGER,call_kind TEXT,target_address INTEGER,target_name TEXT,
          target_source TEXT,evidence TEXT
        );
        CREATE INDEX calls_target ON calls(target_name);
        CREATE VIRTUAL TABLE search USING fts5(kind,name,content,source_path UNINDEXED,status UNINDEXED);
        """
    )
    return connection


def standard_aarch64_plt(elf, relocations: list[dict]) -> dict[int, str]:
    plt = next((section for section in elf.sections if section["name"] == ".plt"), None)
    if not plt:
        return {}
    entries = [row for row in relocations if row["section"] in (".rela.plt", ".rel.plt") and row["symbol"]]
    mapping = {}
    # AArch64 System V .plt uses a 32-byte resolver followed by 16-byte slots.
    for index, row in enumerate(entries):
        address = int(plt["addr"]) + 32 + index * 16
        if address < int(plt["addr"]) + int(plt["size"]):
            mapping[address] = row["symbol"]
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-index", required=True, type=Path)
    parser.add_argument("--elf-parser", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-functions-per-elf", type=int, default=96)
    parser.add_argument("--max-bytes-per-function", type=int, default=4096)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=False)
    pseudo_root = args.output / "pseudocode"
    pseudo_root.mkdir()
    parser_module = load_elf_parser(args.elf_parser)
    source_db = sqlite3.connect(args.native_index)
    indexed_files = source_db.execute("SELECT path,size,sha256,machine,status FROM files WHERE status='indexed' ORDER BY path").fetchall()
    source_db.close()
    output_db_path = args.output / "native_function_index.sqlite"
    db = init_db(output_db_path)

    files_report = []
    total_functions = total_calls = total_indirect = total_returns = 0
    excluded = Counter()
    for file_number, (path_text, indexed_size, indexed_hash, indexed_machine, indexed_status) in enumerate(indexed_files, 1):
        path = Path(path_text)
        current_hash = sha256(path)
        if current_hash != indexed_hash:
            db.execute("INSERT INTO files(path,size,sha256,index_sha256,status,machine) VALUES(?,?,?,?,?,?)", (str(path), path.stat().st_size, current_hash, indexed_hash, "blocked_hash_mismatch", indexed_machine))
            files_report.append({"path": str(path), "status": "blocked_hash_mismatch", "sha256": current_hash, "index_sha256": indexed_hash})
            continue
        elf = parser_module.Elf64(path)
        file_cursor = db.execute("INSERT INTO files(path,size,sha256,index_sha256,status,machine) VALUES(?,?,?,?,?,?)", (str(path), path.stat().st_size, current_hash, indexed_hash, "analyzed", elf.e_machine))
        file_id = file_cursor.lastrowid
        if elf.e_machine != 183:
            files_report.append({"path": str(path), "status": "not_aarch64", "sha256": current_hash})
            continue
        symbols, by_section = elf.symbols()
        relocations = elf.relocations(by_section)
        plt_imports = standard_aarch64_plt(elf, relocations)
        sections = {section["index"]: section for section in elf.sections}
        all_functions = [
            symbol for symbol in symbols
            if symbol["symbol_type"] == 2 and symbol["section_index"] in sections and symbol["value"] > 0
        ]
        by_range = defaultdict(list)
        for symbol in all_functions:
            by_range[(int(symbol["value"]), int(symbol["size"]), int(symbol["section_index"]))].append(symbol)
        canonical = []
        address_names = defaultdict(list)
        for symbol in all_functions:
            address_names[int(symbol["value"])].append(symbol["name"])
        for (address, size, section_index), aliases in by_range.items():
            if size <= 0:
                excluded["zero_size_symbol_boundary"] += 1
                continue
            section = sections[section_index]
            if not int(section["flags"]) & 0x4:
                excluded["non_executable_function_symbol"] += 1
                continue
            aliases = sorted(aliases, key=lambda item: (-int(item["bind"]), item["name"]))
            canonical.append((aliases[0], [item["name"] for item in aliases if item["name"]]))
        canonical.sort(key=lambda pair: (-int(pair[0]["bind"]), pair[0]["name"].startswith("$"), int(pair[0]["value"])))
        selected = canonical[: max(0, args.max_functions_per_elf)]
        excluded["bounded_function_limit"] += max(0, len(canonical) - len(selected))
        file_function_count = file_call_count = file_indirect_count = file_return_count = 0

        for symbol, aliases in selected:
            section = sections[int(symbol["section_index"])]
            address = int(symbol["value"])
            declared_size = int(symbol["size"])
            scan_size = min(declared_size, args.max_bytes_per_function)
            section_relative = address - int(section["addr"])
            file_offset = int(section["offset"]) + section_relative
            failure = None
            if section_relative < 0 or file_offset < 0 or file_offset + scan_size > len(elf.data):
                raw = b""
                failure = "symbol range is outside current ELF section bytes"
            else:
                raw = elf.data[file_offset:file_offset + scan_size]
            calls = []
            returns = []
            instruction_count = len(raw) // 4
            for offset in range(0, len(raw) - 3, 4):
                word = struct.unpack_from("<I", raw, offset)[0]
                pc = address + offset
                if word & 0xFC000000 == 0x94000000:
                    target = pc + sign_extend(word & 0x03FFFFFF, 26) * 4
                    if target in plt_imports:
                        target_name = plt_imports[target]
                        target_source = "current ELF AArch64 PLT slot and relocation order"
                    elif target in address_names:
                        target_name = address_names[target][0]
                        target_source = "current ELF exact function symbol address"
                    else:
                        target_name = f"sub_{target:X}"
                        target_source = "current ELF BL immediate target without a matching symbol"
                    calls.append({"address": pc, "word": word, "kind": "direct_bl", "target": target, "name": target_name, "source": target_source})
                elif word & 0xFFFFFC1F == 0xD63F0000:
                    register = (word >> 5) & 0x1F
                    calls.append({"address": pc, "word": word, "kind": "indirect_blr", "target": None, "name": f"register_x{register}", "source": "current ELF BLR instruction; target unresolved"})
                elif word & 0xFFFFFC1F == 0xD65F0000:
                    returns.append({"address": pc, "word": word})

            safe_name = "".join(character if character.isalnum() or character in "._-" else "_" for character in symbol["name"])[:120] or f"sub_{address:X}"
            pseudo_path = pseudo_root / f"{file_number:02d}_{address:016X}_{safe_name}.txt"
            lines = [
                f"function {symbol['name']} @ static ELF VA/RVA 0x{address:X} size 0x{declared_size:X}",
                "boundary: current ELF STT_FUNC symbol value + st_size",
                "// Structural call/return lift only. This is not full decompiler pseudocode.",
                "{",
            ]
            for call in calls:
                if call["kind"] == "direct_bl":
                    lines.append(f"  call {call['name']}(); // BL at +0x{call['address'] - address:X}")
                else:
                    lines.append(f"  call_indirect {call['name']}(); // BLR at +0x{call['address'] - address:X}")
            if returns:
                lines.append(f"  return; // {len(returns)} RET instruction(s) observed")
            if failure:
                lines.append(f"  // failure: {failure}")
            if scan_size < declared_size:
                lines.append(f"  // bounded: scanned 0x{scan_size:X} of 0x{declared_size:X} bytes")
            lines.append("}")
            pseudo_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            function_status = "structural_lift_completed" if raw else "failed_range"
            function_cursor = db.execute(
                "INSERT INTO functions(file_id,name,aliases,address,size,section,file_offset,boundary_source,status,instruction_count,direct_call_count,indirect_call_count,return_count,pseudocode_path,pseudocode_sha256,failure_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, symbol["name"], json.dumps(aliases, ensure_ascii=False), address, declared_size, section["name"], file_offset,
                 "current ELF STT_FUNC symbol value plus st_size", function_status, instruction_count,
                 sum(call["kind"] == "direct_bl" for call in calls), sum(call["kind"] == "indirect_blr" for call in calls), len(returns),
                 str(pseudo_path), sha256(pseudo_path), failure),
            )
            function_id = function_cursor.lastrowid
            db.execute("INSERT INTO search VALUES(?,?,?,?,?)", ("native_function", symbol["name"], " ".join(aliases), str(path), function_status))
            for call in calls:
                db.execute(
                    "INSERT INTO calls(file_id,caller_function_id,call_address,instruction_word,call_kind,target_address,target_name,target_source,evidence) VALUES(?,?,?,?,?,?,?,?,?)",
                    (file_id, function_id, call["address"], call["word"], call["kind"], call["target"], call["name"], call["source"],
                     "decoded from current ELF AArch64 instruction bytes within symbol boundary"),
                )
            file_function_count += 1
            file_call_count += sum(call["kind"] == "direct_bl" for call in calls)
            file_indirect_count += sum(call["kind"] == "indirect_blr" for call in calls)
            file_return_count += len(returns)
        db.commit()
        total_functions += file_function_count
        total_calls += file_call_count
        total_indirect += file_indirect_count
        total_returns += file_return_count
        files_report.append({
            "path": str(path), "sha256": current_hash, "status": "analyzed",
            "eligible_symbol_function_count": len(canonical), "selected_function_count": file_function_count,
            "direct_call_count": file_call_count, "indirect_call_count": file_indirect_count,
            "return_instruction_count": file_return_count,
        })
        del elf

    quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
    db.close()
    functions_path = args.output / "native_functions.jsonl"
    calls_path = args.output / "native_calls.jsonl"
    output_db = sqlite3.connect(output_db_path)
    with functions_path.open("w", encoding="utf-8") as stream:
        for row in output_db.execute("SELECT f.path,n.name,n.aliases,n.address,n.size,n.section,n.boundary_source,n.status,n.direct_call_count,n.indirect_call_count,n.return_count,n.pseudocode_path,n.pseudocode_sha256,n.failure_reason FROM functions n JOIN files f ON f.id=n.file_id ORDER BY f.path,n.address"):
            stream.write(json.dumps(dict(zip(("elf","name","aliases","address","size","section","boundary_source","status","direct_calls","indirect_calls","returns","pseudocode","pseudocode_sha256","failure_reason"), row)), ensure_ascii=False) + "\n")
    with calls_path.open("w", encoding="utf-8") as stream:
        for row in output_db.execute("SELECT f.path,n.name,c.call_address,c.instruction_word,c.call_kind,c.target_address,c.target_name,c.target_source,c.evidence FROM calls c JOIN functions n ON n.id=c.caller_function_id JOIN files f ON f.id=c.file_id ORDER BY f.path,c.call_address"):
            stream.write(json.dumps(dict(zip(("elf","caller","call_address","instruction_word","kind","target_address","target_name","target_source","evidence"), row)), ensure_ascii=False) + "\n")
    output_db.close()

    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_with_limits",
        "phase": "2B",
        "source_native_index": str(args.native_index),
        "source_native_index_sha256": sha256(args.native_index),
        "file_count": len(files_report),
        "selected_function_count": total_functions,
        "direct_call_count": total_calls,
        "indirect_call_count": total_indirect,
        "return_instruction_count": total_returns,
        "excluded_counts": dict(excluded),
        "database": str(output_db_path),
        "database_quick_check": quick_check,
        "functions_jsonl": str(functions_path),
        "calls_jsonl": str(calls_path),
        "files": files_report,
        "trust": {
            "function_boundary": "current ELF STT_FUNC symbol plus st_size only",
            "direct_call": "current ELF AArch64 BL instruction within that boundary",
            "indirect_call": "BLR instruction observed; target deliberately unresolved",
            "address_kind": "static ELF virtual address / RVA, not a runtime address",
            "old_addresses_used": False,
            "full_decompilation_claimed": False,
        },
        "limitations": [
            "Only a bounded number of symbol-sized functions per ELF is lifted.",
            "The pseudocode files summarize call and return instructions and omit other semantics.",
            "Functions without a current symbol size are excluded rather than manually created.",
            "No runtime address is validated.",
        ],
    }
    report_path = args.output / "native_function_analysis.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
