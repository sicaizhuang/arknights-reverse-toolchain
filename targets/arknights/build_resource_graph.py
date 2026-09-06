#!/usr/bin/env python3
"""Build a bounded static PPtr graph from named roots in an Arknights Bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import UnityPy

from vendor.ark_unpacker_lz4ak import install_unitypy_patch


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def object_name(reader: Any) -> str:
    try:
        return str(reader.peek_name() or "")
    except Exception:
        return ""


def references(value: Any, field: str = "$", result: list[dict] | None = None) -> list[dict]:
    if result is None:
        result = []
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            result.append(
                {
                    "field": field,
                    "file_id": int(value.get("m_FileID", 0)),
                    "path_id": str(value.get("m_PathID", 0)),
                }
            )
        for key, item in value.items():
            references(item, f"{field}.{key}", result)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            references(item, f"{field}[{index}]", result)
    return result


def is_forward_edge(field: str) -> bool:
    # Parent pointers escape the selected root and pull unrelated siblings into the graph.
    return not field.endswith(".m_Father") and ".m_Father." not in field


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root-name", action="append", required=True)
    parser.add_argument("--root-type", default="GameObject")
    parser.add_argument("--max-depth", type=int, default=12)
    args = parser.parse_args()

    source = args.input.resolve()
    output = args.output.resolve()
    if not source.is_file():
        raise SystemExit(f"Input Bundle missing: {source}")
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)
    patch = install_unitypy_patch()
    environment = UnityPy.load(str(source))

    index: dict[str, dict] = {}
    name_index: dict[str, list[str]] = defaultdict(list)
    for reader in environment.objects:
        path_id = str(reader.path_id)
        name = object_name(reader)
        try:
            tree = reader.read_typetree()
            refs = references(tree)
            tree_status = "read"
            tree_error = None
        except Exception as exc:
            refs = []
            tree_status = "failed"
            tree_error = f"{type(exc).__name__}: {exc}"
        index[path_id] = {
            "path_id": path_id,
            "type": reader.type.name,
            "name": name,
            "references": refs,
            "type_tree_status": tree_status,
            "type_tree_error": tree_error,
        }
        if name:
            name_index[name.casefold()].append(path_id)

    requested_roots = []
    missing_roots = []
    for name in args.root_name:
        matches = [
            path_id
            for path_id in name_index.get(name.casefold(), [])
            if not args.root_type or index[path_id]["type"].casefold() == args.root_type.casefold()
        ]
        if not matches:
            missing_roots.append(name)
        for path_id in matches:
            requested_roots.append({"requested_name": name, "path_id": path_id})

    graph_nodes: dict[str, dict] = {}
    graph_edges: list[dict] = []
    reachability: dict[str, set[str]] = defaultdict(set)
    root_summaries = []
    for root in requested_roots:
        root_id = root["path_id"]
        root_label = f"{root['requested_name']}@{root_id}"
        queue = deque([(root_id, 0)])
        visited: set[str] = set()
        while queue:
            current, depth = queue.popleft()
            if current in visited or depth > args.max_depth:
                continue
            visited.add(current)
            reachability[current].add(root_label)
            node = index.get(current)
            if node is None:
                continue
            graph_nodes[current] = {
                key: value for key, value in node.items() if key != "references"
            }
            for ref in node["references"]:
                target = ref["path_id"]
                if target == "0":
                    continue
                edge = {
                    "root": root_label,
                    "from_path_id": current,
                    "to_file_id": ref["file_id"],
                    "to_path_id": target,
                    "field": ref["field"],
                    "depth": depth + 1,
                    "status": "local_resolved" if ref["file_id"] == 0 and target in index else "external_or_unresolved",
                }
                graph_edges.append(edge)
                if ref["file_id"] == 0 and target in index and is_forward_edge(ref["field"]):
                    queue.append((target, depth + 1))

        nodes = [index[path_id] for path_id in visited if path_id in index]
        type_counts = Counter(node["type"] for node in nodes)
        names_by_type: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            if node["name"]:
                names_by_type[node["type"]].append(node["name"])
        root_summaries.append(
            {
                "root": root_label,
                "requested_name": root["requested_name"],
                "path_id": root_id,
                "reachable_node_count": len(nodes),
                "type_counts": dict(sorted(type_counts.items())),
                "names_by_type": {
                    key: sorted(set(values), key=str.casefold)
                    for key, values in sorted(names_by_type.items())
                },
            }
        )

    shared_nodes = [
        {
            "path_id": path_id,
            "type": index[path_id]["type"],
            "name": index[path_id]["name"],
            "roots": sorted(roots),
        }
        for path_id, roots in reachability.items()
        if len(roots) > 1 and path_id in index
    ]
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if requested_roots and not missing_roots else "partial" if requested_roots else "blocked",
        "method": "bounded_static_pptr_and_transform_graph",
        "input": {"path": str(source), "size": source.stat().st_size, "sha256": sha256(source)},
        "unitypy_version": getattr(UnityPy, "__version__", "unknown"),
        "lz4ak_patch": patch,
        "max_depth": args.max_depth,
        "requested_root_names": args.root_name,
        "requested_root_type": args.root_type,
        "missing_root_names": missing_roots,
        "roots": root_summaries,
        "nodes": sorted(graph_nodes.values(), key=lambda node: (node["type"], node["name"].casefold(), node["path_id"])),
        "edges": graph_edges,
        "shared_nodes": sorted(shared_nodes, key=lambda node: (node["type"], node["name"].casefold(), node["path_id"])),
        "limits": [
            "Edges are serialized PPtr evidence, not runtime call edges.",
            "Parent Transform pointers are recorded but not traversed to prevent unrelated sibling expansion.",
            "Animation binding hashes and shader execution are not reconstructed by this graph.",
        ],
    }
    json_path = output / "resource_graph.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig")

    lines = [
        "# Arknights static resource graph",
        "",
        f"- Status: `{report['status']}`",
        f"- Input: `{source}`",
        f"- SHA-256: `{report['input']['sha256']}`",
        f"- Method: `{report['method']}`",
        "",
    ]
    for root in root_summaries:
        lines.extend(
            [
                f"## {root['root']}",
                "",
                f"Reachable nodes: {root['reachable_node_count']}",
                "",
                "| Type | Count |",
                "|---|---:|",
            ]
        )
        lines.extend(f"| {key} | {value} |" for key, value in root["type_counts"].items())
        lines.append("")
        for kind in ("GameObject", "AnimationClip", "ParticleSystem", "ParticleSystemRenderer", "Material", "Texture2D", "Mesh"):
            values = root["names_by_type"].get(kind, [])
            if values:
                lines.append(f"- {kind}: " + ", ".join(f"`{value}`" for value in values))
        lines.append("")
    lines.extend(
        [
            "## Evidence boundary",
            "",
            "The graph proves serialized object references only. It does not prove runtime instantiation or shader output.",
            "",
        ]
    )
    md_path = output / "resource_graph.md"
    md_path.write_text("\n".join(lines), encoding="utf-8-sig")

    manifest_rows = []
    for path in [Path(__file__).resolve(), json_path, md_path]:
        manifest_rows.append({"path": str(path), "size": path.stat().st_size, "sha256": sha256(path)})
    (output / "sha256_manifest.json").write_text(
        json.dumps({"schema_version": 1, "files": manifest_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    (output / "KEY_SHA256SUMS.txt").write_text(
        "".join(f"{row['sha256']}  {row['path']}\n" for row in manifest_rows),
        encoding="utf-8-sig",
    )
    print(json.dumps({"status": report["status"], "roots": len(root_summaries), "nodes": len(graph_nodes), "edges": len(graph_edges)}))
    return 0 if report["status"] == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
