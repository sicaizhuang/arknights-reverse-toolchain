#!/usr/bin/env python3
"""Build an evidence-backed, operator-scoped Arknights resource closure.

The closure promotes a resource only when a current client config field, a
serialized component field, an exact object name, or an exact operator path
proves the relationship. Filename similarity is never sufficient.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import UnityPy

from effects.prepare_effect_stage import StageBuilder, external_cab, serialized_files
from vendor.ark_unpacker_lz4ak import install_unitypy_patch


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = ROOT / "targets" / "arknights" / "profile.json"
DEFAULT_CONFIG_PROBE = ROOT / "work" / "operator_excel_probe_20260831_01"
DEFAULT_SCHEMA_ROOT = ROOT / "work" / "third_party_probe_20260831" / "torappu" / "OpenArknightsFBS" / "FBS"
DEFAULT_PYDEPS = ROOT / "work" / "third_party_probe_20260831" / "pydeps"
TABLES = (
    "character_table",
    "skill_table",
    "battle_equip_table",
    "audio_data",
    "skin_table",
    "charword_table",
)
ROOT_RESOURCE_KINDS = {"projectile", "effect", "skill_prefab"}


def exact_static_closure_status(
    *,
    config_failures: Iterable[Any],
    unresolved_resources: Iterable[Any],
    unresolved_pptrs: Iterable[Any],
    payload_export_failures: Iterable[Any],
    copy_mismatches: Iterable[Any],
    negative_pollution: Iterable[Any],
    zero_byte_count: int = 0,
) -> str:
    failures = (
        config_failures,
        unresolved_resources,
        unresolved_pptrs,
        payload_export_failures,
        copy_mismatches,
        negative_pollution,
    )
    return (
        "completed_exact_static_closure"
        if not any(bool(value) for value in failures) and zero_byte_count == 0
        else "partial_exact_static_closure"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def safe_name(value: str) -> str:
    value = value.replace("\\", "_").replace("/", "_")
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", value)
    return value.strip("._") or "unnamed"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # TypeTree strings can contain surrogateescape code points when a binary
    # TextAsset is exposed through Unity's string field. ASCII JSON escaping
    # preserves those code units without dropping the object or failing write.
    path.write_text(json.dumps(json_value(value), ensure_ascii=True, indent=2), encoding="utf-8-sig")


def json_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        result = {
            "$binary_size": len(data),
            "$binary_sha256": hashlib.sha256(data).hexdigest().upper(),
            "$binary_prefix_hex": data[:64].hex().upper(),
        }
        if len(data) <= 4096:
            result["$binary_hex"] = data.hex().upper()
        return result
    if isinstance(value, dict):
        return {str(key): json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def link_or_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        return "copy"


def find_table_payload(config_probe: Path, table_name: str) -> Path:
    matches = sorted(config_probe.glob(f"payloads/**/*{table_name}*.bytes"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {table_name} payload in {config_probe}, observed {len(matches)}")
    return matches[0]


def text_asset_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8", "surrogateescape")
    return bytes(value)


def inventory_table_payload(database: Path, table_name: str) -> tuple[bytes, dict[str, Any]] | None:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT b.relative_path, b.source_path, b.sha256 AS bundle_sha256,
                   o.name, o.container, o.path_id
            FROM objects o JOIN bundles b ON b.id = o.bundle_id
            WHERE lower(o.unity_type) = 'textasset'
              AND lower(o.name) LIKE lower(?)
            ORDER BY lower(b.relative_path), o.path_id
            """,
            (f"{table_name}%",),
        ).fetchall()
    finally:
        connection.close()

    name_pattern = re.compile(rf"^{re.escape(table_name)}[0-9a-f]*$", re.IGNORECASE)
    candidates = []
    for row in rows:
        source = Path(str(row["source_path"]))
        container = str(row["container"] or "").replace("\\", "/").casefold()
        if not source.is_file() or not name_pattern.fullmatch(str(row["name"] or "")):
            continue
        if not container.startswith("dyn/gamedata/excel/") or not container.endswith(".bytes"):
            continue
        environment = UnityPy.load(str(source))
        try:
            reader = next((item for item in environment.objects if str(item.path_id) == str(row["path_id"])), None)
            if reader is None or reader.type.name != "TextAsset":
                continue
            payload = text_asset_bytes(reader.read().m_Script)
        finally:
            environment.files.clear()
        candidates.append((payload, dict(row)))

    if not candidates:
        return None
    by_hash: dict[str, list[tuple[bytes, dict[str, Any]]]] = defaultdict(list)
    for payload, row in candidates:
        by_hash[hashlib.sha256(payload).hexdigest().upper()].append((payload, row))
    if len(by_hash) != 1:
        raise RuntimeError(f"Current inventory has different payloads for {table_name}: {sorted(by_hash)}")
    payload, row = sorted(next(iter(by_hash.values())), key=lambda item: str(item[1]["source_path"]).casefold())[0]
    return payload, {
        "payload_source": "current_unity_inventory_textasset",
        "bundle_relative_path": row["relative_path"],
        "bundle_path": row["source_path"],
        "bundle_sha256": row["bundle_sha256"],
        "text_asset_name": row["name"],
        "text_asset_container": row["container"],
        "text_asset_path_id": str(row["path_id"]),
        "duplicate_same_payload_count": len(candidates),
    }


def load_flatbuffer_table(
    table_name: str,
    config_probe: Path,
    schema_root: Path,
    pydeps: Path,
    inventory_db: Path | None = None,
) -> tuple[Any, dict[str, Any]]:
    if str(pydeps) not in sys.path:
        sys.path.insert(0, str(pydeps))
    ark_fbs = importlib.import_module("ark_fbs")
    schema_path = schema_root / f"{table_name}.fbs"
    if not schema_path.is_file():
        raise FileNotFoundError(schema_path)
    inventory_payload = inventory_table_payload(inventory_db, table_name) if inventory_db else None
    if inventory_payload is None:
        payload_path = find_table_payload(config_probe, table_name)
        raw = payload_path.read_bytes()
        payload_origin = {
            "payload_source": "fallback_preextracted_probe",
            "payload_path": str(payload_path),
        }
    else:
        raw, payload_origin = inventory_payload
    if len(raw) <= 128:
        raise RuntimeError(f"{table_name} payload is too short for the signed header")
    options = ark_fbs.Options(
        natural_utf8=True,
        defaults_json=True,
        output_enum_identifiers=True,
    )
    schema = ark_fbs.Schema.from_fbs_file(str(schema_path), options=options)
    payload = raw[128:]
    decoded = json.loads(schema.binary_to_json(payload, verify=True))
    source = {
        "table": table_name,
        **payload_origin,
        "payload_size": len(raw),
        "payload_sha256": hashlib.sha256(raw).hexdigest().upper(),
        "signed_header_size": 128,
        "signed_header_sha256": hashlib.sha256(raw[:128]).hexdigest().upper(),
        "flatbuffer_size": len(payload),
        "flatbuffer_sha256": hashlib.sha256(payload).hexdigest().upper(),
        "schema_path": str(schema_path),
        "schema_sha256": sha256(schema_path),
        "parser": f"ark-fbs {getattr(ark_fbs, '__version__', 'unknown')}",
        "parser_license": "MIT",
        "schema_reference_boundary": "external_read_only_reference_no_declared_license",
    }
    return decoded, source


def scalar_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from scalar_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from scalar_values(child)
    elif isinstance(value, str):
        yield value


