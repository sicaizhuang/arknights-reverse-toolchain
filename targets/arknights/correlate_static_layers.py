#!/usr/bin/env python3
"""Build exact-name static correlations without importing legacy addresses."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


TERM_MIN = 3
TERM_MAX = 512
EVIDENCE_LIMIT_PER_KIND = 25


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def eligible(value: str | None) -> bool:
    return bool(value and TERM_MIN <= len(value) <= TERM_MAX and value == value.strip())


def init_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript(
        """
        CREATE TABLE terms(
          value TEXT PRIMARY KEY COLLATE BINARY,
          unity_count INTEGER NOT NULL DEFAULT 0,
          java_count INTEGER NOT NULL DEFAULT 0,
          il2cpp_legacy_count INTEGER NOT NULL DEFAULT 0,
          native_count INTEGER NOT NULL DEFAULT 0,
          layer_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'unlinked'
        );
        CREATE TABLE il2cpp_legacy_names(
          id INTEGER PRIMARY KEY,
          value TEXT NOT NULL,
          kind TEXT NOT NULL,
          source_line INTEGER NOT NULL,
          namespace TEXT,
          type_name TEXT,
          status TEXT NOT NULL
        );
        CREATE INDEX idx_il2cpp_legacy_value ON il2cpp_legacy_names(value);
        CREATE TABLE linked_terms(value TEXT PRIMARY KEY COLLATE BINARY);
        CREATE TABLE evidence(
          id INTEGER PRIMARY KEY,
          value TEXT NOT NULL,
          layer TEXT NOT NULL,
          kind TEXT NOT NULL,
          source_path TEXT NOT NULL,
          source_sha256 TEXT,
          source_record_id TEXT,
          detail_json TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE INDEX idx_evidence_value ON evidence(value);
        CREATE INDEX idx_evidence_layer ON evidence(layer);
        """
    )
    return db


UPSERT = {
    "unity": "INSERT INTO terms(value,unity_count) VALUES(?,?) ON CONFLICT(value) DO UPDATE SET unity_count=unity_count+excluded.unity_count",
    "java": "INSERT INTO terms(value,java_count) VALUES(?,?) ON CONFLICT(value) DO UPDATE SET java_count=java_count+excluded.java_count",
    "il2cpp": "INSERT INTO terms(value,il2cpp_legacy_count) VALUES(?,?) ON CONFLICT(value) DO UPDATE SET il2cpp_legacy_count=il2cpp_legacy_count+excluded.il2cpp_legacy_count",
    "native": "INSERT INTO terms(value,native_count) VALUES(?,?) ON CONFLICT(value) DO UPDATE SET native_count=native_count+excluded.native_count",
}


def add_grouped(db: sqlite3.Connection, source: sqlite3.Connection, layer: str, query: str) -> tuple[int, int]:
    unique = entities = 0
    batch = []
    for value, count in source.execute(query):
        if not eligible(value):
            continue
        batch.append((value, int(count)))
        unique += 1
        entities += int(count)
        if len(batch) >= 10_000:
            db.executemany(UPSERT[layer], batch)
            batch.clear()
    if batch:
        db.executemany(UPSERT[layer], batch)
    db.commit()
    return unique, entities


TYPE_RE = re.compile(r"^\s*(?:public|private|protected|internal|static|sealed|abstract|partial|readonly|unsafe|\s)*\s*(?:class|struct|interface|enum)\s+([^\s:{]+)")
METHOD_RE = re.compile(r"^\s*(?:public|private|protected|internal)\s+.+?\s+([.$<>A-Za-z_][.$<>`A-Za-z0-9_]*)\s*\(")


def parse_legacy_dump(db: sqlite3.Connection, dump_path: Path) -> dict:
    namespace = ""
    current_type = ""
    counts = Counter()
    term_counts = Counter()
    rows = []
    with dump_path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.startswith("// Namespace:"):
                namespace = line.partition(":")[2].strip()
                continue
            type_match = TYPE_RE.match(line)
            if type_match and "TypeDefIndex:" in line:
                current_type = type_match.group(1)
                values = [(current_type, "type_name")]
                if namespace:
                    values.append((f"{namespace}.{current_type}", "qualified_type_name"))
                for value, kind in values:
                    if eligible(value):
                        rows.append((value, kind, line_number, namespace, current_type, "unverified_legacy_name"))
                        term_counts[value] += 1
                        counts[kind] += 1
            method_match = METHOD_RE.match(line)
            if method_match and line.rstrip().endswith("{ }"):
                value = method_match.group(1)
                if eligible(value):
                    rows.append((value, "method_name", line_number, namespace, current_type, "unverified_legacy_name"))
                    term_counts[value] += 1
                    counts["method_name"] += 1
            if len(rows) >= 10_000:
                db.executemany(
                    "INSERT INTO il2cpp_legacy_names(value,kind,source_line,namespace,type_name,status) VALUES(?,?,?,?,?,?)",
                    rows,
                )
                rows.clear()
    if rows:
        db.executemany(
            "INSERT INTO il2cpp_legacy_names(value,kind,source_line,namespace,type_name,status) VALUES(?,?,?,?,?,?)",
            rows,
        )
    db.executemany(UPSERT["il2cpp"], term_counts.items())
    db.commit()
    return {"record_counts": dict(counts), "record_count": sum(counts.values()), "unique_term_count": len(term_counts)}


def attach(db: sqlite3.Connection, path: Path, alias: str) -> None:
    db.execute(f"ATTACH DATABASE ? AS {alias}", (str(path.resolve()),))


def insert_evidence(db: sqlite3.Connection, sql: str) -> None:
    db.execute(sql)
    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unity-index", type=Path, required=True)
    parser.add_argument("--java-index", type=Path, required=True)
    parser.add_argument("--jadx-root", type=Path, required=True)
    parser.add_argument("--native-index", type=Path, required=True)
    parser.add_argument("--legacy-dump", type=Path, required=True)
    parser.add_argument("--il2cpp-provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    db_path = output / "cross_layer_index.sqlite"
    db = init_db(db_path)

    unity = sqlite3.connect(f"file:{args.unity_index.resolve().as_posix()}?mode=ro", uri=True)
    java = sqlite3.connect(f"file:{args.java_index.resolve().as_posix()}?mode=ro", uri=True)
    native = sqlite3.connect(f"file:{args.native_index.resolve().as_posix()}?mode=ro", uri=True)
    layer_inputs = {}

    queries = [
        ("object_name", "SELECT name,COUNT(*) FROM objects WHERE name IS NOT NULL GROUP BY name"),
        ("object_container", "SELECT container,COUNT(*) FROM objects WHERE container IS NOT NULL GROUP BY container"),
        ("bundle_path", "SELECT relative_path,COUNT(*) FROM bundles GROUP BY relative_path"),
    ]
    layer_inputs["unity"] = {}
    for kind, query in queries:
        unique, entities = add_grouped(db, unity, "unity", query)
        layer_inputs["unity"][kind] = {"unique_terms_seen": unique, "entity_count": entities}

    queries = [
        ("class_name", "SELECT name,COUNT(*) FROM classes GROUP BY name"),
        ("qualified_class_name", "SELECT qualified_name,COUNT(*) FROM classes GROUP BY qualified_name"),
        ("method_name", "SELECT name,COUNT(*) FROM methods GROUP BY name"),
        ("field_name", "SELECT name,COUNT(*) FROM fields GROUP BY name"),
        ("string", "SELECT value,COUNT(*) FROM strings GROUP BY value"),
        ("resource_reference", "SELECT reference,COUNT(*) FROM resource_refs GROUP BY reference"),
    ]
    layer_inputs["java"] = {}
    for kind, query in queries:
        unique, entities = add_grouped(db, java, "java", query)
        layer_inputs["java"][kind] = {"unique_terms_seen": unique, "entity_count": entities}

    layer_inputs["il2cpp_legacy"] = parse_legacy_dump(db, args.legacy_dump)

    queries = [
        ("string", "SELECT value,COUNT(*) FROM strings GROUP BY value"),
        ("symbol", "SELECT name,COUNT(*) FROM symbols GROUP BY name"),
        ("import", "SELECT name,COUNT(*) FROM imports GROUP BY name"),
        ("export", "SELECT name,COUNT(*) FROM exports GROUP BY name"),
    ]
    layer_inputs["native"] = {}
    for kind, query in queries:
        unique, entities = add_grouped(db, native, "native", query)
        layer_inputs["native"][kind] = {"unique_terms_seen": unique, "entity_count": entities}

    unity.close()
    java.close()
    native.close()

    db.execute(
        """
        UPDATE terms SET
          layer_count=(unity_count>0)+(java_count>0)+(il2cpp_legacy_count>0)+(native_count>0),
          status=CASE WHEN (unity_count>0)+(java_count>0)+(il2cpp_legacy_count>0)+(native_count>0)>=2
                      THEN 'exact_name_candidate' ELSE 'unlinked' END
        """
    )
    db.execute("INSERT INTO linked_terms SELECT value FROM terms WHERE layer_count>=2")
    db.commit()

    attach(db, args.unity_index, "unity")
    attach(db, args.java_index, "java")
    attach(db, args.native_index, "native")

    # Evidence is capped per kind. Full counts remain in terms and every unlinked
    # unique term remains represented there.
    insert_evidence(db, f"""
      INSERT INTO evidence(value,layer,kind,source_path,source_sha256,source_record_id,detail_json,status)
      SELECT value,'unity','object_name',source_path,source_sha256,CAST(object_id AS TEXT),detail_json,'parsed'
      FROM (
        SELECT o.name value,b.source_path,b.sha256 source_sha256,o.id object_id,
               json_object('bundle',b.relative_path,'unity_type',o.unity_type,'container',o.container,'path_id',o.path_id) detail_json,
               row_number() OVER(PARTITION BY o.name ORDER BY o.id) rn
        FROM unity.objects o JOIN unity.bundles b ON b.id=o.bundle_id JOIN linked_terms l ON l.value=o.name
      ) WHERE rn<={EVIDENCE_LIMIT_PER_KIND}
    """)
    insert_evidence(db, f"""
      INSERT INTO evidence(value,layer,kind,source_path,source_sha256,source_record_id,detail_json,status)
      SELECT value,'unity','object_container',source_path,source_sha256,CAST(object_id AS TEXT),detail_json,'parsed'
      FROM (
        SELECT o.container value,b.source_path,b.sha256 source_sha256,o.id object_id,
               json_object('bundle',b.relative_path,'unity_type',o.unity_type,'name',o.name,'path_id',o.path_id) detail_json,
               row_number() OVER(PARTITION BY o.container ORDER BY o.id) rn
        FROM unity.objects o JOIN unity.bundles b ON b.id=o.bundle_id JOIN linked_terms l ON l.value=o.container
      ) WHERE rn<={EVIDENCE_LIMIT_PER_KIND}
    """)
    insert_evidence(db, f"""
      INSERT INTO evidence(value,layer,kind,source_path,source_sha256,source_record_id,detail_json,status)
      SELECT relative_path,'unity','bundle_path',source_path,sha256,CAST(id AS TEXT),json_object('parse_status',parse_status,'asset_count',asset_count),'parsed'
      FROM unity.bundles b JOIN linked_terms l ON l.value=b.relative_path
    """)

    jadx_root = str(args.jadx_root.resolve()).replace("'", "''")
    for kind, table, column, detail in (
        ("class_name", "classes", "name", "json_object('qualified_name',x.qualified_name,'line',x.line)"),
        ("qualified_class_name", "classes", "qualified_name", "json_object('name',x.name,'line',x.line)"),
        ("method_name", "methods", "name", "json_object('class_name',x.class_name,'signature',x.signature,'line',x.line,'decompile_status',x.decompile_status)"),
        ("field_name", "fields", "name", "json_object('class_name',x.class_name,'field_type',x.field_type,'line',x.line)"),
        ("string", "strings", "value", "json_object('line',x.line)"),
        ("resource_reference", "resource_refs", "reference", "json_object('line',x.line)"),
    ):
        insert_evidence(db, f"""
          INSERT INTO evidence(value,layer,kind,source_path,source_sha256,source_record_id,detail_json,status)
          SELECT value,'java','{kind}','{jadx_root}' || '\\' || relative_path,file_sha256,CAST(row_id AS TEXT),detail_json,'partial'
          FROM (
            SELECT x.{column} value,f.relative_path,f.sha256 file_sha256,x.id row_id,{detail} detail_json,
                   row_number() OVER(PARTITION BY x.{column} ORDER BY x.id) rn
            FROM java.{table} x JOIN java.files f ON f.id=x.file_id JOIN linked_terms l ON l.value=x.{column}
          ) WHERE rn<={EVIDENCE_LIMIT_PER_KIND}
        """)

    legacy_dump_path = str(args.legacy_dump.resolve()).replace("'", "''")
    legacy_dump_hash = sha256(args.legacy_dump)
    insert_evidence(db, f"""
      INSERT INTO evidence(value,layer,kind,source_path,source_sha256,source_record_id,detail_json,status)
      SELECT value,'il2cpp_legacy',kind,'{legacy_dump_path}','{legacy_dump_hash}',CAST(id AS TEXT),
             json_object('source_line',source_line,'namespace',namespace,'type_name',type_name),'unverified_legacy_name'
      FROM (
        SELECT n.*,row_number() OVER(PARTITION BY value,kind ORDER BY id) rn
        FROM il2cpp_legacy_names n JOIN linked_terms l USING(value)
      ) WHERE rn<={EVIDENCE_LIMIT_PER_KIND}
    """)

    for kind, table, column, detail in (
        ("string", "strings", "value", "json_object('section',x.section,'file_offset',x.file_offset,'virtual_address',x.virtual_address)"),
        ("symbol", "symbols", "name", "json_object('table_name',x.table_name,'section_index',x.section_index,'value',x.value,'symbol_type',x.symbol_type)"),
        ("import", "imports", "name", "json_object('table_name',x.table_name,'symbol_type',x.symbol_type)"),
        ("export", "exports", "name", "json_object('section_index',x.section_index,'value',x.value,'symbol_type',x.symbol_type)"),
    ):
        insert_evidence(db, f"""
          INSERT INTO evidence(value,layer,kind,source_path,source_sha256,source_record_id,detail_json,status)
          SELECT value,'native','{kind}',source_path,file_sha256,CAST(row_id AS TEXT),detail_json,'current_static_structure'
          FROM (
            SELECT x.{column} value,f.path source_path,f.sha256 file_sha256,x.rowid row_id,{detail} detail_json,
                   row_number() OVER(PARTITION BY x.{column} ORDER BY x.rowid) rn
            FROM native.{table} x JOIN native.files f ON f.id=x.file_id JOIN linked_terms l ON l.value=x.{column}
          ) WHERE rn<={EVIDENCE_LIMIT_PER_KIND}
        """)

    layer_distribution = dict(db.execute("SELECT layer_count,COUNT(*) FROM terms GROUP BY layer_count ORDER BY layer_count").fetchall())
    status_counts = dict(db.execute("SELECT status,COUNT(*) FROM terms GROUP BY status").fetchall())
    exact_pattern_counts = {}
    for row in db.execute(
        """
        SELECT (unity_count>0)||(java_count>0)||(il2cpp_legacy_count>0)||(native_count>0),COUNT(*)
        FROM terms WHERE layer_count>=2 GROUP BY 1 ORDER BY 1
        """
    ):
        exact_pattern_counts[row[0]] = row[1]
    evidence_counts = dict(db.execute("SELECT layer||':'||kind,COUNT(*) FROM evidence GROUP BY layer,kind ORDER BY layer,kind").fetchall())

    correlations_path = output / "correlations.jsonl"
    with correlations_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in db.execute(
            "SELECT value,unity_count,java_count,il2cpp_legacy_count,native_count,layer_count,status FROM terms WHERE layer_count>=2 ORDER BY layer_count DESC,value"
        ):
            record = {
                "value": row[0], "unity_count": row[1], "java_count": row[2],
                "il2cpp_legacy_count": row[3], "native_count": row[4],
                "layer_count": row[5], "status": row[6],
                "semantic_relationship_proven": False,
                "runtime_verified": False,
            }
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_with_limits",
        "scope": "Exact BINARY string/name equality across retained static indexes; no fuzzy matching, address reuse, disassembly, or runtime validation.",
        "command": {
            "executable": str(Path(sys.executable).resolve()),
            "arguments": [str(Path(__file__).resolve()), *sys.argv[1:]],
            "exit_code": 0,
        },
        "provenance": {
            "unity_index": {"path": str(args.unity_index.resolve()), "sha256": sha256(args.unity_index)},
            "java_index": {"path": str(args.java_index.resolve()), "sha256": sha256(args.java_index)},
            "native_index": {"path": str(args.native_index.resolve()), "sha256": sha256(args.native_index)},
            "legacy_il2cpp_dump": {"path": str(args.legacy_dump.resolve()), "sha256": legacy_dump_hash, "status": "unverified_legacy_name_only"},
            "il2cpp_provenance": {"path": str(args.il2cpp_provenance.resolve()), "sha256": sha256(args.il2cpp_provenance)},
        },
        "input_counts": layer_inputs,
        "coverage": {
            "unique_term_count": db.execute("SELECT COUNT(*) FROM terms").fetchone()[0],
            "linked_unique_term_count": db.execute("SELECT COUNT(*) FROM linked_terms").fetchone()[0],
            "unlinked_unique_term_count": db.execute("SELECT COUNT(*) FROM terms WHERE status='unlinked'").fetchone()[0],
            "layer_count_distribution": layer_distribution,
            "exact_layer_patterns": exact_pattern_counts,
            "status_counts": status_counts,
            "evidence_sample_counts": evidence_counts,
            "evidence_limit_per_term_per_kind": EVIDENCE_LIMIT_PER_KIND,
        },
        "status_policy": {
            "unity": "parsed object metadata",
            "java": "partial due to JADX exit 1 and aggregate 111 errors",
            "il2cpp_legacy": "unverified legacy names; no address imported",
            "native": "current static section/symbol/string evidence",
            "correlation": "candidate by exact name only; semantic or call relationship not proven",
            "runtime": "unverified",
        },
        "outputs": {"sqlite": str(db_path), "correlations_jsonl": str(correlations_path)},
        "limitations": [
            "Exact names can collide coincidentally and do not prove a dependency or call edge.",
            "Unlinked terms are retained in the terms table with status=unlinked; evidence rows are sampled only for linked terms.",
            "IL2CPP names come from the 2026-08-03 legacy dump because current restored-metadata provenance is blocked.",
            "No old IL2CPP RVA/VA and no runtime address is present in this index.",
        ],
    }
    write_json(output / "cross_layer_summary.json", report)
    db.commit()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
