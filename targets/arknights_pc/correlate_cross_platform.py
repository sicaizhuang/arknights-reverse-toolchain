#!/usr/bin/env python3
"""Report-only logical join between the PC code index and Android static candidates."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

COMMON = {"get", "set", "add", "remove", "update", "init", "start", "stop", "data", "value", "name", "item", "list", "array", "empty", "true", "false"}

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))

def pc_terms(dump: Path):
    terms = {}
    namespace = ""
    class_re = re.compile(r"\b(?:class|struct|interface|enum|delegate)\s+([A-Za-z_]\w*)")
    namespace_re = re.compile(r"^namespace\s+([A-Za-z0-9_.]+)")
    method_re = re.compile(r"^\s*(?:public|private|protected|internal|static|virtual|abstract|override|sealed|extern|unsafe|new|async|readonly|partial|\s)+.*?\b([A-Za-z_]\w*)\s*\(")
    for line in dump.open("r", encoding="utf-8-sig", errors="replace"):
        match = namespace_re.search(line)
        if match:
            namespace = match.group(1)
        match = class_re.search(line)
        if match:
            value = match.group(1)
            terms.setdefault(value, set()).add("type:" + (namespace + "." if namespace else "") + value)
        match = method_re.search(line)
        if match:
            value = match.group(1)
            if len(value) >= 3:
                terms.setdefault(value, set()).add("method:" + (namespace + "." if namespace else "") + value)
    return terms

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pc-profile", required=True)
    parser.add_argument("--android-profile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    pc_profile_path = Path(args.pc_profile)
    android_profile_path = Path(args.android_profile)
    pc_profile = load_json(pc_profile_path)
    android_profile = load_json(android_profile_path)
    if pc_profile.get("profile_id") != "arknights_pc":
        raise SystemExit("pc profile must be arknights_pc")
    if android_profile.get("profile_id") != "arknights":
        raise SystemExit("android profile must be arknights")
    pc_report_path = Path(pc_profile["static_evidence"]["feasibility_report"])
    pc_dump_path = Path(pc_profile["static_evidence"]["dump_cs"])
    android_cross_path = Path(pc_profile["cross_platform"]["android_cross_layer_report"])
    for path in (pc_report_path, pc_dump_path, android_cross_path):
        if not path.is_file():
            raise SystemExit(f"missing evidence: {path}")
    pc_report = load_json(pc_report_path)
    android_cross = load_json(android_cross_path)
    android_candidates = {}
    correlations_path = android_cross_path.parent / "correlations.jsonl"
    if correlations_path.is_file():
        for line in correlations_path.open("r", encoding="utf-8-sig", errors="replace"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = str(row.get("value", ""))
            if value and len(value) >= 3 and value.lower() not in COMMON:
                android_candidates[value] = row
    terms = pc_terms(pc_dump_path)
    matches = []
    for value in sorted(set(terms) & set(android_candidates), key=str.lower):
        row = android_candidates[value]
        matches.append({
            "value": value,
            "pc_evidence": sorted(terms[value]),
            "android_evidence": {
                "layer_count": row.get("layer_count"),
                "unity_count": row.get("unity_count"),
                "java_count": row.get("java_count"),
                "legacy_il2cpp_count": row.get("il2cpp_legacy_count"),
                "native_count": row.get("native_count"),
                "status": "exact_name_candidate"
            },
            "address_reuse": False,
            "semantic_relationship_proven": False,
            "runtime_verified": False
        })
        if len(matches) >= args.limit:
            break
    output = Path(args.output)
    if output.exists():
        raise SystemExit(f"refusing existing output: {output}")
    output.mkdir(parents=True)
    matches_path = output / "matches.jsonl"
    with matches_path.open("w", encoding="utf-8", newline="\n") as stream:
        for item in matches:
            stream.write(json.dumps(item, ensure_ascii=True, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "target": "arknights_cross_platform",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_with_limits" if matches else "completed_no_exact_matches",
        "join_policy": "exact logical names only; PC and Android addresses are never joined",
        "pc": {"profile": str(pc_profile_path), "report": str(pc_report_path), "dump_cs": str(pc_dump_path), "metadata_version": pc_report["inputs"]["metadata_version"], "gameassembly_sha256": next(item["sha256"] for item in pc_report["inputs"]["files"] if item["relative_path"] == "GameAssembly.dll")},
        "android": {"profile": str(android_profile_path), "cross_layer_report": str(android_cross_path), "cross_layer_status": android_cross.get("status"), "runtime_verified": False},
        "counts": {"pc_terms": len(terms), "android_candidate_terms": len(android_candidates), "exact_name_matches": len(matches)},
        "matches": str(matches_path),
        "limitations": ["Name equality is not proof of a resource dependency, call edge, or behavior.", "Android legacy IL2CPP names remain unverified.", "No RVA/VA is transferred between platforms.", "No ADB, game execution, process memory, or runtime observation was used."]
    }
    report_path = output / "cross_platform_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    hash_records = []
    for path in (pc_profile_path, android_profile_path, pc_report_path, pc_dump_path, android_cross_path, correlations_path, report_path, matches_path):
        if path.is_file():
            hash_records.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
    (output / "sha256_manifest.json").write_text(json.dumps({"schema_version": 1, "algorithm": "SHA-256", "records": hash_records}, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    (output / "KEY_SHA256SUMS.txt").write_text("\n".join(f"{row['sha256']} *{row['path']}" for row in hash_records) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"], "exact_name_matches": len(matches), "hash_records": len(hash_records)}, ensure_ascii=True))

if __name__ == "__main__":
    main()
