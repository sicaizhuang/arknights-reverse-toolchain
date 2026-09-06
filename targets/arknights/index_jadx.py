#!/usr/bin/env python3
"""Build a searchable, provenance-backed index from the retained partial JADX output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)
CLASS_RE = re.compile(
    r"(?m)^\s*((?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|strictfp)\s+)*)"
    r"(class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s+extends\s+([^\s{]+))?(?:\s+implements\s+([^\{]+))?"
)
METHOD_RE = re.compile(
    r"(?m)^\s*((?:(?:public|protected|private|abstract|final|static|synchronized|native|strictfp|default)\s+)*)"
    r"(?:<[^>]+>\s*)?([\w.$<>\[\]?]+)\s+([A-Za-z_$][\w$]*)\s*\(([^;{}]*)\)\s*(?:throws\s+[^\{;]+)?(?:\{|;)"
)
FIELD_RE = re.compile(
    r"(?m)^\s*((?:(?:public|protected|private|final|static|transient|volatile)\s+)*)"
    r"([\w.$<>\[\]?]+)\s+([A-Za-z_$][\w$]*)\s*(?:=[^;]*)?;\s*$"
)
STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
RESOURCE_RE = re.compile(r"\bR\.(\w+)\.([A-Za-z0-9_]+)\b|@([\w.]+/[-\w.]+)")
ERROR_PATTERNS = {
    "method_not_decompiled": re.compile(r"Method not decompiled(?::\s*([^\r\n*]+))?", re.I),
    "jadx_error_comment": re.compile(r"JADX ERROR:\s*([^\r\n*]+)", re.I),
    "decode_exception": re.compile(r"DecodeException(?::\s*([^\r\n*]+))?", re.I),
    "bogus_opcode": re.compile(r"bogus opcode[^\r\n]*", re.I),
    "code_decompiled_incorrectly": re.compile(r"Code decompiled incorrectly[^\r\n]*", re.I),
    "duplicate_class": re.compile(r"duplicate class[^\r\n]*", re.I),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def init_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(
        """
        CREATE TABLE files(id INTEGER PRIMARY KEY,relative_path TEXT UNIQUE,kind TEXT,size INTEGER,sha256 TEXT,package_name TEXT,parse_status TEXT,error_marker_count INTEGER DEFAULT 0);
        CREATE TABLE classes(id INTEGER PRIMARY KEY,file_id INTEGER,package_name TEXT,name TEXT,qualified_name TEXT,kind TEXT,modifiers TEXT,extends_name TEXT,implements_names TEXT,line INTEGER);
        CREATE INDEX idx_classes_name ON classes(name); CREATE INDEX idx_classes_qualified ON classes(qualified_name);
        CREATE TABLE methods(id INTEGER PRIMARY KEY,file_id INTEGER,class_name TEXT,name TEXT,signature TEXT,return_type TEXT,modifiers TEXT,line INTEGER,decompile_status TEXT);
        CREATE INDEX idx_methods_name ON methods(name); CREATE INDEX idx_methods_class ON methods(class_name);
        CREATE TABLE fields(id INTEGER PRIMARY KEY,file_id INTEGER,class_name TEXT,name TEXT,field_type TEXT,modifiers TEXT,line INTEGER);
        CREATE INDEX idx_fields_name ON fields(name);
        CREATE TABLE imports(file_id INTEGER,name TEXT); CREATE INDEX idx_imports_name ON imports(name);
        CREATE TABLE strings(id INTEGER PRIMARY KEY,file_id INTEGER,line INTEGER,value TEXT); CREATE INDEX idx_strings_value ON strings(value);
        CREATE TABLE resource_refs(id INTEGER PRIMARY KEY,file_id INTEGER,line INTEGER,reference TEXT); CREATE INDEX idx_resource_refs_reference ON resource_refs(reference);
        CREATE TABLE components(id INTEGER PRIMARY KEY,component_type TEXT,name TEXT,exported TEXT,enabled TEXT,permission TEXT,process TEXT,source TEXT);
        CREATE TABLE errors(id INTEGER PRIMARY KEY,file_id INTEGER,line INTEGER,category TEXT,message TEXT,evidence TEXT);
        CREATE INDEX idx_errors_category ON errors(category);
        CREATE VIRTUAL TABLE search USING fts5(kind, name, qualified_name, content, source_path UNINDEXED);
        """
    )
    return db


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def nearest_class(classes: list[dict], line: int, fallback: str) -> str:
    prior = [row for row in classes if row["line"] <= line]
    return prior[-1]["qualified_name"] if prior else fallback


def index_java(db: sqlite3.Connection, root: Path, path: Path) -> Counter:
    relative = path.relative_to(root).as_posix()
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest().upper()
    text = data.decode("utf-8", errors="replace")
    package_match = PACKAGE_RE.search(text)
    package = package_match.group(1) if package_match else ""
    errors = []
    for category, pattern in ERROR_PATTERNS.items():
        for match in pattern.finditer(text):
            errors.append((line_number(text, match.start()), category, (match.group(1) if match.lastindex else match.group(0)) or match.group(0), match.group(0)[:2048]))
    parse_status = "partial" if errors else "indexed"
    cursor = db.execute(
        "INSERT INTO files(relative_path,kind,size,sha256,package_name,parse_status,error_marker_count) VALUES(?,?,?,?,?,?,?)",
        (relative, "java", len(data), digest, package, parse_status, len(errors)),
    )
    file_id = cursor.lastrowid
    class_rows = []
    for match in CLASS_RE.finditer(text):
        name = match.group(3)
        qualified = f"{package}.{name}" if package else name
        row = {"name": name, "qualified_name": qualified, "line": line_number(text, match.start())}
        class_rows.append(row)
        db.execute(
            "INSERT INTO classes(file_id,package_name,name,qualified_name,kind,modifiers,extends_name,implements_names,line) VALUES(?,?,?,?,?,?,?,?,?)",
            (file_id, package, name, qualified, match.group(2), match.group(1).strip(), match.group(4), (match.group(5) or "").strip(), row["line"]),
        )
        db.execute("INSERT INTO search(kind,name,qualified_name,content,source_path) VALUES(?,?,?,?,?)", ("class", name, qualified, match.group(0), relative))
    fallback_class = f"{package}.{path.stem}" if package else path.stem
    for match in METHOD_RE.finditer(text):
        line = line_number(text, match.start())
        name = match.group(3)
        owner = nearest_class(class_rows, line, fallback_class)
        signature = re.sub(r"\s+", " ", match.group(0).strip())
        status = "failed" if any(abs(line - error[0]) < 40 and error[1] == "method_not_decompiled" for error in errors) else "indexed_lexically"
        db.execute(
            "INSERT INTO methods(file_id,class_name,name,signature,return_type,modifiers,line,decompile_status) VALUES(?,?,?,?,?,?,?,?)",
            (file_id, owner, name, signature, match.group(2), match.group(1).strip(), line, status),
        )
        db.execute("INSERT INTO search(kind,name,qualified_name,content,source_path) VALUES(?,?,?,?,?)", ("method", name, f"{owner}.{name}", signature, relative))
    for match in FIELD_RE.finditer(text):
        line = line_number(text, match.start())
        name = match.group(3)
        owner = nearest_class(class_rows, line, fallback_class)
        db.execute(
            "INSERT INTO fields(file_id,class_name,name,field_type,modifiers,line) VALUES(?,?,?,?,?,?)",
            (file_id, owner, name, match.group(2), match.group(1).strip(), line),
        )
        db.execute("INSERT INTO search(kind,name,qualified_name,content,source_path) VALUES(?,?,?,?,?)", ("field", name, f"{owner}.{name}", match.group(0).strip(), relative))
    for match in IMPORT_RE.finditer(text):
        db.execute("INSERT INTO imports(file_id,name) VALUES(?,?)", (file_id, match.group(1)))
    seen_strings = set()
    for match in STRING_RE.finditer(text):
        value = match.group(1)
        if not value or len(value) > 4096:
            continue
        key = (line_number(text, match.start()), value)
        if key in seen_strings:
            continue
        seen_strings.add(key)
        db.execute("INSERT INTO strings(file_id,line,value) VALUES(?,?,?)", (file_id, key[0], value))
        if len(value) >= 3:
            db.execute("INSERT INTO search(kind,name,qualified_name,content,source_path) VALUES(?,?,?,?,?)", ("string", value[:256], "", value, relative))
    seen_refs = set()
    for match in RESOURCE_RE.finditer(text):
        ref = f"R.{match.group(1)}.{match.group(2)}" if match.group(1) else f"@{match.group(3)}"
        key = (line_number(text, match.start()), ref)
        if key not in seen_refs:
            seen_refs.add(key)
            db.execute("INSERT INTO resource_refs(file_id,line,reference) VALUES(?,?,?)", (file_id, key[0], ref))
    for line, category, message, evidence in errors:
        db.execute("INSERT INTO errors(file_id,line,category,message,evidence) VALUES(?,?,?,?,?)", (file_id, line, category, message[:2048], evidence))
    return Counter(error[1] for error in errors)


def index_resource(db: sqlite3.Connection, root: Path, path: Path) -> None:
    relative = path.relative_to(root).as_posix()
    size = path.stat().st_size
    kind = "xml" if path.suffix.lower() == ".xml" else "resource"
    digest = sha256(path) if size <= 16 * 1024 * 1024 else None
    db.execute(
        "INSERT INTO files(relative_path,kind,size,sha256,package_name,parse_status,error_marker_count) VALUES(?,?,?,?,?,?,0)",
        (relative, kind, size, digest, "", "indexed_metadata" if digest is None else "indexed"),
    )


def index_manifest(db: sqlite3.Connection, manifest: Path) -> Counter:
    counts = Counter()
    root = ElementTree.parse(manifest).getroot()
    application = root.find("application")
    if application is None:
        return counts
    mapping = {"activity": "Activity", "activity-alias": "ActivityAlias", "service": "Service", "receiver": "Receiver", "provider": "Provider"}
    for tag, component_type in mapping.items():
        for node in application.findall(tag):
            db.execute(
                "INSERT INTO components(component_type,name,exported,enabled,permission,process,source) VALUES(?,?,?,?,?,?,?)",
                (component_type, node.get(ANDROID_NS + "name"), node.get(ANDROID_NS + "exported"), node.get(ANDROID_NS + "enabled"), node.get(ANDROID_NS + "permission"), node.get(ANDROID_NS + "process"), str(manifest)),
            )
            counts[component_type] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jadx-root", type=Path, required=True)
    parser.add_argument("--base-apk", type=Path, required=True)
    parser.add_argument("--jadx-command", type=Path, required=True)
    parser.add_argument("--jadx-stdout", type=Path, required=True)
    parser.add_argument("--jadx-stderr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    root = args.jadx_root.resolve()
    db_path = output / "java_index.sqlite"
    db = init_db(db_path)
    all_files = sorted((path for path in root.rglob("*") if path.is_file()), key=lambda p: str(p).lower())
    error_categories = Counter()
    java_count = 0
    for index, path in enumerate(all_files, 1):
        if path.suffix.lower() == ".java":
            java_count += 1
            error_categories.update(index_java(db, root, path))
        else:
            index_resource(db, root, path)
        if index % 250 == 0:
            db.commit()
    manifest = root / "resources" / "AndroidManifest.xml"
    component_counts = index_manifest(db, manifest) if manifest.exists() else Counter()
    db.commit()

    stdout = args.jadx_stdout.read_text(encoding="utf-8-sig", errors="replace")
    stderr = args.jadx_stderr.read_text(encoding="utf-8-sig", errors="replace")
    command = json.loads(args.jadx_command.read_text(encoding="utf-8-sig"))
    count_match = re.search(r"finished with errors, count:\s*(\d+)", stdout, re.I)
    jadx_error_count = int(count_match.group(1)) if count_match else None
    localized_failures = db.execute("SELECT COUNT(DISTINCT file_id || ':' || line || ':' || category) FROM errors").fetchone()[0]
    duplicate_count = error_categories.get("duplicate_class", 0)
    resource_error_count = sum(1 for line in (stdout + "\n" + stderr).splitlines() if re.search(r"resource.*(?:error|fail)", line, re.I))
    ordinary_warning_count = sum(1 for line in (stdout + "\n" + stderr).splitlines() if re.search(r"\bWARN(?:ING)?\b", line, re.I))
    unlocalized = max(0, (jadx_error_count or 0) - localized_failures)
    summary = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "status": "partial",
        "provenance": {
            "base_apk": str(args.base_apk.resolve()),
            "base_apk_sha256": sha256(args.base_apk.resolve()),
            "jadx_root": str(root),
            "jadx_command": command,
            "jadx_command_sha256": sha256(args.jadx_command),
            "jadx_exit_code": command.get("exit_code"),
            "stdout": str(args.jadx_stdout.resolve()),
            "stdout_sha256": sha256(args.jadx_stdout),
            "stderr": str(args.jadx_stderr.resolve()),
            "stderr_sha256": sha256(args.jadx_stderr),
            "reuse_status": "verified_reuse_not_rerun",
        },
        "coverage": {
            "total_files": len(all_files),
            "java_files": java_count,
            "resource_files": len(all_files) - java_count,
            "packages": db.execute("SELECT COUNT(DISTINCT package_name) FROM files WHERE package_name<>''").fetchone()[0],
            "classes": db.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
            "methods": db.execute("SELECT COUNT(*) FROM methods").fetchone()[0],
            "fields": db.execute("SELECT COUNT(*) FROM fields").fetchone()[0],
            "strings": db.execute("SELECT COUNT(*) FROM strings").fetchone()[0],
            "resource_references": db.execute("SELECT COUNT(*) FROM resource_refs").fetchone()[0],
            "components": dict(component_counts),
        },
        "jadx_errors": {
            "reported_total": jadx_error_count,
            "localized_output_markers": localized_failures,
            "localized_category_counts": dict(error_categories),
            "true_decompilation_failure_markers": sum(value for key, value in error_categories.items() if key not in {"duplicate_class"}),
            "duplicate_class_markers": duplicate_count,
            "resource_error_log_lines": resource_error_count,
            "ordinary_warning_log_lines": ordinary_warning_count,
            "unlocalized_reported_errors": unlocalized,
            "classification_limit": "JADX stdout only retained the aggregate 111 count. Errors without a generated source marker cannot be assigned a more specific category without inventing evidence.",
        },
        "outputs": {"sqlite": str(db_path)},
        "limitations": [
            "The Java index is lexical and searchable; it is not a Java compiler or proof of semantic correctness.",
            "JADX returned a non-zero exit code and explicitly reported errors, so source recovery remains partial.",
            "Large binary resources are indexed by path and size without duplicating or hashing their payload in this module.",
        ],
    }
    write_json(output / "java_analysis.json", summary)
    db.close()
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