def exact_scalar_present(value: Any, expected: str) -> bool:
    folded = expected.casefold()
    return any(item.casefold() == folded for item in scalar_values(value))


def segment_present(value: str, expected: str) -> bool:
    return expected.casefold() in {item.casefold() for item in value.split(".")}


def exact_mapping_records(value: Any, expected: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if segment_present(str(key), expected) or exact_scalar_present(item, expected)
    }


def extract_operator_config(tables: dict[str, Any], operator_id: str) -> dict[str, Any]:
    characters = tables["character_table"]
    character = characters.get(operator_id) if isinstance(characters, dict) else None
    if not isinstance(character, dict):
        raise KeyError(f"Operator absent from current character_table: {operator_id}")

    skill_ids = [
        str(item.get("skillId"))
        for item in character.get("skills") or []
        if isinstance(item, dict) and item.get("skillId")
    ]
    skill_table = tables["skill_table"] if isinstance(tables["skill_table"], dict) else {}
    skills = {skill_id: skill_table.get(skill_id) for skill_id in skill_ids}
    missing_skills = sorted(skill_id for skill_id, value in skills.items() if value is None)

    equipment_table = tables["battle_equip_table"] if isinstance(tables["battle_equip_table"], dict) else {}
    prefab_codenames = {
        match.group(1).casefold()
        for phase in character.get("phases") or []
        if isinstance(phase, dict)
        for match in [re.fullmatch(r"char_[0-9]+_(.+)", str(phase.get("characterPrefabKey", "")), re.IGNORECASE)]
        if match
    }
    equipment_key_pattern = re.compile(
        rf"^uniequip_[0-9]+_(?:{'|'.join(re.escape(value) for value in sorted(prefab_codenames))})$",
        re.IGNORECASE,
    ) if prefab_codenames else None
    equipment = {
        key: value
        for key, value in equipment_table.items()
        if exact_scalar_present(value, operator_id)
        or (equipment_key_pattern is not None and equipment_key_pattern.fullmatch(str(key)))
    }

    audio = tables["audio_data"] if isinstance(tables["audio_data"], dict) else {}
    sound_banks = [
        item
        for item in audio.get("soundFXBanks") or []
        if isinstance(item, dict) and segment_present(str(item.get("name", "")), operator_id)
    ]
    voice_language = {
        key: value
        for key, value in (audio.get("soundFxVoiceLang") or {}).items()
        if segment_present(str(key), operator_id) or exact_scalar_present(value, operator_id)
    }
    bank_alias = {
        key: value
        for key, value in (audio.get("bankAlias") or {}).items()
        if segment_present(str(key), operator_id)
        or (isinstance(value, str) and segment_present(value, operator_id))
    }
    audio_assets = sorted(
        {
            str(sound.get("asset"))
            for bank in sound_banks
            for sound in bank.get("sounds") or []
            if isinstance(sound, dict) and sound.get("asset")
        },
        key=str.casefold,
    )

    skin_table = tables["skin_table"] if isinstance(tables["skin_table"], dict) else {}
    skins = {
        str(key): value
        for key, value in (skin_table.get("charSkins") or {}).items()
        if isinstance(value, dict) and str(value.get("charId", "")).casefold() == operator_id.casefold()
    }
    skin_asset_ids = sorted(
        {
            str(value[field])
            for value in skins.values()
            for field in ("illustId", "avatarId", "portraitId", "buildingId", "dynIllustId")
            if value.get(field)
        },
        key=str.casefold,
    )

    charword_table = tables["charword_table"] if isinstance(tables["charword_table"], dict) else {}
    char_words = exact_mapping_records(charword_table.get("charWords"), operator_id)
    char_extra_words = exact_mapping_records(charword_table.get("charExtraWords"), operator_id)
    char_voice_languages = exact_mapping_records(charword_table.get("voiceLangDict"), operator_id)
    char_default_languages = exact_mapping_records(charword_table.get("charDefaultTypeDict"), operator_id)
    voice_assets = sorted(
        {
            str(record["voiceAsset"])
            for records in (char_words, char_extra_words)
            for record in records.values()
            if isinstance(record, dict) and record.get("voiceAsset")
        },
        key=str.casefold,
    )

    phases = character.get("phases") or []
    prefab_keys = list(
        dict.fromkeys(
            str(phase.get("characterPrefabKey"))
            for phase in phases
            if isinstance(phase, dict) and phase.get("characterPrefabKey")
        )
    )
    skill_prefab_keys: list[str] = []
    for skill in character.get("skills") or []:
        if isinstance(skill, dict) and skill.get("overridePrefabKey"):
            skill_prefab_keys.append(str(skill["overridePrefabKey"]))
    for value in skills.values():
        if not isinstance(value, dict):
            continue
        for level in value.get("levels") or []:
            if isinstance(level, dict) and level.get("prefabId"):
                skill_prefab_keys.append(str(level["prefabId"]))

    return {
        "operator_id": operator_id,
        "character": character,
        "character_prefab_keys": prefab_keys,
        "skill_ids": skill_ids,
        "skills": skills,
        "missing_skill_ids": missing_skills,
        "skill_prefab_keys": list(dict.fromkeys(skill_prefab_keys)),
        "equipment": equipment,
        "equipment_ids": sorted((str(key) for key in equipment), key=str.casefold),
        "equipment_selection": "exact scalar operator-id or characterPrefabKey codename to exact uniequip key",
        "skins": {
            "records": skins,
            "asset_ids": skin_asset_ids,
        },
        "voice_lines": {
            "char_words": char_words,
            "char_extra_words": char_extra_words,
            "voice_languages": char_voice_languages,
            "default_languages": char_default_languages,
            "asset_paths": voice_assets,
        },
        "audio": {
            "sound_fx_banks": sound_banks,
            "voice_language": voice_language,
            "bank_alias": bank_alias,
            "asset_paths": audio_assets,
        },
        "text_warning": "Localized strings may be mojibake; IDs, enum values, numbers, and structure remain usable.",
    }


def object_name(reader: Any) -> str:
    try:
        return str(reader.peek_name() or "")
    except Exception:
        return ""


