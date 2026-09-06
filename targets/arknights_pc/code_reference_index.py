from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
MODIFIERS = {
    "public", "private", "protected", "internal", "static", "virtual",
    "override", "abstract", "sealed", "extern", "new", "unsafe", "async",
    "readonly", "partial",
}

TYPE_RE = re.compile(
    r"^(?P<prefix>.*?)\b(?P<kind>class|struct|interface|enum)\s+"
    r"(?P<name>[^\s:{]+)(?:\s*:\s*(?P<bases>.*?))?"
    r"(?:\s*//\s*TypeDefIndex:\s*(?P<index>\d+))?$"
)
LEGACY_NAMESPACE_RE = re.compile(r"^namespace\s+([^;]+);$")
CURRENT_NAMESPACE_RE = re.compile(r"^// Namespace:\s*(.*)$")
TOKEN_RE = re.compile(r'\[Token\(Token\s*=\s*"(0x[0-9A-Fa-f]+)"\)\]')
FIELD_OFFSET_RE = re.compile(r'(?:FieldOffset\(Offset\s*=\s*"(0x[0-9A-Fa-f]+)"\)|Field offset:\s*(0x[0-9A-Fa-f]+))')
ADDRESS_RE = re.compile(
    r'\[Address\(RVA\s*=\s*"(0x[0-9A-Fa-f]+)",\s*Offset\s*=\s*"(0x[0-9A-Fa-f]+)",\s*Length\s*=\s*"(0x[0-9A-Fa-f]+)"\)\]'
)
CURRENT_ADDRESS_RE = re.compile(
    r"^// RVA:\s*(0x[0-9A-Fa-f]+)\s+Offset:\s*(0x[0-9A-Fa-f]+)\s+VA:\s*(0x[0-9A-Fa-f]+)(?:\s+Slot:\s*(\d+))?"
)
CALLER_COUNT_RE = re.compile(r"\[CallerCount\(Count\s*=\s*(\d+)\)\]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def split_top_level(text: str) -> list[str]:
    if not text.strip():
        return []
    result: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return [item for item in result if item]


def strip_default(text: str) -> str:
    depth = 0
    for index, char in enumerate(text):
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth = max(0, depth - 1)
        elif char == "=" and depth == 0:
            return text[:index].strip()
    return text.strip()


def parameter_types(text: str) -> list[str]:
    output: list[str] = []
    for item in split_top_level(text):
        item = strip_default(item)
        parts = item.split()
        while parts and parts[0] in {"ref", "out", "in", "params", "this"}:
            parts.pop(0)
        if not parts:
            continue
        output.append(" ".join(parts[:-1]) if len(parts) > 1 else parts[0])
    return output


def simple_type(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"\b(?:ref|out|in|params)\s+", "", value)
    value = value.replace("System.", "")
    value = value.replace("Torappu.Battle.", "")
    value = value.replace("UnitDataFlowConfig.", "")
    value = re.sub(r"\s+", "", value)
    return value


def parse_method_declaration(line: str, type_name: str) -> dict[str, Any] | None:
    clean = line.strip()
    # Generated C# also contains string constants such as
    # ``private const string X = "...({0})...";``.  Their format braces and
    # parentheses must not be mistaken for a method declaration.
    if ";" in clean or "(" not in clean or ")" not in clean or "{" not in clean:
        return None
    if clean.startswith("[") or clean.startswith("//"):
        return None
    signature = clean.split("{", 1)[0].strip()
    left, right = signature.split("(", 1)
    parameters = right.rsplit(")", 1)[0]
    tokens = left.split()
    while tokens and tokens[0] in MODIFIERS:
        tokens.pop(0)
    if not tokens:
        return None
    if len(tokens) == 1:
        name = ".ctor" if tokens[0].split(".")[-1] == type_name.split(".")[-1] else tokens[0]
        return_type = "void"
    else:
        name = tokens[-1]
        return_type = " ".join(tokens[:-1])
        if name.split(".")[-1] == type_name.split(".")[-1]:
            name = ".ctor"
            return_type = "void"
    params = parameter_types(parameters)
    return {
        "name": name,
        "return_type": return_type,
        "parameters": params,
        "param_count": len(params),
    }


def parse_field_declaration(line: str) -> dict[str, str] | None:
    clean = line.strip()
    if not clean or "(" in clean or ";" not in clean or clean.startswith("["):
        return None
    declaration = clean.split("//", 1)[0].split(";", 1)[0].strip()
    declaration = declaration.split("=", 1)[0].strip()
    tokens = declaration.split()
    while tokens and tokens[0] in MODIFIERS.union({"const", "volatile"}):
        tokens.pop(0)
    if len(tokens) < 2:
        return None
    return {"name": tokens[-1], "field_type": " ".join(tokens[:-1])}


def parse_edge(line: str) -> dict[str, Any] | None:
    if not (line.startswith("[Calls(") or line.startswith("[CalledBy(")):
        return None
    kind = "calls" if line.startswith("[Calls(") else "called_by"
    type_match = re.search(r"Type\s*=\s*typeof\(([^)]+)\)", line)
    member_match = re.search(r'Member\s*=\s*"([^"]+)"', line)
    if not type_match or not member_match:
        return None
    return_match = re.search(r'ReturnType\s*=\s*(?:typeof\(([^)]+)\)|"([^"]+)")', line)
    params_match = re.search(r"MemberParameters\s*=\s*new .*?\{(.*?)\},\s*ReturnType", line)
    params_raw = params_match.group(1) if params_match else ""
    target_params = re.findall(r"typeof\(([^)]+)\)|\"([^\"]+)\"", params_raw)
    return {
        "direction": kind,
        "target_type": type_match.group(1),
        "target_member": member_match.group(1),
        "target_parameters": [left or right for left, right in target_params],
        "target_return": (return_match.group(1) or return_match.group(2)) if return_match else None,
        "raw": line,
    }


def parse_legacy_file(path: Path, relative_path: str) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    namespace = ""
    type_info: dict[str, Any] | None = None
    pending: list[str] = []
    methods: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        namespace_match = LEGACY_NAMESPACE_RE.match(line)
        if namespace_match:
            namespace = namespace_match.group(1)
            continue
        type_match = TYPE_RE.match(line)
        if type_match and type_info is None:
            type_token = next((TOKEN_RE.search(item).group(1) for item in reversed(pending) if TOKEN_RE.search(item)), None)
            type_info = {
                "namespace": namespace,
                "name": type_match.group("name"),
                "kind": type_match.group("kind"),
                "base_types": (type_match.group("bases") or "").strip(),
                "token": type_token,
                "source_path": relative_path,
                "source_line": line_number,
            }
            pending.clear()
            continue
        if line.startswith("["):
            pending.append(line)
            continue
        if type_info:
            method = parse_method_declaration(line, type_info["name"])
            if method:
                address = next((ADDRESS_RE.search(item) for item in pending if ADDRESS_RE.search(item)), None)
                token = next((TOKEN_RE.search(item).group(1) for item in reversed(pending) if TOKEN_RE.search(item)), None)
                caller = next((CALLER_COUNT_RE.search(item) for item in pending if CALLER_COUNT_RE.search(item)), None)
                method.update({
                    "token": token,
                    "old_rva": address.group(1) if address else None,
                    "old_offset": address.group(2) if address else None,
                    "length": address.group(3) if address else None,
                    "caller_count": int(caller.group(1)) if caller else None,
                    "source_line": line_number,
                    "edges": [edge for item in pending if (edge := parse_edge(item))],
                })
                methods.append(method)
                pending.clear()
                continue
            field = parse_field_declaration(line)
            if field:
                token = next((TOKEN_RE.search(item).group(1) for item in reversed(pending) if TOKEN_RE.search(item)), None)
                offset_match = next((FIELD_OFFSET_RE.search(item) for item in pending if FIELD_OFFSET_RE.search(item)), None)
                inline_offset = FIELD_OFFSET_RE.search(line)
                offset_match = offset_match or inline_offset
                field.update({
                    "token": token,
                    "old_offset": (offset_match.group(1) or offset_match.group(2)) if offset_match else None,
                    "source_line": line_number,
                })
                fields.append(field)
                pending.clear()
                continue
        if line and not line.startswith("//"):
            pending.clear()
    if not type_info or not (type_info["namespace"] == "Torappu" or type_info["namespace"].startswith("Torappu.Battle")):
        return None
    type_info["methods"] = methods
    type_info["fields"] = fields
    type_info["full_name"] = f"{type_info['namespace']}.{type_info['name']}" if type_info["namespace"] else type_info["name"]
    return type_info


def parse_current_dump(path: Path) -> list[dict[str, Any]]:
    types: list[dict[str, Any]] = []
    namespace = ""
    current: dict[str, Any] | None = None
    pending_address: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, raw in enumerate(stream, 1):
            line = raw.strip()
            namespace_match = CURRENT_NAMESPACE_RE.match(line)
            if namespace_match:
                namespace = namespace_match.group(1)
                current = None
                pending_address = None
                continue
            type_match = TYPE_RE.match(line)
            if type_match:
                current = None
                if namespace == "Torappu" or namespace.startswith("Torappu.Battle"):
                    name = type_match.group("name")
                    current = {
                        "namespace": namespace,
                        "name": name,
                        "full_name": f"{namespace}.{name}" if namespace else name,
                        "kind": type_match.group("kind"),
                        "base_types": (type_match.group("bases") or "").strip(),
                        "typedef_index": int(type_match.group("index")) if type_match.group("index") else None,
                        "source_line": line_number,
                        "methods": [],
                        "fields": [],
                    }
                    types.append(current)
                pending_address = None
                continue
            if not current:
                continue
            address_match = CURRENT_ADDRESS_RE.match(line)
            if address_match:
                pending_address = {
                    "current_rva": address_match.group(1),
                    "current_offset": address_match.group(2),
                    "current_va": address_match.group(3),
                    "slot": int(address_match.group(4)) if address_match.group(4) else None,
                }
                continue
            method = parse_method_declaration(line, current["name"])
            if method:
                method.update(pending_address or {})
                method["source_line"] = line_number
                current["methods"].append(method)
                pending_address = None
                continue
            field = parse_field_declaration(line)
            if field and "//" in line:
                offset_match = re.search(r"//\s*(0x[0-9A-Fa-f]+)", line)
                field.update({"current_offset": offset_match.group(1) if offset_match else None, "source_line": line_number})
                current["fields"].append(field)
    return types


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE legacy_types(
          id INTEGER PRIMARY KEY, namespace TEXT, name TEXT, full_name TEXT,
          kind TEXT, base_types TEXT, token TEXT, source_path TEXT, source_line INTEGER
        );
        CREATE TABLE current_types(
          id INTEGER PRIMARY KEY, namespace TEXT, name TEXT, full_name TEXT,
          kind TEXT, base_types TEXT, typedef_index INTEGER, source_line INTEGER
        );
        CREATE TABLE legacy_methods(
          id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL, name TEXT, return_type TEXT,
          parameters_json TEXT, param_count INTEGER, token TEXT, old_rva TEXT, old_offset TEXT,
          length TEXT, caller_count INTEGER, source_line INTEGER,
          FOREIGN KEY(type_id) REFERENCES legacy_types(id)
        );
        CREATE TABLE current_methods(
          id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL, name TEXT, return_type TEXT,
          parameters_json TEXT, param_count INTEGER, current_rva TEXT, current_offset TEXT,
          current_va TEXT, slot INTEGER, source_line INTEGER,
          FOREIGN KEY(type_id) REFERENCES current_types(id)
        );
        CREATE TABLE legacy_fields(
          id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL, name TEXT, field_type TEXT,
          token TEXT, old_offset TEXT, source_line INTEGER,
          FOREIGN KEY(type_id) REFERENCES legacy_types(id)
        );
        CREATE TABLE current_fields(
          id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL, name TEXT, field_type TEXT,
          current_offset TEXT, source_line INTEGER,
          FOREIGN KEY(type_id) REFERENCES current_types(id)
        );
        CREATE TABLE method_edges(
          id INTEGER PRIMARY KEY, source_method_id INTEGER NOT NULL, direction TEXT,
          target_type TEXT, target_member TEXT, target_parameters_json TEXT,
          target_return TEXT, raw TEXT,
          FOREIGN KEY(source_method_id) REFERENCES legacy_methods(id)
        );
        CREATE TABLE type_matches(
          legacy_type_id INTEGER PRIMARY KEY, current_type_id INTEGER,
          confidence REAL, status TEXT, evidence TEXT
        );
        CREATE TABLE method_matches(
          legacy_method_id INTEGER PRIMARY KEY, current_method_id INTEGER,
          confidence REAL, status TEXT, evidence TEXT
        );
        CREATE INDEX idx_legacy_type_name ON legacy_types(name);
        CREATE INDEX idx_current_type_name ON current_types(name);
        CREATE INDEX idx_legacy_method_name ON legacy_methods(name);
        CREATE INDEX idx_current_method_name ON current_methods(name);
        CREATE INDEX idx_edge_target ON method_edges(target_type,target_member);
        """
    )


def insert_types(db: sqlite3.Connection, legacy: list[dict[str, Any]], current: list[dict[str, Any]]) -> None:
    for item in legacy:
        cur = db.execute(
            "INSERT INTO legacy_types(namespace,name,full_name,kind,base_types,token,source_path,source_line) VALUES(?,?,?,?,?,?,?,?)",
            (item["namespace"], item["name"], item["full_name"], item["kind"], item["base_types"], item["token"], item["source_path"], item["source_line"]),
        )
        type_id = cur.lastrowid
        for field in item["fields"]:
            db.execute(
                "INSERT INTO legacy_fields(type_id,name,field_type,token,old_offset,source_line) VALUES(?,?,?,?,?,?)",
                (type_id, field["name"], field["field_type"], field["token"], field["old_offset"], field["source_line"]),
            )
        for method in item["methods"]:
            method_cur = db.execute(
                "INSERT INTO legacy_methods(type_id,name,return_type,parameters_json,param_count,token,old_rva,old_offset,length,caller_count,source_line) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (type_id, method["name"], method["return_type"], json.dumps(method["parameters"], ensure_ascii=False), method["param_count"], method["token"], method["old_rva"], method["old_offset"], method["length"], method["caller_count"], method["source_line"]),
            )
            for edge in method["edges"]:
                db.execute(
                    "INSERT INTO method_edges(source_method_id,direction,target_type,target_member,target_parameters_json,target_return,raw) VALUES(?,?,?,?,?,?,?)",
                    (method_cur.lastrowid, edge["direction"], edge["target_type"], edge["target_member"], json.dumps(edge["target_parameters"], ensure_ascii=False), edge["target_return"], edge["raw"]),
                )
    for item in current:
        cur = db.execute(
            "INSERT INTO current_types(namespace,name,full_name,kind,base_types,typedef_index,source_line) VALUES(?,?,?,?,?,?,?)",
            (item["namespace"], item["name"], item["full_name"], item["kind"], item["base_types"], item["typedef_index"], item["source_line"]),
        )
        type_id = cur.lastrowid
        for field in item["fields"]:
            db.execute(
                "INSERT INTO current_fields(type_id,name,field_type,current_offset,source_line) VALUES(?,?,?,?,?)",
                (type_id, field["name"], field["field_type"], field["current_offset"], field["source_line"]),
            )
        for method in item["methods"]:
            db.execute(
                "INSERT INTO current_methods(type_id,name,return_type,parameters_json,param_count,current_rva,current_offset,current_va,slot,source_line) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (type_id, method["name"], method["return_type"], json.dumps(method["parameters"], ensure_ascii=False), method["param_count"], method.get("current_rva"), method.get("current_offset"), method.get("current_va"), method.get("slot"), method["source_line"]),
            )


def build_matches(db: sqlite3.Connection) -> None:
    current_types = {row["full_name"]: row for row in db.execute("SELECT * FROM current_types")}
    for legacy_type in db.execute("SELECT * FROM legacy_types"):
        current_type = current_types.get(legacy_type["full_name"])
        if not current_type:
            db.execute("INSERT INTO type_matches VALUES(?,?,?,?,?)", (legacy_type["id"], None, 0.0, "unmatched", "no exact current full-name match"))
            continue
        base_equal = simple_type(legacy_type["base_types"]) == simple_type(current_type["base_types"])
        confidence = 1.0 if base_equal else 0.92
        evidence = "exact namespace+type+base" if base_equal else "exact namespace+type; base changed"
        db.execute("INSERT INTO type_matches VALUES(?,?,?,?,?)", (legacy_type["id"], current_type["id"], confidence, "matched", evidence))
        current_methods = list(db.execute("SELECT * FROM current_methods WHERE type_id=?", (current_type["id"],)))
        for legacy_method in db.execute("SELECT * FROM legacy_methods WHERE type_id=?", (legacy_type["id"],)):
            same_name = [m for m in current_methods if m["name"] == legacy_method["name"]]
            same_count = [m for m in same_name if m["param_count"] == legacy_method["param_count"]]
            same_return = [m for m in same_count if simple_type(m["return_type"]) == simple_type(legacy_method["return_type"])]
            chosen = None
            confidence = 0.0
            status = "unmatched"
            evidence = "no current method with same name"
            if len(same_return) == 1:
                chosen, confidence, status = same_return[0], 0.98, "matched_high"
                evidence = "exact type + method name + parameter count + normalized return type"
            elif len(same_count) == 1:
                chosen, confidence, status = same_count[0], 0.86, "matched_medium"
                evidence = "exact type + method name + parameter count"
            elif len(same_name) == 1:
                chosen, confidence, status = same_name[0], 0.66, "matched_low"
                evidence = "exact type + unique method name; signature changed"
            elif same_count:
                status = "ambiguous"
                evidence = f"{len(same_count)} current overloads share name and parameter count"
            elif same_name:
                status = "ambiguous"
                evidence = f"{len(same_name)} current overloads share method name"
            db.execute(
                "INSERT INTO method_matches VALUES(?,?,?,?,?)",
                (legacy_method["id"], chosen["id"] if chosen else None, confidence, status, evidence),
            )
    db.commit()


def scalar(db: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> Any:
    return db.execute(query, tuple(params)).fetchone()[0]


def build_index(args: argparse.Namespace) -> int:
    legacy_root = args.legacy_root.resolve()
    dump_path = args.current_dump.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    db_path = output / "code_reference.sqlite"
    if db_path.exists():
        raise RuntimeError(f"refusing existing index: {db_path}")
    legacy_files = sorted(legacy_root.rglob("*.cs"))
    legacy = []
    for path in legacy_files:
        parsed = parse_legacy_file(path, path.relative_to(legacy_root).as_posix())
        if parsed:
            legacy.append(parsed)
    current = parse_current_dump(dump_path)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        create_schema(db)
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "created_utc": utc_now(),
            "scope": "Torappu.Battle",
            "legacy_source_kind": "Il2CppInspector generated C# reference",
            "current_source_kind": "current PC Il2CppDumper dump.cs",
            "address_policy": "legacy RVA is reference-only; current RVA is stored separately and never copied from legacy",
        }
        db.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", metadata.items())
        insert_types(db, legacy, current)
        build_matches(db)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        stats = {
            "legacy_types": scalar(db, "SELECT COUNT(*) FROM legacy_types"),
            "current_types": scalar(db, "SELECT COUNT(*) FROM current_types"),
            "legacy_methods": scalar(db, "SELECT COUNT(*) FROM legacy_methods"),
            "current_methods": scalar(db, "SELECT COUNT(*) FROM current_methods"),
            "legacy_fields": scalar(db, "SELECT COUNT(*) FROM legacy_fields"),
            "current_fields": scalar(db, "SELECT COUNT(*) FROM current_fields"),
            "call_edges": scalar(db, "SELECT COUNT(*) FROM method_edges WHERE direction='calls'"),
            "called_by_edges": scalar(db, "SELECT COUNT(*) FROM method_edges WHERE direction='called_by'"),
            "matched_types": scalar(db, "SELECT COUNT(*) FROM type_matches WHERE current_type_id IS NOT NULL"),
            "matched_methods": scalar(db, "SELECT COUNT(*) FROM method_matches WHERE current_method_id IS NOT NULL"),
            "high_method_matches": scalar(db, "SELECT COUNT(*) FROM method_matches WHERE status='matched_high'"),
            "ambiguous_methods": scalar(db, "SELECT COUNT(*) FROM method_matches WHERE status='ambiguous'"),
        }
    finally:
        db.close()
    source_records = []
    for label, path in (
        ("legacy_generated_archive", args.source_archive),
        ("legacy_metadata", args.legacy_metadata),
        ("legacy_metadata_windows_2026jan", args.legacy_metadata_windows),
        ("current_pc_dump_cs", dump_path),
        ("current_pc_metadata", args.current_metadata),
    ):
        path = path.resolve()
        source_records.append({
            "label": label,
            "path": str(path),
            "bytes": path.stat().st_size if path.is_file() else None,
            "sha256": sha256_file(path) if path.is_file() else None,
            "status": "present" if path.is_file() else "missing",
        })
    current_hash = next((r["sha256"] for r in source_records if r["label"] == "current_pc_metadata"), None)
    for record in source_records:
        if record["label"].startswith("legacy_metadata"):
            record["matches_current_pc_metadata"] = bool(current_hash and record["sha256"] == current_hash)
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": utc_now(),
        "status": "passed_static_reference_index" if integrity == "ok" and stats["matched_types"] else "partial",
        "scope": "Torappu.Battle only",
        "index": {"path": str(db_path), "bytes": db_path.stat().st_size, "sha256": sha256_file(db_path), "integrity_check": integrity},
        "statistics": stats,
        "sources": source_records,
        "policy": {
            "group_executables_run": False,
            "game_started": False,
            "adb_started": False,
            "ghidra_started": False,
            "runtime_observation": False,
            "legacy_rva_imported_as_current": False,
            "current_rva_source": str(dump_path),
            "cross_version_edges": "reference candidates until verified against current native code or current-version call evidence",
        },
    }
    write_json(output / "code_reference_report.json", report)
    print(json.dumps({"status": report["status"], "index": str(db_path), "statistics": stats}, ensure_ascii=False))
    return 0 if integrity == "ok" else 3


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("code reference index failed integrity_check")
    return db


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def query_index(args: argparse.Namespace) -> dict[str, Any]:
    db = open_db(args.index)
    try:
        term = f"%{args.query}%"
        limit = args.limit
        types = [dict(row) for row in db.execute(
            """SELECT l.full_name,l.kind,l.base_types,l.token,l.source_path,
                      c.typedef_index,t.confidence,t.status,t.evidence
               FROM legacy_types l LEFT JOIN type_matches t ON t.legacy_type_id=l.id
               LEFT JOIN current_types c ON c.id=t.current_type_id
               WHERE l.full_name LIKE ? OR l.name LIKE ? ORDER BY t.confidence DESC,l.full_name LIMIT ?""",
            (term, term, limit),
        )]
        methods = [dict(row) for row in db.execute(
            """SELECT l.full_name AS type_name,m.name,m.return_type,m.parameters_json,m.token,m.old_rva,
                      cm.current_rva,mm.confidence,mm.status,mm.evidence
               FROM legacy_methods m JOIN legacy_types l ON l.id=m.type_id
               LEFT JOIN method_matches mm ON mm.legacy_method_id=m.id
               LEFT JOIN current_methods cm ON cm.id=mm.current_method_id
               WHERE l.full_name LIKE ? OR m.name LIKE ? ORDER BY mm.confidence DESC,l.full_name,m.name LIMIT ?""",
            (term, term, limit),
        )]
        fields = [dict(row) for row in db.execute(
            """SELECT l.full_name AS type_name,f.name,f.field_type,f.token,f.old_offset
               FROM legacy_fields f JOIN legacy_types l ON l.id=f.type_id
               WHERE l.full_name LIKE ? OR f.name LIKE ? ORDER BY l.full_name,f.name LIMIT ?""",
            (term, term, limit),
        )]
        edges = [dict(row) for row in db.execute(
            """SELECT l.full_name AS source_type,m.name AS source_method,e.direction,e.target_type,e.target_member
               FROM method_edges e JOIN legacy_methods m ON m.id=e.source_method_id
               JOIN legacy_types l ON l.id=m.type_id
               WHERE l.full_name LIKE ? OR m.name LIKE ? OR e.target_type LIKE ? OR e.target_member LIKE ?
               ORDER BY l.full_name,m.name LIMIT ?""",
            (term, term, term, term, limit),
        )]
        return {"action": "query", "query": args.query, "index": str(args.index), "types": types, "methods": methods, "fields": fields, "edges": edges}
    finally:
        db.close()


def resolve_type(db: sqlite3.Connection, text: str) -> list[sqlite3.Row]:
    exact = list(db.execute("SELECT * FROM legacy_types WHERE full_name=? OR name=? ORDER BY full_name", (text, text)))
    if exact:
        return exact
    return list(db.execute("SELECT * FROM legacy_types WHERE full_name LIKE ? OR name LIKE ? ORDER BY full_name LIMIT 20", (f"%{text}%", f"%{text}%")))


def map_type(args: argparse.Namespace) -> dict[str, Any]:
    db = open_db(args.index)
    try:
        candidates = resolve_type(db, args.type)
        output = []
        for legacy_type in candidates:
            match = db.execute(
                "SELECT t.*,c.* FROM type_matches t LEFT JOIN current_types c ON c.id=t.current_type_id WHERE t.legacy_type_id=?",
                (legacy_type["id"],),
            ).fetchone()
            methods = [dict(row) for row in db.execute(
                """SELECT lm.name,lm.return_type,lm.parameters_json,lm.token,lm.old_rva,
                          cm.current_rva,cm.current_va,mm.confidence,mm.status,mm.evidence
                   FROM legacy_methods lm LEFT JOIN method_matches mm ON mm.legacy_method_id=lm.id
                   LEFT JOIN current_methods cm ON cm.id=mm.current_method_id
                   WHERE lm.type_id=? ORDER BY lm.name,lm.param_count LIMIT ?""",
                (legacy_type["id"], args.limit),
            )]
            fields = [dict(row) for row in db.execute(
                """SELECT lf.name,lf.field_type,lf.old_offset,cf.current_offset
                   FROM legacy_fields lf LEFT JOIN type_matches tm ON tm.legacy_type_id=lf.type_id
                   LEFT JOIN current_fields cf ON cf.type_id=tm.current_type_id AND cf.name=lf.name
                   WHERE lf.type_id=? ORDER BY lf.name LIMIT ?""",
                (legacy_type["id"], args.limit),
            )]
            output.append({"legacy_type": dict(legacy_type), "type_match": dict(match) if match else None, "methods": methods, "fields": fields})
        return {"action": "map", "query_type": args.type, "index": str(args.index), "candidate_count": len(output), "candidates": output}
    finally:
        db.close()


def resolve_method(db: sqlite3.Connection, type_text: str, method_text: str) -> list[sqlite3.Row]:
    types = resolve_type(db, type_text)
    rows: list[sqlite3.Row] = []
    for type_row in types:
        exact = list(db.execute("SELECT * FROM legacy_methods WHERE type_id=? AND name=?", (type_row["id"], method_text)))
        rows.extend(exact or list(db.execute("SELECT * FROM legacy_methods WHERE type_id=? AND name LIKE ?", (type_row["id"], f"%{method_text}%"))))
    return rows[:20]


def resolved_edge_target(db: sqlite3.Connection, edge: sqlite3.Row) -> dict[str, Any]:
    target_simple = edge["target_type"].split(".")[-1]
    types = list(db.execute("SELECT * FROM legacy_types WHERE name=? OR full_name=?", (target_simple, edge["target_type"])))
    methods: list[sqlite3.Row] = []
    for type_row in types:
        methods.extend(db.execute("SELECT * FROM legacy_methods WHERE type_id=? AND name=?", (type_row["id"], edge["target_member"])))
    resolved = None
    if len(methods) == 1:
        method = methods[0]
        mapping = db.execute(
            """SELECT cm.current_rva,cm.current_va,mm.confidence,mm.status,lt.full_name
               FROM method_matches mm LEFT JOIN current_methods cm ON cm.id=mm.current_method_id
               JOIN legacy_methods lm ON lm.id=mm.legacy_method_id JOIN legacy_types lt ON lt.id=lm.type_id
               WHERE mm.legacy_method_id=?""",
            (method["id"],),
        ).fetchone()
        resolved = {"legacy_method_id": method["id"], "legacy_type": types[0]["full_name"] if len(types) == 1 else target_simple, "current_mapping": row_dict(mapping)}
    return {"candidate_count": len(methods), "resolved": resolved}


def trace_method(args: argparse.Namespace) -> dict[str, Any]:
    if args.depth < 0 or args.depth > 2:
        raise RuntimeError("trace depth is strictly limited to 0..2")
    db = open_db(args.index)
    try:
        starts = resolve_method(db, args.type, args.method)
        queue = deque((row["id"], 0) for row in starts)
        visited: set[tuple[int, int]] = set()
        nodes: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []
        while queue and len(nodes) < args.limit:
            method_id, depth = queue.popleft()
            key = (method_id, depth)
            if key in visited:
                continue
            visited.add(key)
            method = db.execute(
                """SELECT lm.*,lt.full_name,cm.current_rva,cm.current_va,mm.confidence,mm.status,mm.evidence
                   FROM legacy_methods lm JOIN legacy_types lt ON lt.id=lm.type_id
                   LEFT JOIN method_matches mm ON mm.legacy_method_id=lm.id
                   LEFT JOIN current_methods cm ON cm.id=mm.current_method_id WHERE lm.id=?""",
                (method_id,),
            ).fetchone()
            if not method:
                continue
            nodes.append({"depth": depth, **dict(method)})
            if depth >= args.depth:
                continue
            directions = ("calls", "called_by") if args.direction == "both" else (args.direction,)
            for direction in directions:
                for edge in db.execute("SELECT * FROM method_edges WHERE source_method_id=? AND direction=?", (method_id, direction)):
                    resolved = resolved_edge_target(db, edge)
                    edge_record = {"depth": depth + 1, **dict(edge), "resolution": resolved}
                    edges_out.append(edge_record)
                    if resolved["resolved"] and resolved["resolved"]["legacy_method_id"]:
                        queue.append((resolved["resolved"]["legacy_method_id"], depth + 1))
        return {
            "action": "trace",
            "query_type": args.type,
            "query_method": args.method,
            "depth_limit": args.depth,
            "direction": args.direction,
            "index": str(args.index),
            "start_candidates": len(starts),
            "nodes": nodes,
            "edges": edges_out[: args.limit * 8],
            "evidence_policy": "Calls/CalledBy are proven for the legacy reference version. Current RVA mapping is structural cross-version evidence, not a proven current call edge.",
        }
    finally:
        db.close()


def render_text(result: dict[str, Any]) -> str:
    action = result["action"]
    if action == "query":
        lines = [f"Code query: {result['query']}"]
        for section in ("types", "methods", "fields", "edges"):
            lines.append(f"\n[{section}] {len(result[section])}")
            for item in result[section]:
                lines.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(lines)
    if action == "map":
        lines = [f"Type map: {result['query_type']} ({result['candidate_count']} candidate(s))"]
        for candidate in result["candidates"]:
            legacy = candidate["legacy_type"]
            match = candidate["type_match"] or {}
            lines.append(f"\n{legacy['full_name']} -> {match.get('full_name') or '<unmatched>'} [{match.get('status')}] confidence={match.get('confidence')}")
            lines.append("Methods:")
            for method in candidate["methods"]:
                lines.append(f"  {method['name']} old={method['old_rva']} current={method['current_rva']} {method['status']} ({method['confidence']})")
            lines.append("Fields:")
            for field in candidate["fields"]:
                lines.append(f"  {field['name']} old={field['old_offset']} current={field['current_offset']}")
        return "\n".join(lines)
    lines = [f"Trace: {result['query_type']}::{result['query_method']} depth={result['depth_limit']} direction={result['direction']}"]
    for node in result["nodes"]:
        lines.append(f"  depth={node['depth']} {node['full_name']}::{node['name']} old={node['old_rva']} current={node['current_rva']} {node['status']}")
    lines.append("Edges:")
    for edge in result["edges"]:
        current = ((edge.get("resolution") or {}).get("resolved") or {}).get("current_mapping") or {}
        lines.append(f"  {edge['direction']} -> {edge['target_type']}::{edge['target_member']} current={current.get('current_rva')}")
    lines.append(result["evidence_policy"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Arknights PC cross-version battle code reference index")
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("index")
    build.add_argument("--legacy-root", type=Path, required=True)
    build.add_argument("--source-archive", type=Path, required=True)
    build.add_argument("--legacy-metadata", type=Path, required=True)
    build.add_argument("--legacy-metadata-windows", type=Path, required=True)
    build.add_argument("--current-dump", type=Path, required=True)
    build.add_argument("--current-metadata", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    for name in ("query", "map", "trace"):
        command = sub.add_parser(name)
        command.add_argument("--index", type=Path, required=True)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--json", action="store_true")
        command.add_argument("--output", type=Path)
        if name == "query":
            command.add_argument("--query", required=True)
        else:
            command.add_argument("--type", required=True)
        if name == "trace":
            command.add_argument("--method", required=True)
            command.add_argument("--depth", type=int, default=2)
            command.add_argument("--direction", choices=("calls", "called_by", "both"), default="both")
    args = parser.parse_args()
    if args.action == "index":
        return build_index(args)
    if not args.index.is_file():
        raise RuntimeError(f"missing code reference index: {args.index}")
    if args.action == "query":
        result = query_index(args)
    elif args.action == "map":
        result = map_type(args)
    else:
        result = trace_method(args)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_text(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8-sig")
    print(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
