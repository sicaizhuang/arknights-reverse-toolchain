#!/usr/bin/env python3
"""Build method-level smali fallbacks for retained JADX issue groups."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


METHOD_RE = re.compile(r"Method not decompiled:\s*(?P<prefix>[^\r\n(]+)\((?P<args>[^)]*)\):(?P<return>[^\"\r\n]+)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_method(group: dict) -> dict | None:
    for marker in group.get("markers", []):
        text = str(marker.get("evidence") or marker.get("message") or "")
        match = METHOD_RE.search(text)
        if not match:
            continue
        prefix = match.group("prefix").strip()
        if "." not in prefix:
            continue
        class_name, method_name = prefix.rsplit(".", 1)
        return {
            "qualified_class": class_name,
            "method_name": method_name,
            "argument_text": match.group("args"),
            "return_text": match.group("return").strip(),
            "jadx_signature": text,
        }
    return None


def method_blocks(text: str, method_name: str) -> list[tuple[int, int, str, str]]:
    lines = text.splitlines()
    results = []
    start = None
    header = None
    pattern = re.compile(r"^\.method\b.*\s" + re.escape(method_name) + r"\(")
    for index, line in enumerate(lines, 1):
        if start is None and pattern.search(line):
            start = index
            header = line
        elif start is not None and line.strip() == ".end method":
            results.append((start, index, header or "", "\n".join(lines[start - 1:index]) + "\n"))
            start = None
            header = None
    return results


def candidate_smali_files(smali_roots: list[Path], java_relative: str, by_outer_leaf: dict[str, list[Path]]) -> list[Path]:
    relative = Path(java_relative.replace("\\", "/"))
    parts = list(relative.parts)
    if parts and parts[0] == "sources":
        parts = parts[1:]
    if not parts:
        return []
    leaf = Path(parts[-1]).stem
    parent = Path(*parts[:-1])
    candidates = []
    for root in smali_roots:
        directory = root / parent
        if directory.is_dir():
            candidates.extend(sorted(directory.glob(leaf + "*.smali")))
    candidates.extend(by_outer_leaf.get(leaf.lower(), []))
    return sorted(set(path.resolve() for path in candidates), key=str)


def explicit_method_aliases(java_path: Path, group: dict, method_name: str) -> list[str]:
    aliases = [method_name]
    if not java_path.is_file():
        return aliases
    lines = java_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, int(group.get("start_line") or 1) - 8)
    end = min(len(lines), int(group.get("end_line") or len(lines)) + 5)
    for line in lines[start:end]:
        match = re.search(r"renamed from:\s*([^,*/\s]+)", line)
        if match and match.group(1) not in aliases:
            aliases.append(match.group(1))
    return aliases


def init_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE issues(
          group_id INTEGER PRIMARY KEY, kind TEXT, status TEXT, java_path TEXT, java_sha256 TEXT,
          start_line INTEGER, end_line INTEGER, qualified_class TEXT, method_name TEXT,
          jadx_signature TEXT, smali_candidate_count INTEGER, smali_method_count INTEGER,
          reason TEXT
        );
        CREATE TABLE smali_methods(
          id INTEGER PRIMARY KEY, group_id INTEGER, source_path TEXT, source_sha256 TEXT,
          start_line INTEGER, end_line INTEGER, method_header TEXT, snippet_path TEXT,
          snippet_sha256 TEXT, match_status TEXT
        );
        CREATE VIRTUAL TABLE search USING fts5(kind, name, qualified_name, content, source_path UNINDEXED, status UNINDEXED);
        """
    )
    return connection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issues", required=True, type=Path)
    parser.add_argument("--jadx-root", required=True, type=Path)
    parser.add_argument("--decoded-root", required=True, type=Path)
    parser.add_argument("--current-apk", required=True, type=Path)
    parser.add_argument("--apk-analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=False)
    snippets_root = args.output / "smali_methods"
    snippets_root.mkdir()
    groups = [json.loads(line) for line in args.issues.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    analysis = json.loads(args.apk_analysis.read_text(encoding="utf-8-sig"))
    current_hash = sha256(args.current_apk)
    recorded_hashes = {str(item.get("sha256", "")).upper() for item in analysis.get("inputs", [])}
    provenance_ok = current_hash in recorded_hashes
    if not provenance_ok:
        raise SystemExit("decoded/JADX provenance does not match current APK SHA-256")

    smali_roots = sorted(path for path in args.decoded_root.glob("smali*") if path.is_dir())
    by_outer_leaf: dict[str, list[Path]] = {}
    for root in smali_roots:
        for path in root.rglob("*.smali"):
            outer = path.stem.split("$", 1)[0].lower()
            by_outer_leaf.setdefault(outer, []).append(path.resolve())
    database_path = args.output / "java_fallback_index.sqlite"
    db = init_db(database_path)
    records = []
    status_counts = Counter()
    method_groups = 0
    method_groups_with_smali = 0
    method_groups_ambiguous = 0

    for group in groups:
        java_path = args.jadx_root / str(group["path"])
        java_exists = java_path.is_file()
        parsed = parse_method(group)
        candidates = candidate_smali_files(smali_roots, str(group["path"]), by_outer_leaf)
        matches = []
        if parsed:
            method_groups += 1
            aliases = explicit_method_aliases(java_path, group, parsed["method_name"])
            for smali_path in candidates:
                text = smali_path.read_text(encoding="utf-8", errors="replace")
                for alias in aliases:
                    for start, end, header, block in method_blocks(text, alias):
                        matches.append((smali_path, start, end, header, block, alias))

        if parsed and matches:
            status = "smali_method_recovered" if len(matches) == 1 else "smali_method_recovered_ambiguous_overloads_or_inner_classes"
            method_groups_with_smali += 1
            if len(matches) > 1:
                method_groups_ambiguous += 1
            reason = "JADX method body failed; one or more retained smali method bodies are available."
        elif parsed:
            status = "explicit_unrecoverable_method_smali_match_missing"
            reason = "JADX body failed and no matching method header was found in provenance-verified smali candidates."
        elif java_exists and candidates:
            status = "java_warning_with_smali_class_evidence"
            reason = "This issue is not a method-body failure; Java source and provenance-verified class smali are retained."
        elif java_exists:
            status = "java_warning_only"
            reason = "This issue is not a method-body failure; retained Java evidence exists but no class smali candidate matched."
        else:
            status = "explicit_unrecoverable_source_missing"
            reason = "The referenced retained Java source is missing."
        status_counts[status] += 1

        java_digest = sha256(java_path) if java_exists else None
        issue_record = {
            "group_id": group["group_id"],
            "kind": group["kind"],
            "status": status,
            "reason": reason,
            "java_path": str(java_path),
            "java_sha256": java_digest,
            "java_lines": [group.get("start_line"), group.get("end_line")],
            "qualified_class": parsed["qualified_class"] if parsed else None,
            "method_name": parsed["method_name"] if parsed else None,
            "jadx_signature": parsed["jadx_signature"] if parsed else None,
            "explicit_method_aliases": explicit_method_aliases(java_path, group, parsed["method_name"]) if parsed else [],
            "smali_candidate_count": len(candidates),
            "smali_method_count": len(matches),
            "smali_methods": [],
        }
        db.execute(
            "INSERT INTO issues VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (group["group_id"], group["kind"], status, str(java_path), java_digest,
             group.get("start_line"), group.get("end_line"), issue_record["qualified_class"],
             issue_record["method_name"], issue_record["jadx_signature"], len(candidates), len(matches), reason),
        )
        search_content = " ".join(str(marker.get("evidence", "")) for marker in group.get("markers", []))
        db.execute("INSERT INTO search VALUES(?,?,?,?,?,?)", (
            "java_issue", issue_record["method_name"] or Path(group["path"]).stem,
            issue_record["qualified_class"] or group["path"], search_content, str(java_path), status,
        ))

        for match_index, (smali_path, start, end, header, block, matched_name) in enumerate(matches, 1):
            snippet_path = snippets_root / f"{int(group['group_id']):04d}_{match_index:02d}_{parsed['method_name'].replace('<','_').replace('>','_')}.smali"
            snippet_path.write_text(
                f"# source: {smali_path}\n# source lines: {start}-{end}\n{block}",
                encoding="utf-8-sig",
            )
            item = {
                "source_path": str(smali_path),
                "source_sha256": sha256(smali_path),
                "start_line": start,
                "end_line": end,
                "method_header": header,
                "matched_method_name": matched_name,
                "snippet_path": str(snippet_path),
                "snippet_sha256": sha256(snippet_path),
                "match_status": "unique" if len(matches) == 1 else "ambiguous_candidate",
            }
            issue_record["smali_methods"].append(item)
            db.execute(
                "INSERT INTO smali_methods(group_id,source_path,source_sha256,start_line,end_line,method_header,snippet_path,snippet_sha256,match_status) VALUES(?,?,?,?,?,?,?,?,?)",
                (group["group_id"], item["source_path"], item["source_sha256"], start, end, header,
                 item["snippet_path"], item["snippet_sha256"], item["match_status"]),
            )
            db.execute("INSERT INTO search VALUES(?,?,?,?,?,?)", (
                "smali_method", parsed["method_name"], parsed["qualified_class"], block,
                str(smali_path), item["match_status"],
            ))
        records.append(issue_record)

    db.commit()
    quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
    db.close()
    records_path = args.output / "java_fallback_issues.jsonl"
    with records_path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_with_explicit_unrecoverable_items" if any(key.startswith("explicit_unrecoverable") for key in status_counts) else "completed",
        "phase": "2B",
        "provenance": {
            "current_apk": str(args.current_apk),
            "current_apk_sha256": current_hash,
            "apk_analysis": str(args.apk_analysis),
            "apk_analysis_sha256": sha256(args.apk_analysis),
            "recorded_hash_match": provenance_ok,
            "jadx_root": str(args.jadx_root),
            "decoded_root": str(args.decoded_root),
            "smali_roots": [str(path) for path in smali_roots],
        },
        "issue_group_count": len(groups),
        "method_failure_group_count": method_groups,
        "method_groups_with_smali_body": method_groups_with_smali,
        "method_groups_with_ambiguous_smali_matches": method_groups_ambiguous,
        "method_groups_without_smali_body": method_groups - method_groups_with_smali,
        "status_counts": dict(status_counts),
        "database": str(database_path),
        "database_quick_check": quick_check,
        "issues_jsonl": str(records_path),
        "limitations": [
            "Smali is a bytecode-level fallback and does not reconstruct the original Java spelling or control structure.",
            "Multiple smali matches are retained as ambiguous rather than selected by guesswork.",
            "No full APK or JADX analysis was rerun.",
        ],
    }
    report_path = args.output / "java_hybrid_recovery.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
