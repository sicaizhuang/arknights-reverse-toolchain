#!/usr/bin/env python3
"""Correct JADX error accounting without rerunning JADX or rewriting its index."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java-index", type=Path, required=True)
    parser.add_argument("--java-analysis", type=Path, required=True)
    parser.add_argument("--jadx-stdout", type=Path, required=True)
    parser.add_argument("--jadx-stderr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)

    original = json.loads(args.java_analysis.read_text(encoding="utf-8-sig"))
    stdout = args.jadx_stdout.read_text(encoding="utf-8-sig", errors="replace")
    stderr = args.jadx_stderr.read_text(encoding="utf-8-sig", errors="replace")
    db = sqlite3.connect(args.java_index)
    rows = db.execute(
        """
        SELECT e.id,f.relative_path,e.line,e.category,e.message,e.evidence
        FROM errors e JOIN files f ON f.id=e.file_id
        ORDER BY f.relative_path,e.line,e.id
        """
    ).fetchall()
    db.close()

    records = [
        {"id": row[0], "path": row[1], "line": row[2], "category": row[3], "message": row[4], "evidence": row[5]}
        for row in rows
    ]
    raw_counts = Counter(row["category"] for row in records)

    unique = []
    duplicate_marker_count = 0
    seen = set()
    for row in records:
        key = (row["path"], row["line"], row["category"], row["message"].casefold())
        if key in seen:
            duplicate_marker_count += 1
            continue
        seen.add(key)
        unique.append(row)

    by_path = defaultdict(list)
    for row in unique:
        by_path[row["path"]].append(row)

    groups = []
    used_comments = set()
    group_id = 0

    def append_group(kind: str, members: list[dict], confidence: str, note: str) -> None:
        nonlocal group_id
        group_id += 1
        groups.append({
            "group_id": group_id,
            "kind": kind,
            "confidence": confidence,
            "path": members[0]["path"],
            "start_line": min(item["line"] for item in members),
            "end_line": max(item["line"] for item in members),
            "markers": [{k: item[k] for k in ("line", "category", "message", "evidence")} for item in members],
            "note": note,
        })

    # JADX commonly emits a diagnostic comment before a later Method-not-decompiled
    # stub. Pair only within the same source file and a bounded 200-line window.
    for path, path_rows in by_path.items():
        comments = [row for row in path_rows if row["category"] == "jadx_error_comment"]
        for method in [row for row in path_rows if row["category"] == "method_not_decompiled"]:
            candidates = [
                row for row in comments
                if row["id"] not in used_comments and 0 <= method["line"] - row["line"] <= 200
            ]
            if candidates:
                comment = max(candidates, key=lambda row: row["line"])
                used_comments.add(comment["id"])
                append_group(
                    "method_decompilation_failure",
                    [comment, method],
                    "confirmed_generated_output",
                    "Two nearby generated-source markers describe one failed method body.",
                )
            else:
                append_group(
                    "method_decompilation_failure",
                    [method],
                    "confirmed_generated_output",
                    "Method-not-decompiled stub had no nearby diagnostic comment.",
                )

    for row in unique:
        if row["category"] == "jadx_error_comment" and row["id"] not in used_comments:
            append_group(
                "decompiler_error_comment",
                [row],
                "confirmed_generated_output",
                "Generated JADX ERROR comment was not paired with a method stub.",
            )
        elif row["category"] == "code_decompiled_incorrectly":
            append_group(
                "incorrect_code_warning",
                [row],
                "confirmed_generated_output",
                "Generated source explicitly warns that the recovered code is incorrect.",
            )

    ambiguous_tokens = [
        row for row in unique if row["category"] == "decode_exception"
    ]
    group_counts = Counter(group["kind"] for group in groups)
    reported_total = original.get("jadx_errors", {}).get("reported_total")
    log_lines = (stdout + "\n" + stderr).splitlines()
    duplicate_log_lines = [line for line in log_lines if "duplicate class" in line.casefold()]
    resource_error_lines = [line for line in log_lines if "resource" in line.casefold() and any(word in line.casefold() for word in ("error", "fail"))]
    warning_lines = [line for line in log_lines if "warning" in line.casefold() or "warn -" in line.casefold()]

    groups_path = output / "jadx_issue_groups.jsonl"
    with groups_path.open("w", encoding="utf-8", newline="\n") as stream:
        for group in groups:
            stream.write(json.dumps(group, ensure_ascii=False, separators=(",", ":")) + "\n")

    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "partial",
        "command": {
            "executable": str(Path(sys.executable).resolve()),
            "arguments": [str(Path(__file__).resolve()), *sys.argv[1:]],
            "exit_code": 0,
        },
        "provenance": {
            "java_index": str(args.java_index.resolve()),
            "java_index_sha256": sha256(args.java_index),
            "original_java_analysis": str(args.java_analysis.resolve()),
            "original_java_analysis_sha256": sha256(args.java_analysis),
            "jadx_stdout": str(args.jadx_stdout.resolve()),
            "jadx_stdout_sha256": sha256(args.jadx_stdout),
            "jadx_stderr": str(args.jadx_stderr.resolve()),
            "jadx_stderr_sha256": sha256(args.jadx_stderr),
            "jadx_exit_code": original.get("provenance", {}).get("jadx_exit_code"),
        },
        "jadx_cli_error_domain": {
            "reported_total": reported_total,
            "individually_logged_errors": 0,
            "classified_individual_errors": 0,
            "unlocalized_reported_errors": reported_total,
            "explanation": "The retained JADX stdout reports only the aggregate count. Generated-source markers are not one-to-one records of these 111 CLI errors and are therefore not forced into that total.",
        },
        "generated_source_marker_domain": {
            "raw_marker_count": len(records),
            "raw_category_counts": dict(raw_counts),
            "normalized_unique_marker_count": len(unique),
            "duplicate_marker_count": duplicate_marker_count,
            "confirmed_issue_group_count": len(groups),
            "confirmed_issue_group_counts": dict(group_counts),
            "pairing_rule": "Same source file; nearest preceding JADX ERROR comment within 200 lines of a Method-not-decompiled marker.",
            "ambiguous_source_token_count": len(ambiguous_tokens),
            "ambiguous_source_tokens": ambiguous_tokens,
            "explanation": "The DecodeException token is ordinary program text in the matched source and is excluded from confirmed JADX issue groups.",
        },
        "separate_log_classification": {
            "duplicate_class_lines": len(duplicate_log_lines),
            "resource_error_lines": len(resource_error_lines),
            "ordinary_warning_lines": len(warning_lines),
        },
        "coverage": original.get("coverage", {}),
        "outputs": {
            "issue_groups_jsonl": str(groups_path),
        },
        "limitations": [
            "JADX returned a non-zero exit code and reported 111 errors; Java recovery remains partial.",
            "The corrected grouping describes evidence embedded in generated source, not a reconstruction of missing per-error CLI logs.",
            "No existing JADX output or Java index was modified.",
        ],
    }
    write_json(output / "jadx_error_classification.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
