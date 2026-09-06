#!/usr/bin/env python3
"""Read-only cross-layer search for the retained Arknights static indexes."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def ro(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def add(results: list[dict], **record) -> None:
    record.setdefault("confidence", "direct_static_evidence")
    results.append(record)


def like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def query_unity(path: str, token: str, limit: int, results: list[dict]) -> None:
    db = ro(path)
    pattern = like(token)
    for row in db.execute(
        """SELECT o.name,o.container,o.path_id,o.unity_type,o.raw_support,o.convert_support,
                  b.source_path,b.sha256,b.relative_path
           FROM objects o JOIN bundles b ON b.id=o.bundle_id
           WHERE o.name LIKE ? ESCAPE '\\' OR o.container LIKE ? ESCAPE '\\'
           LIMIT ?""", (pattern, pattern, limit)):
        add(results, layer="unity", evidence_type="parsed_object_metadata", value=row[0] or row[1] or row[2],
            source_path=row[6], source_sha256=row[7], status="parsed_not_necessarily_exported",
            details={"container": row[1], "path_id": row[2], "unity_type": row[3], "raw_support": row[4], "convert_support": row[5], "bundle": row[8]})
    for row in db.execute("SELECT relative_path,source_path,sha256,parse_status,failure_reason FROM bundles WHERE relative_path LIKE ? ESCAPE '\\' LIMIT ?", (pattern, limit)):
        add(results, layer="unity", evidence_type="bundle_path", value=row[0], source_path=row[1], source_sha256=row[2],
            status=row[3], details={"failure_reason": row[4]})
    db.close()


def query_java(path: str, jadx_root: str, token: str, limit: int, results: list[dict], kinds: set[str]) -> None:
    db = ro(path)
    pattern = like(token)
    queries = [
        ("class", "SELECT c.qualified_name,f.relative_path,f.sha256,c.kind,c.line FROM classes c JOIN files f ON f.id=c.file_id WHERE c.name LIKE ? ESCAPE '\\' OR c.qualified_name LIKE ? ESCAPE '\\' LIMIT ?"),
        ("method", "SELECT COALESCE(m.class_name,'') || '.' || m.name,f.relative_path,f.sha256,m.signature,m.line,m.decompile_status FROM methods m JOIN files f ON f.id=m.file_id WHERE m.name LIKE ? ESCAPE '\\' OR m.signature LIKE ? ESCAPE '\\' LIMIT ?"),
        ("field", "SELECT f2.class_name || '.' || f2.name,f.relative_path,f.sha256,f2.field_type,f2.line FROM fields f2 JOIN files f ON f.id=f2.file_id WHERE f2.name LIKE ? ESCAPE '\\' OR f2.class_name LIKE ? ESCAPE '\\' LIMIT ?"),
        ("string", "SELECT s.value,f.relative_path,f.sha256,s.line,NULL FROM strings s JOIN files f ON f.id=s.file_id WHERE s.value LIKE ? ESCAPE '\\' LIMIT ?"),
    ]
    for kind, sql in queries:
        if kind not in kinds:
            continue
        parameters = (pattern, limit) if kind == "string" else (pattern, pattern, limit)
        for row in db.execute(sql, parameters):
            relative = row[1]
            status = row[5] if kind == "method" and len(row) > 5 and row[5] else "retained_jadx_output"
            add(results, layer="java", evidence_type=kind, value=row[0],
                source_path=str((Path(jadx_root) / relative).resolve()), source_sha256=row[2], status=status,
                details={"detail": row[3], "line": row[4]})
    db.close()


def query_java_fallback(path: str, token: str, limit: int, results: list[dict]) -> None:
    db = ro(path)
    pattern = like(token)
    for row in db.execute(
        "SELECT qualified_class,method_name,status,java_path,java_sha256,group_id,reason FROM issues WHERE qualified_class LIKE ? ESCAPE '\\' OR method_name LIKE ? ESCAPE '\\' LIMIT ?",
        (pattern, pattern, limit)):
        add(results, layer="java_fallback", evidence_type="jadx_issue_smali_fallback", value=".".join(x for x in row[:2] if x),
            source_path=row[3], source_sha256=row[4], status=row[2], details={"group_id": row[5], "reason": row[6]})
    db.close()


def query_native(path: str, token: str, limit: int, results: list[dict]) -> None:
    db = ro(path)
    pattern = like(token)
    for row in db.execute(
        "SELECT n.name,f.path,f.sha256,n.address,n.size,n.boundary_source,n.status,n.pseudocode_path,n.pseudocode_sha256 FROM functions n JOIN files f ON f.id=n.file_id WHERE n.name LIKE ? ESCAPE '\\' LIMIT ?",
        (pattern, limit)):
        add(results, layer="native", evidence_type="current_symbol_function_boundary", value=row[0], source_path=row[1], source_sha256=row[2],
            status=row[6], details={"static_elf_va_rva": row[3], "size": row[4], "boundary_source": row[5], "pseudocode": row[7], "pseudocode_sha256": row[8], "runtime_verified": False})
    for row in db.execute(
        "SELECT c.target_name,f.path,f.sha256,n.name,c.call_address,c.target_address,c.call_kind,c.target_source FROM calls c JOIN functions n ON n.id=c.caller_function_id JOIN files f ON f.id=c.file_id WHERE c.target_name LIKE ? ESCAPE '\\' OR n.name LIKE ? ESCAPE '\\' LIMIT ?",
        (pattern, pattern, limit)):
        add(results, layer="native", evidence_type="current_aarch64_call_instruction", value=f"{row[3]} -> {row[0]}", source_path=row[1], source_sha256=row[2],
            status="direct_call" if row[6] == "direct_bl" else "unresolved_indirect_call",
            details={"call_static_elf_va_rva": row[4], "target_static_elf_va_rva": row[5], "kind": row[6], "target_source": row[7], "runtime_verified": False})
    db.close()


def query_native_structure(path: str, token: str, limit: int, results: list[dict]) -> None:
    db = ro(path)
    pattern = like(token)
    for row in db.execute("SELECT s.value,f.path,f.sha256,s.section,s.virtual_address,s.evidence FROM strings s JOIN files f ON f.id=s.file_id WHERE s.value LIKE ? ESCAPE '\\' LIMIT ?", (pattern, limit)):
        add(results, layer="native", evidence_type="current_elf_string", value=row[0], source_path=row[1], source_sha256=row[2], status="current_static_string",
            details={"section": row[3], "static_elf_va_rva": row[4], "evidence": row[5], "runtime_verified": False})
    for table, evidence in (("symbols", "current_elf_symbol"), ("exports", "current_elf_export"), ("imports", "current_elf_import")):
        for row in db.execute(f"SELECT t.name,f.path,f.sha256 FROM {table} t JOIN files f ON f.id=t.file_id WHERE t.name LIKE ? ESCAPE '\\' LIMIT ?", (pattern, limit)):
            add(results, layer="native", evidence_type=evidence, value=row[0], source_path=row[1], source_sha256=row[2], status="current_static_evidence", details={})
    db.close()


def query_correlate(path: str, token: str, limit: int, results: list[dict]) -> None:
    db = ro(path)
    pattern = like(token)
    terms = [row[0] for row in db.execute("SELECT value FROM terms WHERE value LIKE ? ESCAPE '\\' LIMIT ?", (pattern, limit))]
    for term in terms:
        for row in db.execute("SELECT value,layer,kind,source_path,source_sha256,status,detail_json FROM evidence WHERE value=? LIMIT ?", (term, limit)):
            add(results, layer=row[1], evidence_type=f"correlate_exact_name:{row[2]}", value=row[0], source_path=row[3], source_sha256=row[4], status=row[5],
                confidence="exact_name_candidate_not_semantic_link", details=json.loads(row[6]))
    db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--kind", choices=("all", "role", "resource", "class", "method", "string", "native", "unity", "java", "correlate"), default="all")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    phase1 = profile["phase1_static"]
    phase2b = profile["phase2b_static"]["outputs"]
    results: list[dict] = []
    broad = args.kind in ("all", "role", "resource")
    if broad or args.kind == "unity":
        query_unity(phase1["outputs"]["unity_index"], args.query, args.limit, results)
    if broad or args.kind in ("java", "class", "method", "string"):
        java_kinds = {"class", "method", "field", "string"} if broad or args.kind == "java" else {args.kind}
        query_java(phase1["outputs"]["java_index"], phase1["inputs"]["jadx_root"], args.query, args.limit, results, java_kinds)
        query_java_fallback(phase2b["java_fallback_index"], args.query, args.limit, results)
    if broad or args.kind in ("native", "string"):
        query_native(phase2b["native_function_index"], args.query, args.limit, results)
        query_native_structure(phase1["outputs"]["native_index"], args.query, args.limit, results)
    if args.kind in ("all", "role", "resource", "correlate"):
        query_correlate(phase1["outputs"]["cross_layer_index"], args.query, args.limit, results)
    payload = {
        "schema_version": 1,
        "status": "completed",
        "query": args.query,
        "query_kind": args.kind,
        "result_count": len(results),
        "results": results,
        "interpretation": "Results are static evidence records. Exact names are candidates, not proof of semantic identity or runtime behavior.",
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8-sig")
    if args.format == "json":
        print(rendered)
    else:
        print(f"query={args.query!r} kind={args.kind} results={len(results)}")
        for result in results:
            print(f"[{result['layer']} | {result['status']}] {result['evidence_type']}: {result['value']}")
            print(f"  source: {result['source_path']}")
            print(f"  sha256: {result['source_sha256']}")
            print(f"  confidence: {result['confidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