def pptrs(value: Any, field: str = "$") -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            try:
                yield {
                    "field": field,
                    "file_id": int(value.get("m_FileID", 0)),
                    "path_id": str(value.get("m_PathID", 0)),
                }
            except (TypeError, ValueError):
                pass
        for key, child in value.items():
            yield from pptrs(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from pptrs(child, f"{field}[{index}]")


def semantic_strings(value: Any, field: str = "$") -> Iterable[dict[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_field = f"{field}.{key}"
            if isinstance(child, str) and child:
                yield {"field": child_field, "field_name": str(key), "value": child}
            else:
                yield from semantic_strings(child, child_field)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_field = f"{field}[{index}]"
            if isinstance(child, str) and child:
                field_name = field.rsplit(".", 1)[-1].split("[", 1)[0]
                yield {"field": child_field, "field_name": field_name, "value": child}
            else:
                yield from semantic_strings(child, child_field)


def forward_field(field: str) -> bool:
    return not field.endswith(".m_Father") and ".m_Father." not in field


class BundleIndex:
    def __init__(self, path: Path):
        self.path = path
        self.environment = UnityPy.load(str(path))
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.names: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for serialized_name, serialized in serialized_files(self.environment):
            objects = getattr(serialized, "objects", {})
            readers = objects.values() if isinstance(objects, dict) else objects
            externals = list(getattr(serialized, "externals", None) or [])
            for reader in readers:
                path_id = str(reader.path_id)
                name = object_name(reader)
                key = (serialized_name, path_id)
                self.records[key] = {
                    "serialized_name": serialized_name,
                    "path_id": path_id,
                    "reader": reader,
                    "externals": externals,
                    "type": reader.type.name,
                    "name": name,
                }
                if name:
                    self.names[(reader.type.name.casefold(), name.casefold())].append(key)

    def named(self, unity_type: str, name: str) -> list[tuple[str, str]]:
        return self.names.get((unity_type.casefold(), name.casefold()), [])

    def by_path_id(self, path_id: str, host_cab: str | None = None) -> list[tuple[str, str]]:
        matches = []
        for key in self.records:
            serialized_name, candidate = key
            if candidate != str(path_id):
                continue
            if host_cab and host_cab.casefold() not in serialized_name.casefold():
                continue
            matches.append(key)
        return matches

    def external(self, serialized_name: str, file_id: int) -> dict[str, Any] | None:
        record = next(
            (value for (name, _), value in self.records.items() if name == serialized_name),
            None,
        )
        if record is None or file_id <= 0 or file_id > len(record["externals"]):
            return None
        item = record["externals"][file_id - 1]
        return {
            "name": str(getattr(item, "name", "")),
            "path": str(getattr(item, "path", "")),
            "cab": external_cab(item),
        }

    def close(self) -> None:
        self.records.clear()
        self.names.clear()
        self.environment.files.clear()


class ExactObjectWalker:
    def __init__(self, profile: dict[str, Any], output: Path, max_depth: int = 24):
        self.output = output
        self.max_depth = max_depth
        self.indices: dict[str, BundleIndex] = {}
        self.parse_sources: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.node_trees: dict[str, Any] = {}
        self.node_readers: dict[str, Any] = {}
        self.edges: list[dict[str, Any]] = []
        self.roots: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.stage_temp = tempfile.TemporaryDirectory(
            prefix="operator_closure_",
            dir=str(ROOT / "work"),
            ignore_cleanup_errors=True,
        )
        self.stage = StageBuilder(profile, Path(self.stage_temp.name))

    def close(self) -> None:
        for index in self.indices.values():
            index.close()
        self.indices.clear()
        self.node_readers.clear()
        self.stage.converted_by_source.clear()
        gc.collect()
        self.stage_temp.cleanup()

    def index(self, parse_path: Path) -> BundleIndex:
        key = str(parse_path.resolve()).casefold()
        if key not in self.indices:
            self.indices[key] = BundleIndex(parse_path)
        return self.indices[key]

    def register_source(
        self,
        parse_path: Path,
        original_source: Path,
        role: str,
        evidence: str,
        cab: str | None = None,
    ) -> None:
        parse_key = str(parse_path.resolve()).casefold()
        self.parse_sources[parse_key] = {
            "original_source": str(original_source.resolve()),
            "role": role,
            "cab": cab,
        }
        source_key = str(original_source.resolve()).casefold()
        row = self.sources.setdefault(
            source_key,
            {
                "source_path": str(original_source.resolve()),
                "size": original_source.stat().st_size,
                "sha256": sha256(original_source),
                "roles": [],
                "evidence": [],
                "cabs": [],
            },
        )
        if role not in row["roles"]:
            row["roles"].append(role)
        if evidence not in row["evidence"]:
            row["evidence"].append(evidence)
        if cab and cab not in row["cabs"]:
            row["cabs"].append(cab)

    def resolve_cab(self, cab: str) -> tuple[Path, str, str] | None:
        match = self.stage.resolve(cab)
        if match is None:
            return None
        converted, info = match
        source = Path(info["source_path"]).resolve()
        self.register_source(converted, source, "external_cab", "serialized_external_pptr", cab)
        return converted, str(source), cab

    def walk_named(
        self,
        source: Path,
        root_names: list[str],
        root_type: str,
        role: str,
        evidence: str,
    ) -> dict[str, Any]:
        source = source.resolve()
        self.register_source(source, source, role, evidence)
        index = self.index(source)
        starts: list[tuple[str, str, str]] = []
        missing = []
        ambiguous = []
        for name in root_names:
            matches = index.named(root_type, name)
            if not matches:
                missing.append(name)
            elif len(matches) > 1:
                ambiguous.append({"name": name, "matches": [f"{a}:{b}" for a, b in matches]})
            else:
                starts.append((matches[0][0], matches[0][1], name))
        return self._walk(source, starts, role, missing, ambiguous)

    def walk_path_ids(
        self,
        source: Path,
        path_ids: list[str],
        role: str,
        evidence: str,
    ) -> dict[str, Any]:
        source = source.resolve()
        self.register_source(source, source, role, evidence)
        index = self.index(source)
        starts: list[tuple[str, str, str]] = []
        missing = []
        ambiguous = []
        for path_id in path_ids:
            matches = index.by_path_id(path_id)
            if not matches:
                missing.append(path_id)
            elif len(matches) > 1:
                ambiguous.append({"path_id": path_id, "matches": [f"{a}:{b}" for a, b in matches]})
            else:
                starts.append((matches[0][0], matches[0][1], path_id))
        return self._walk(source, starts, role, missing, ambiguous)

    def _walk(
        self,
        source: Path,
        starts: list[tuple[str, str, str]],
        role: str,
        missing: list[str],
        ambiguous: list[dict[str, Any]],
    ) -> dict[str, Any]:
        call_id = f"{role}:{len(self.roots)}"
        unresolved_start = len(self.unresolved)
        call_nodes: set[str] = set()
        queue = deque((source, serialized, path_id, 0, label) for serialized, path_id, label in starts)
        visited: set[tuple[str, str, str]] = set()
        root_rows = []
        for serialized, path_id, label in starts:
            root_rows.append({"label": label, "serialized_name": serialized, "path_id": path_id})

        while queue:
            parse_path, serialized_name, path_id, depth, root_label = queue.popleft()
            visit_key = (str(parse_path.resolve()).casefold(), serialized_name, path_id)
            if visit_key in visited:
                continue
            if depth > self.max_depth:
                self.unresolved.append(
                    {
                        "reason": "max_depth_exceeded",
                        "call": call_id,
                        "root": root_label,
                        "parsed_bundle": str(parse_path),
                        "serialized_name": serialized_name,
                        "path_id": path_id,
                        "depth": depth,
                        "max_depth": self.max_depth,
                    }
                )
                continue
            visited.add(visit_key)
            index = self.index(parse_path)
            record = index.records.get((serialized_name, path_id))
            if record is None:
                self.unresolved.append(
                    {"reason": "local_path_id_missing", "parsed_bundle": str(parse_path), "path_id": path_id}
                )
                continue
            parse_meta = self.parse_sources[str(parse_path.resolve()).casefold()]
            original_source = Path(parse_meta["original_source"])
            node_key = f"{sha256(original_source)[:16]}:{serialized_name}:{path_id}"
            try:
                tree = record["reader"].read_typetree()
                tree_status = "read"
                tree_error = None
            except Exception as exc:
                tree = None
                tree_status = "failed"
                tree_error = f"{type(exc).__name__}: {exc}"
                self.unresolved.append(
                    {
                        "reason": "type_tree_read_failed",
                        "call": call_id,
                        "root": root_label,
                        "parsed_bundle": str(parse_path),
                        "serialized_name": serialized_name,
                        "path_id": path_id,
                        "error": tree_error,
                    }
                )
            tree_path = self.output / "objects" / safe_name(role) / f"{safe_name(record['type'])}_{path_id}.json"
            if tree is not None and not tree_path.exists():
                write_json(tree_path, tree)
            row = self.nodes.setdefault(
                node_key,
                {
                    "node_id": node_key,
                    "source_bundle": str(original_source),
                    "source_bundle_sha256": sha256(original_source),
                    "serialized_name": serialized_name,
                    "path_id": path_id,
                    "unity_type": record["type"],
                    "name": record["name"],
                    "type_tree_status": tree_status,
                    "type_tree_error": tree_error,
                    "type_tree_file": str(tree_path) if tree is not None else None,
                    "roles": [],
                    "roots": [],
                },
            )
            if role not in row["roles"]:
                row["roles"].append(role)
            root_ref = f"{call_id}:{root_label}"
            if root_ref not in row["roots"]:
                row["roots"].append(root_ref)
            if tree is not None:
                self.node_trees[node_key] = tree
            self.node_readers[node_key] = record["reader"]
            call_nodes.add(node_key)
            if tree is None:
                continue
            for ref in pptrs(tree):
                if ref["path_id"] == "0":
                    continue
                edge = {
                    "call": call_id,
                    "root": root_label,
                    "from_node_id": node_key,
                    "from_path_id": path_id,
                    "field": ref["field"],
                    "file_id": ref["file_id"],
                    "to_path_id": ref["path_id"],
                    "depth": depth + 1,
                }
                if ref["file_id"] == 0:
                    target = index.records.get((serialized_name, ref["path_id"]))
                    edge["status"] = "local_resolved" if target else "local_missing"
                    self.edges.append(edge)
                    if not target:
                        self.unresolved.append({**edge, "reason": "local_pptr_target_missing"})
                    if target and forward_field(ref["field"]):
                        queue.append((parse_path, serialized_name, ref["path_id"], depth + 1, root_label))
                    continue

                external = index.external(serialized_name, ref["file_id"])
                edge["external"] = external
                if external is None:
                    edge["status"] = "external_index_invalid"
                    self.edges.append(edge)
                    self.unresolved.append(edge)
                    continue
                cab = external.get("cab")
                if not cab:
                    edge["status"] = "external_builtin_or_non_cab"
                    self.edges.append(edge)
                    continue
                match = self.resolve_cab(str(cab))
                if match is None:
                    edge["status"] = "external_cab_unresolved"
                    self.edges.append(edge)
                    self.unresolved.append(edge)
                    continue
                converted, original, resolved_cab = match
                target_index = self.index(converted)
                targets = target_index.by_path_id(ref["path_id"], resolved_cab)
                if not targets:
                    targets = target_index.by_path_id(ref["path_id"])
                if len(targets) != 1:
                    edge["status"] = "external_target_missing" if not targets else "external_target_ambiguous"
                    edge["resolved_source"] = original
                    edge["target_matches"] = [f"{a}:{b}" for a, b in targets]
                    self.edges.append(edge)
                    self.unresolved.append(edge)
                    continue
                edge["status"] = "external_resolved"
                edge["resolved_source"] = original
                edge["resolved_cab"] = resolved_cab
                self.edges.append(edge)
                if forward_field(ref["field"]):
                    queue.append((converted, targets[0][0], targets[0][1], depth + 1, root_label))

        call_unresolved = self.unresolved[unresolved_start:]
        result = {
            "call": call_id,
            "role": role,
            "source": str(source),
            "roots": root_rows,
            "missing_roots": missing,
            "ambiguous_roots": ambiguous,
            "reachable_node_ids": sorted(call_nodes),
            "reachable_node_count": len(call_nodes),
            "unresolved_count": len(call_unresolved),
            "status": "completed"
            if starts and not missing and not ambiguous and not call_unresolved
            else "partial"
            if starts
            else "blocked",
        }
        self.roots.append(result)
        return result

    def semantics(self, node_ids: Iterable[str]) -> list[dict[str, Any]]:
        result = []
        for node_id in node_ids:
            tree = self.node_trees.get(node_id)
            if tree is None:
                continue
            node = self.nodes[node_id]
            for item in semantic_strings(tree):
                field = item["field_name"].casefold()
                kind = None
                if "projectile" in field and ("key" in field or field.endswith("projectiles")):
                    kind = "projectile"
                elif "effect" in field:
                    kind = "effect"
                elif "audio" in field and ("key" in field or "signal" in field or "event" in field):
                    kind = "audio_event"
                elif field in {"buffkey", "templatekey"}:
                    kind = "buff"
                if kind:
                    result.append(
                        {
                            "source_node_id": node_id,
                            "source_bundle": node["source_bundle"],
                            "source_path_id": node["path_id"],
                            "field": item["field"],
                            "kind": kind,
                            "value": item["value"],
                        }
                    )
        unique = {(item["source_node_id"], item["field"], item["kind"], item["value"]): item for item in result}
        return sorted(unique.values(), key=lambda item: (item["kind"], item["value"].casefold(), item["field"]))

    def export_payloads(self) -> list[dict[str, Any]]:
        """Export concrete payloads for reachable resource-bearing Unity objects.

        Other component types already have lossless TypeTree JSON plus their
        source Bundle. They are recorded as metadata_only rather than silently
        pretending a standalone asset file was produced.
        """
        rows = []
        exportable = {"Texture2D", "Sprite", "TextAsset", "AudioClip"}
        for node_id, node in sorted(self.nodes.items()):
            unity_type = str(node["unity_type"])
            row = {
                "node_id": node_id,
                "unity_type": unity_type,
                "name": node["name"],
                "source_bundle": node["source_bundle"],
                "path_id": node["path_id"],
                "roles": node["roles"],
                "status": "metadata_only" if unity_type not in exportable else "pending",
                "files": [],
            }
            if unity_type not in exportable:
                rows.append(row)
                continue
            reader = self.node_readers[node_id]
            base = (
                self.output
                / "assets"
                / safe_name(node["roles"][0] if node["roles"] else "dependency")
                / f"{node_id.split(':', 1)[0]}_{safe_name(unity_type)}_{safe_name(str(node['path_id']))}_{safe_name(str(node['name']))}"
            )
            try:
                data = reader.read()
                payloads: list[tuple[str, bytes]] = []
                if unity_type in {"Texture2D", "Sprite"}:
                    image = data.image
                    base.mkdir(parents=True, exist_ok=True)
                    image_path = base / "image.png"
                    image.save(image_path, format="PNG")
                    payloads.append((image_path.name, image_path.read_bytes()))
                elif unity_type == "TextAsset":
                    payload = text_asset_bytes(data.m_Script)
                    if not payload:
                        raise ValueError("empty TextAsset payload")
                    suffix = Path(str(node["name"])).suffix
                    filename = "payload" + (suffix if suffix else ".bin")
                    payloads.append((filename, payload))
                elif unity_type == "AudioClip":
                    samples = data.samples
                    if not samples:
                        raise ValueError("AudioClip has no decodable samples")
                    payloads.extend((safe_name(str(name)), bytes(payload)) for name, payload in sorted(samples.items()))

                base.mkdir(parents=True, exist_ok=True)
                for filename, payload in payloads:
                    if not payload:
                        raise ValueError(f"empty exported payload: {filename}")
                    destination = base / filename
                    if not destination.exists():
                        destination.write_bytes(payload)
                    row["files"].append(
                        {
                            "path": str(destination),
                            "size": destination.stat().st_size,
                            "sha256": sha256(destination),
                        }
                    )
                row["status"] = "exported" if row["files"] else "failed"
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
        return rows


class InventoryLocator:
    def __init__(self, database: Path):
        self.database = database
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def named(self, name: str, unity_type: str = "GameObject") -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT b.relative_path, b.source_path, b.sha256 AS bundle_sha256,
                   o.name, o.container, o.path_id, o.unity_type
            FROM objects o JOIN bundles b ON b.id = o.bundle_id
            WHERE lower(o.name) = lower(?) AND lower(o.unity_type) = lower(?)
            """,
            (name, unity_type),
        ).fetchall()
        result = [dict(row) for row in rows if Path(str(row["source_path"])).is_file()]
        return sorted(result, key=lambda row: (str(row["relative_path"]).casefold(), str(row["path_id"])))

    def prefab_container(self, name: str) -> list[dict[str, Any]]:
        suffix = f"/{name}.prefab".casefold()
        rows = self.connection.execute(
            """
            SELECT DISTINCT b.relative_path, b.source_path, b.sha256 AS bundle_sha256,
                   o.container
            FROM objects o JOIN bundles b ON b.id = o.bundle_id
            WHERE lower(replace(o.container, '\\', '/')) LIKE ?
            """,
            (f"%{suffix}",),
        ).fetchall()
        result = []
        for row in rows:
            container = str(row["container"] or "").replace("\\", "/").casefold()
            if not container.endswith(suffix) or not Path(str(row["source_path"])).is_file():
                continue
            item = dict(row)
            item.update({"name": name, "path_id": None, "unity_type": "GameObject"})
            result.append(item)
        return sorted(result, key=lambda row: str(row["relative_path"]).casefold())

    def asset(self, asset_path: str, unity_type: str = "AudioClip") -> list[dict[str, Any]]:
        normalized = asset_path.replace("\\", "/").strip("/").casefold()
        name = Path(normalized).name
        rows = self.connection.execute(
            """
            SELECT b.relative_path, b.source_path, b.sha256 AS bundle_sha256,
                   o.name, o.container, o.path_id, o.unity_type
            FROM objects o JOIN bundles b ON b.id = o.bundle_id
            WHERE lower(o.name) = lower(?) AND lower(o.unity_type) = lower(?)
            """,
            (name, unity_type),
        ).fetchall()
        result = []
        for row in rows:
            container = str(row["container"] or "").replace("\\", "/").strip("/").casefold()
            if container.startswith("dyn/"):
                container = container[4:]
            container_without_suffix = container.rsplit(".", 1)[0] if "." in Path(container).name else container
            if container_without_suffix != normalized or not Path(str(row["source_path"])).is_file():
                continue
            result.append(dict(row))
        return sorted(result, key=lambda row: (str(row["relative_path"]).casefold(), str(row["path_id"])))

    def operator_roots(self, operator_id: str) -> list[dict[str, Any]]:
        """Return objects whose name or container contains the exact operator token.

        SQL narrows the scan; the boundary regex prevents char_010_chen from
        absorbing a hypothetical char_010_chen2.
        """
        like = f"%{operator_id}%"
        rows = self.connection.execute(
            """
            SELECT b.relative_path, b.source_path, b.sha256 AS bundle_sha256,
                   o.name, o.container, o.path_id, o.unity_type
            FROM objects o JOIN bundles b ON b.id = o.bundle_id
            WHERE lower(o.name) LIKE lower(?) OR lower(o.container) LIKE lower(?)
            ORDER BY lower(b.relative_path), o.path_id
            """,
            (like, like),
        ).fetchall()
        boundary = re.compile(rf"(?<![a-z0-9]){re.escape(operator_id)}(?![a-z0-9])", re.IGNORECASE)
        result = []
        for row in rows:
            source = Path(str(row["source_path"]))
            name = str(row["name"] or "")
            container = str(row["container"] or "").replace("\\", "/")
            if not source.is_file() or not (boundary.search(name) or boundary.search(container)):
                continue
            result.append(dict(row))
        return result

    def voice_asset_variants(self, operator_id: str, logical_asset: str) -> list[dict[str, Any]]:
        clip_name = Path(logical_asset.replace("\\", "/")).name
        rows = self.connection.execute(
            """
            SELECT b.relative_path, b.source_path, b.sha256 AS bundle_sha256,
                   o.name, o.container, o.path_id, o.unity_type
            FROM objects o JOIN bundles b ON b.id = o.bundle_id
            WHERE lower(o.unity_type) = 'audioclip' AND lower(o.name) = lower(?)
            ORDER BY lower(b.relative_path), o.path_id
            """,
            (clip_name,),
        ).fetchall()
        operator_boundary = re.compile(
            rf"(?<![a-z0-9]){re.escape(operator_id)}(?![a-z0-9])",
            re.IGNORECASE,
        )
        result = []
        for row in rows:
            source = Path(str(row["source_path"]))
            container = str(row["container"] or "").replace("\\", "/")
            if source.is_file() and operator_boundary.search(container):
                result.append(dict(row))
        return result


def allowed_roots(profile: dict[str, Any]) -> list[Path]:
    return [Path(value).resolve() for value in profile["lz4ak_adapter"]["allowed_roots"]]


def exact_path_candidates(roots: list[Path], relative: str) -> list[Path]:
    result = []
    for root in roots:
        path = (root / Path(relative)).resolve()
        if path.is_file() and path not in result:
            result.append(path)
    return result


def verified_named_bundle_candidates(
    roots: list[Path],
    key: str,
    kind: str,
) -> list[dict[str, Any]]:
    """Use a path only as a bounded locator hint, then prove the exact root.

    APK-extracted Bundles are allowed by the profile but are not necessarily in
    the capture-only SQLite inventory.  The basename hint never becomes
    evidence: promotion requires exactly one GameObject named ``key`` after
    opening the candidate Bundle.
    """
    if kind == "effect":
        hint = key.split("_", 1)[0]
        relatives = [f"battle/prefabs/effects/{hint}.ab"]
    elif kind == "projectile":
        relatives = ["battle/prefabs/[uc]projectiles.ab"]
    else:
        return []

    rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for relative in relatives:
        for source in exact_path_candidates(roots, relative):
            source_key = str(source).casefold()
            if source_key in seen_paths:
                continue
            seen_paths.add(source_key)
            index = BundleIndex(source)
            try:
                matches = index.named("GameObject", key)
                if len(matches) != 1:
                    continue
                rows.append(
                    {
                        "relative_path": relative,
                        "source_path": str(source),
                        "bundle_sha256": sha256(source),
                        "name": key,
                        "container": None,
                        "path_id": matches[0][1],
                        "unity_type": "GameObject",
                        "location_evidence": "allowed_root_hint_then_exact_gameobject_verification",
                    }
                )
            finally:
                index.close()
    return sorted(rows, key=lambda row: (row["source_path"].casefold(), str(row["path_id"])))


def direct_operator_bundles(roots: list[Path], operator_id: str) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    exact_relatives = [
        f"chararts/{operator_id}.ab",
        f"charpack/{operator_id}.ab",
        f"skinpack/{operator_id}.ab",
        f"spritepack/{operator_id}.ab",
    ]
    for relative in exact_relatives:
        for path in exact_path_candidates(roots, relative):
            result[str(path).casefold()] = {"path": path, "relative": relative, "rule": "exact_operator_basename"}
    for root in roots:
        audio_root = root / "audio"
        if audio_root.is_dir():
            boundary = re.compile(rf"^{re.escape(operator_id)}(?:[._#-].*)?\.ab$", re.IGNORECASE)
            for path in audio_root.glob(f"**/{operator_id}*.ab"):
                if not boundary.fullmatch(path.name):
                    continue
                result[str(path.resolve()).casefold()] = {
                    "path": path.resolve(),
                    "relative": path.resolve().relative_to(root).as_posix(),
                    "rule": "exact_audio_operator_basename",
                }
        avg_root = root / "avg" / "characters"
        if avg_root.is_dir():
            avg_boundary = re.compile(rf"^(?:avg_)?{re.escape(operator_id)}(?:[._#-].*)?\.ab$", re.IGNORECASE)
            for path in avg_root.glob(f"*{operator_id}*.ab"):
                if not avg_boundary.fullmatch(path.name):
                    continue
                result[str(path.resolve()).casefold()] = {
                    "path": path.resolve(),
                    "relative": path.resolve().relative_to(root).as_posix(),
                    "rule": "exact_avg_operator_prefix",
                }
    return sorted(result.values(), key=lambda row: row["relative"].casefold())


def classify_direct(relative: str) -> str:
    lower = relative.replace("\\", "/").casefold()
    if lower.startswith("bundles/"):
        lower = lower[len("bundles/") :]
    if lower.startswith("audio/"):
        return "voice"
    if lower.startswith("chararts/"):
        return "character_art_and_spine"
    if lower.startswith("charpack/"):
        return "battle_logic"
    if lower.startswith("skinpack/"):
        return "battle_skin"
    if lower.startswith("avg/"):
        return "story_portrait"
    if lower.startswith("spritepack/"):
        return "portrait"
    return "direct"


def unique_source(rows: list[dict[str, Any]], key: str) -> tuple[dict[str, Any] | None, str]:
    if not rows:
        return None, "missing"
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        source = Path(str(row["source_path"]))
        by_hash[sha256(source)].append(row)
    if len(by_hash) > 1:
        return None, "ambiguous_different_bundle_hashes"
    selected = sorted(next(iter(by_hash.values())), key=lambda row: str(row["source_path"]).casefold())[0]
    return selected, "resolved_exact" if len(rows) == 1 else "resolved_duplicate_same_hash"


def copy_exact_sources(output: Path, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = []
    for source in sources:
        path = Path(source["source_path"])
        for role in sorted(source["roles"]):
            destination = output / "bundles" / safe_name(role) / f"{source['sha256'][:16]}_{safe_name(path.name)}"
            mode = link_or_copy(path, destination)
            copied.append(
                {
                    "source_path": str(path),
                    "destination": str(destination),
                    "role": role,
                    "size": destination.stat().st_size,
                    "sha256": sha256(destination),
                    "source_sha256": source["sha256"],
                    "copy_mode": mode,
                    "evidence": source["evidence"],
                    "cabs": source["cabs"],
                }
            )
    return copied


def build_manifest(output: Path) -> dict[str, Any]:
    files = []
    for path in sorted((item for item in output.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold()):
        if path.name in {"sha256_manifest.json", "KEY_SHA256SUMS.txt"}:
            continue
        files.append(
            {
                "relative_path": path.relative_to(output).as_posix(),
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    manifest = {
        "schema_version": 1,
        "file_count": len(files),
        "zero_byte_count": sum(row["size"] == 0 for row in files),
        "files": files,
    }
    write_json(output / "sha256_manifest.json", manifest)
    (output / "KEY_SHA256SUMS.txt").write_text(
        "".join(f"{row['sha256']}  {row['relative_path']}\n" for row in files),
        encoding="utf-8-sig",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--config-probe", type=Path, default=DEFAULT_CONFIG_PROBE)
    parser.add_argument("--schema-root", type=Path, default=DEFAULT_SCHEMA_ROOT)
    parser.add_argument("--ark-fbs-pydeps", type=Path, default=DEFAULT_PYDEPS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-depth", type=int, default=24)
    args = parser.parse_args()

    operator_id = args.operator_id.strip().casefold()
    if not re.fullmatch(r"char_[0-9]+_[a-z0-9_#-]+", operator_id):
        raise SystemExit("operator-id must look like char_501_durin")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing existing output: {output}")
    output.mkdir(parents=True)

    profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    roots = allowed_roots(profile)
    inventory_db = Path(profile["phase1_static"]["outputs"]["unity_index"])
    if not inventory_db.is_file():
        raise FileNotFoundError(inventory_db)
    install_unitypy_patch()

    table_data: dict[str, Any] = {}
    table_sources: dict[str, Any] = {}
    for table in TABLES:
        table_data[table], table_sources[table] = load_flatbuffer_table(
            table,
            args.config_probe.resolve(),
            args.schema_root.resolve(),
            args.ark_fbs_pydeps.resolve(),
            inventory_db,
        )
    config = extract_operator_config(table_data, operator_id)
    write_json(output / "config" / "operator_config.json", config)
    write_json(output / "config" / "table_sources.json", table_sources)

    locator = InventoryLocator(inventory_db)
    walker = ExactObjectWalker(profile, output, args.max_depth)
    resolution_rows: list[dict[str, Any]] = []
    semantic_rows: list[dict[str, Any]] = []
    direct_rows = direct_operator_bundles(roots, operator_id)
    try:
        for item in direct_rows:
            walker.register_source(item["path"], item["path"], classify_direct(item["relative"]), item["rule"])

        charpack_candidates = [item["path"] for item in direct_rows if item["relative"].casefold() == f"charpack/{operator_id}.ab"]
        charpack_hashes = {sha256(path) for path in charpack_candidates}
        if not charpack_candidates:
            charpack_status = "missing"
            charpack = None
        elif len(charpack_hashes) > 1:
            charpack_status = "ambiguous_different_bundle_hashes"
            charpack = None
        else:
            charpack_status = "resolved_exact" if len(charpack_candidates) == 1 else "resolved_duplicate_same_hash"
            charpack = sorted(charpack_candidates, key=lambda path: str(path).casefold())[0]

        char_graph = None
        discovered_node_ids: set[str] = set()
        if charpack is not None:
            char_graph = walker.walk_named(
                charpack,
                config["character_prefab_keys"],
                "GameObject",
                "character_prefab",
                "character_table.phases.characterPrefabKey",
            )
            discovered_node_ids.update(char_graph["reachable_node_ids"])
        resolution_rows.append(
            {
                "kind": "character_prefab",
                "config_field": "character_table.phases[].characterPrefabKey",
                "values": config["character_prefab_keys"],
                "bundle": str(charpack) if charpack else None,
                "status": charpack_status if char_graph is None else char_graph["status"],
            }
        )

        inventory_root_rows = locator.operator_roots(operator_id)
        roots_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in inventory_root_rows:
            roots_by_source[str(row["source_path"]).casefold()].append(row)
        operator_inventory_graphs = []
        for grouped in sorted(roots_by_source.values(), key=lambda rows: str(rows[0]["source_path"]).casefold()):
            source = Path(str(grouped[0]["source_path"]))
            relative = str(grouped[0]["relative_path"])
            role = "operator_exact_" + classify_direct(relative)
            path_ids = sorted({str(row["path_id"]) for row in grouped}, key=lambda value: int(value))
            graph = walker.walk_path_ids(
                source,
                path_ids,
                role,
                "current_inventory_exact_operator_token_in_name_or_container",
            )
            discovered_node_ids.update(graph["reachable_node_ids"])
            operator_inventory_graphs.append(
                {
                    "source": str(source),
                    "relative_path": relative,
                    "root_object_count": len(path_ids),
                    "root_path_ids": path_ids,
                    "unity_types": sorted({str(row["unity_type"]) for row in grouped}),
                    "graph": graph,
                }
            )
            resolution_rows.append(
                {
                    "kind": "operator_exact_object_roots",
                    "value": relative,
                    "source_evidence": "current inventory exact operator token in object name/container",
                    "status": graph["status"],
                    "bundle": str(source),
                    "bundle_sha256": sha256(source),
                    "root_count": len(path_ids),
                    "graph_call": graph["call"],
                }
            )

        for asset_id in config["skins"]["asset_ids"]:
            matches = locator.operator_roots(asset_id)
            direct_matches: list[dict[str, Any]] = []
            direct_graphs: list[dict[str, Any]] = []
            if not matches:
                # The retained APK root is authoritative current-client input but
                # is not guaranteed to have been included in the older SQLite
                # inventory.  Probe only exact operator-named art/skin Bundles
                # and promote only an exact serialized object name.  This is a
                # bounded evidence fallback, not a fuzzy filename match.
                for item in direct_rows:
                    if classify_direct(str(item["relative"])) not in {
                        "character_art_and_spine",
                        "battle_skin",
                        "portrait",
                    }:
                        continue
                    source = Path(item["path"])
                    index = walker.index(source)
                    exact_keys = sorted(
                        key
                        for key, record in index.records.items()
                        if str(record["name"]).casefold() == asset_id.casefold()
                    )
                    for serialized_name, path_id in exact_keys:
                        record = index.records[(serialized_name, path_id)]
                        direct_matches.append(
                            {
                                "relative_path": str(item["relative"]),
                                "source_path": str(source),
                                "bundle_sha256": sha256(source),
                                "name": record["name"],
                                "container": None,
                                "path_id": path_id,
                                "unity_type": record["type"],
                                "location_evidence": "allowed_current_root_exact_direct_bundle_and_object_name",
                            }
                        )
                direct_hashes = {str(row["bundle_sha256"]) for row in direct_matches}
                if len(direct_hashes) == 1:
                    selected_source = Path(
                        sorted(direct_matches, key=lambda row: str(row["source_path"]).casefold())[0]["source_path"]
                    )
                    selected_path_ids = sorted(
                        {
                            str(row["path_id"])
                            for row in direct_matches
                            if Path(str(row["source_path"])).resolve() == selected_source.resolve()
                        },
                        key=int,
                    )
                    graph = walker.walk_path_ids(
                        selected_source,
                        selected_path_ids,
                        "skin_asset",
                        f"skin_table exact asset identifier:{asset_id}",
                    )
                    direct_graphs.append(graph)
                    discovered_node_ids.update(graph["reachable_node_ids"])
                    direct_status = (
                        "resolved_exact_direct_bundle_objects"
                        if graph["status"] == "completed"
                        else graph["status"]
                    )
                elif direct_matches:
                    direct_status = "ambiguous_different_bundle_hashes"
                else:
                    direct_status = "missing"
            else:
                direct_status = "resolved_exact_objects"
            resolution_rows.append(
                {
                    "kind": "skin_asset_id",
                    "value": asset_id,
                    "source_evidence": "skin_table.charSkins exact asset identifier",
                    "status": direct_status,
                    "inventory_matches": matches,
                    "direct_bundle_matches": direct_matches,
                    "direct_bundle_graphs": direct_graphs,
                }
            )

        for equipment_id in config["equipment_ids"]:
            matches = locator.operator_roots(equipment_id)
            equipment_graphs = []
            grouped_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in matches:
                grouped_matches[str(row["source_path"]).casefold()].append(row)
            for grouped in sorted(grouped_matches.values(), key=lambda rows: str(rows[0]["source_path"]).casefold()):
                source = Path(str(grouped[0]["source_path"]))
                path_ids = sorted({str(row["path_id"]) for row in grouped}, key=lambda value: int(value))
                graph = walker.walk_path_ids(
                    source,
                    path_ids,
                    "equipment_asset",
                    f"battle_equip_table exact key:{equipment_id}",
                )
                discovered_node_ids.update(graph["reachable_node_ids"])
                equipment_graphs.append(graph)
            resolution_rows.append(
                {
                    "kind": "equipment_asset",
                    "value": equipment_id,
                    "source_evidence": "battle_equip_table exact equipment key",
                    "status": "completed" if equipment_graphs and all(item["status"] == "completed" for item in equipment_graphs) else "missing" if not equipment_graphs else "partial",
                    "inventory_matches": matches,
                    "graphs": equipment_graphs,
                }
            )

        for voice_asset in config["voice_lines"]["asset_paths"]:
            matches = locator.voice_asset_variants(operator_id, voice_asset)
            resolution_rows.append(
                {
                    "kind": "voice_asset",
                    "value": voice_asset,
                    "source_evidence": "charword_table exact voiceAsset",
                    "status": "resolved_language_variants" if matches else "missing",
                    "inventory_matches": matches,
                    "language_bundle_count": len({str(item["source_path"]).casefold() for item in matches}),
                }
            )

        pending: deque[tuple[str, str, dict[str, Any]]] = deque()
        seen_keys: set[tuple[str, str]] = set()
        for prefab in config["skill_prefab_keys"]:
            pending.append(("skill_prefab", prefab, {"field": "skill_table.levels[].prefabId"}))

        def enqueue_semantics(node_ids: Iterable[str]) -> None:
            for semantic in walker.semantics(node_ids):
                semantic_rows.append(semantic)
                if semantic["kind"] in {"projectile", "effect"}:
                    pending.append((semantic["kind"], semantic["value"], semantic))

        enqueue_semantics(discovered_node_ids)
        while pending:
            kind, key, evidence = pending.popleft()
            identity = (kind, key.casefold())
            if identity in seen_keys:
                continue
            seen_keys.add(identity)
            matches = locator.named(key, "GameObject")
            locator_rule = "exact_object_name"
            if not matches:
                matches = locator.prefab_container(key)
                locator_rule = "exact_prefab_container_suffix"
            if not matches:
                matches = verified_named_bundle_candidates(roots, key, kind)
                locator_rule = "allowed_root_hint_then_exact_gameobject_verification"
            selected, status = unique_source(matches, key)
            row: dict[str, Any] = {
                "kind": kind,
                "value": key,
                "source_evidence": evidence,
                "inventory_matches": matches,
                "locator_rule": locator_rule,
                "status": status,
                "bundle": None,
                "root_path_id": None,
            }
            if selected is not None:
                source = Path(selected["source_path"])
                graph = walker.walk_named(
                    source,
                    [key],
                    "GameObject",
                    kind,
                    f"serialized_field_exact_value:{evidence.get('field', '')}",
                )
                row.update(
                    {
                        "status": graph["status"],
                        "bundle": str(source),
                        "bundle_sha256": sha256(source),
                        "root_path_id": graph["roots"][0]["path_id"] if graph["roots"] else None,
                        "graph_call": graph["call"],
                    }
                )
                enqueue_semantics(graph["reachable_node_ids"])
            resolution_rows.append(row)

        for asset_path in config["audio"]["asset_paths"]:
            matches = locator.asset(asset_path, "AudioClip")
            selected, status = unique_source(matches, asset_path)
            row = {
                "kind": "audio_asset",
                "value": asset_path,
                "source_evidence": "audio_data.soundFXBanks[].sounds[].asset",
                "inventory_matches": matches,
                "status": status,
                "bundle": None,
                "root_path_id": None,
            }
            if selected is not None:
                source = Path(selected["source_path"])
                graph = walker.walk_path_ids(
                    source,
                    [str(selected["path_id"])],
                    "battle_audio",
                    "audio_data_exact_asset_path",
                )
                row.update(
                    {
                        "status": graph["status"],
                        "bundle": str(source),
                        "bundle_sha256": sha256(source),
                        "root_path_id": selected["path_id"],
                        "graph_call": graph["call"],
                    }
                )
            resolution_rows.append(row)

        payload_exports = walker.export_payloads()
        payload_export_failures = [item for item in payload_exports if item["status"] == "failed"]
        source_rows = sorted(walker.sources.values(), key=lambda item: item["source_path"].casefold())
        copied = copy_exact_sources(output, source_rows)
        copy_mismatches = [item for item in copied if item["sha256"] != item["source_sha256"]]
        accepted_statuses = {
            "completed",
            "resolved_exact",
            "resolved_duplicate_same_hash",
            "resolved_exact_objects",
            "resolved_exact_direct_bundle_objects",
            "resolved_language_variants",
        }
        required_resolution_kinds = ROOT_RESOURCE_KINDS | {
            "character_prefab",
            "audio_asset",
            "voice_asset",
            "skin_asset_id",
            "equipment_asset",
            "operator_exact_object_roots",
        }
        unresolved_resources = [
            row
            for row in resolution_rows
            if row["kind"] in required_resolution_kinds and row["status"] not in accepted_statuses
        ]
        config_failures = []
        if config["missing_skill_ids"]:
            config_failures.append({"reason": "skill_table_records_missing", "values": config["missing_skill_ids"]})
        if not config["character_prefab_keys"]:
            config_failures.append({"reason": "character_prefab_key_missing"})
        if not config["skins"]["records"]:
            config_failures.append({"reason": "skin_table_operator_records_missing"})
        if not config["voice_lines"]["char_words"] and not config["voice_lines"]["char_extra_words"]:
            config_failures.append({"reason": "charword_table_operator_records_missing"})
        if not inventory_root_rows:
            config_failures.append({"reason": "current_inventory_operator_roots_missing"})
        fallback_tables = [
            name
            for name, source in table_sources.items()
            if source.get("payload_source") != "current_unity_inventory_textasset"
        ]
        if fallback_tables:
            config_failures.append({"reason": "table_not_sourced_from_current_inventory", "tables": fallback_tables})
        negative_pollution = [
            item
            for item in copied
            if "furni_" in Path(item["destination"]).name.casefold()
            or "shared_projectile_candidates" in item["destination"].casefold()
        ]
        report = {
            "schema_version": 2,
            "created_utc": utc_now(),
            "status": exact_static_closure_status(
                config_failures=config_failures,
                unresolved_resources=unresolved_resources,
                unresolved_pptrs=walker.unresolved,
                payload_export_failures=payload_export_failures,
                copy_mismatches=copy_mismatches,
                negative_pollution=negative_pollution,
            ),
            "operator_id": operator_id,
            "method": "current_config_and_exact_operator_roots_to_serialized_fields_to_recursive_pptr_and_payloads",
            "profile": str(args.profile.resolve()),
            "config": {
                "record": str(output / "config" / "operator_config.json"),
                "table_sources": str(output / "config" / "table_sources.json"),
                "skill_count": len(config["skill_ids"]),
                "equipment_count": len(config["equipment"]),
                "battle_sfx_asset_count": len(config["audio"]["asset_paths"]),
                "skin_count": len(config["skins"]["records"]),
                "voice_line_count": len(config["voice_lines"]["char_words"])
                + len(config["voice_lines"]["char_extra_words"]),
                "voice_asset_count": len(config["voice_lines"]["asset_paths"]),
            },
            "character_prefab": {
                "status": charpack_status,
                "bundle": str(charpack) if charpack else None,
                "graph": char_graph,
            },
            "resolutions": resolution_rows,
            "semantic_field_edges": sorted(
                {(item["source_node_id"], item["field"], item["kind"], item["value"]): item for item in semantic_rows}.values(),
                key=lambda item: (item["kind"], item["value"].casefold(), item["field"]),
            ),
            "object_graph": {
                "roots": walker.roots,
                "nodes": sorted(walker.nodes.values(), key=lambda item: item["node_id"]),
                "edges": walker.edges,
                "unresolved": walker.unresolved,
            },
            "direct_operator_bundles": [
                {**{key: str(value) if isinstance(value, Path) else value for key, value in item.items()}, "sha256": sha256(item["path"])}
                for item in direct_rows
            ],
            "operator_inventory_roots": operator_inventory_graphs,
            "source_bundles": source_rows,
            "copied_bundles": copied,
            "payload_exports": payload_exports,
            "validation": {
                "unresolved_resource_count": len(unresolved_resources),
                "unresolved_pptr_count": len(walker.unresolved),
                "config_failure_count": len(config_failures),
                "copy_hash_mismatch_count": len(copy_mismatches),
                "payload_export_failure_count": len(payload_export_failures),
                "negative_pollution_count": len(negative_pollution),
                "unresolved_resources": unresolved_resources,
                "config_failures": config_failures,
                "copy_hash_mismatches": copy_mismatches,
                "payload_export_failures": payload_export_failures,
                "negative_pollution": negative_pollution,
            },
            "evidence_boundary": [
                "Config and serialized PPtr edges prove static ownership/dependency, not runtime invocation order.",
                "Exact operator path identity proves packaging identity, not that every object in the Bundle is used in battle.",
                "A source Bundle may contain collateral objects; exact object PathIDs identify the promoted roots.",
                "Localized text decoding is not used as an ownership key.",
                "No game, ADB, injection, runtime hook, old RVA, or Unity rendering is used.",
            ],
        }
        report_path = output / "operator_resource_closure.json"
        write_json(report_path, report)
        manifest = build_manifest(output)
        report["validation"]["manifest_file_count"] = manifest["file_count"]
        report["validation"]["zero_byte_count"] = manifest["zero_byte_count"]
        report["status"] = exact_static_closure_status(
            config_failures=config_failures,
            unresolved_resources=unresolved_resources,
            unresolved_pptrs=walker.unresolved,
            payload_export_failures=payload_export_failures,
            copy_mismatches=copy_mismatches,
            negative_pollution=negative_pollution,
            zero_byte_count=manifest["zero_byte_count"],
        )
        write_json(report_path, report)
        manifest = build_manifest(output)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "operator_id": operator_id,
                    "source_bundles": len(source_rows),
                    "objects": len(walker.nodes),
                    "resolutions": len(resolution_rows),
                    "unresolved_resources": len(unresolved_resources),
                    "unresolved_pptrs": len(walker.unresolved),
                    "manifest_files": manifest["file_count"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["status"] == "completed_exact_static_closure" else 3
    finally:
        locator.close()
        walker.close()


if __name__ == "__main__":
    raise SystemExit(main())
