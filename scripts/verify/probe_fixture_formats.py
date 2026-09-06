#!/usr/bin/env python3
"""Run bounded, target-independent format fixtures and emit toolchain health."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def run(name: str, command: list[str], output: Path, timeout: int = 90, env: dict[str, str] | None = None) -> dict:
    started = time.monotonic()
    stdout_file = output / "logs" / f"{name}.stdout.txt"
    stderr_file = output / "logs" / f"{name}.stderr.txt"
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env, check=False)
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        timed_out = False
        error = None
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        timed_out = True
        error = f"timed out after {timeout} seconds"
    except OSError as exc:
        exit_code = None
        stdout = ""
        stderr = ""
        timed_out = False
        error = str(exc)
    write_text(stdout_file, stdout)
    write_text(stderr_file, stderr)
    return {
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "launch_error": error,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout_path": str(stdout_file),
        "stderr_path": str(stderr_file),
    }


def parse_elf(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return {"recognized": False, "reason": "missing ELF magic"}
    if data[4] != 2 or data[5] != 1:
        return {"recognized": False, "reason": "not ELF64 little endian"}
    machine = struct.unpack_from("<H", data, 18)[0]
    return {
        "recognized": machine == 183,
        "elf_class": data[4],
        "endianness": "little",
        "machine": machine,
        "machine_name": "AArch64" if machine == 183 else "unknown",
        "entry": hex(struct.unpack_from("<Q", data, 24)[0]),
    }


def parse_metadata(path: Path) -> dict:
    data = path.read_bytes()
    magic, version = struct.unpack_from("<II", data, 0) if len(data) >= 8 else (None, None)
    return {
        "recognized": magic == 0xFAB11BAF and version is not None and 20 <= version <= 40,
        "magic": f"0x{magic:08X}" if magic is not None else None,
        "version": version,
        "length": len(data),
        "reason": None if magic == 0xFAB11BAF else "missing standard IL2CPP sanity magic",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aapt2", required=True, type=Path)
    parser.add_argument("--java", required=True, type=Path)
    parser.add_argument("--apktool", required=True, type=Path)
    parser.add_argument("--jadx", required=True, type=Path)
    parser.add_argument("--bundletool", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--unitypy-python", required=True, type=Path)
    args = parser.parse_args()
    fixtures = args.fixtures.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    apk = fixtures / "apk" / "minimal-static-fixture.apk"
    unity = fixtures / "unity" / "empty-unityfs-v6.bundle"
    metadata = fixtures / "il2cpp" / "standard-metadata-header-v29.dat"
    elf = fixtures / "native" / "minimal-arm64-ret.elf"
    inputs = {name: {"path": str(path), "sha256": sha256(path), "size": path.stat().st_size} for name, path in {
        "apk": apk, "unityfs": unity, "il2cpp_metadata_header": metadata, "arm64_elf": elf,
    }.items()}
    commands: list[dict] = []
    commands.append(run("aapt2_badging", [str(args.aapt2), "dump", "badging", str(apk)], output))
    apktool_out = output / "apktool_decode"
    commands.append(run("apktool_decode", [str(args.java), "-jar", str(args.apktool), "d", "-f", str(apk), "-o", str(apktool_out)], output))
    jadx_out = output / "jadx_output"
    env = os.environ.copy()
    commands.append(run("jadx_decompile", [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", str(args.jadx), "-d", str(jadx_out), str(apk)], output, env=env))
    commands.append(run("bundletool_version", [str(args.java), "-jar", str(args.bundletool), "version"], output))
    unity_code = (
        "import json, UnityPy, sys; "
        "env=UnityPy.load(sys.argv[1]); "
        "print(json.dumps({'objects': len(list(env.objects)), 'containers': len(env.container)}))"
    )
    commands.append(run("unitypy_parse", [str(args.unitypy_python), "-c", unity_code, str(unity)], output))

    index = {entry["name"]: entry for entry in commands}
    aapt_ok = index["aapt2_badging"]["exit_code"] == 0 and "package: name='org.example.staticfixture'" in Path(index["aapt2_badging"]["stdout_path"]).read_text(encoding="utf-8")
    apktool_ok = index["apktool_decode"]["exit_code"] == 0 and (apktool_out / "AndroidManifest.xml").is_file()
    jadx_ok = index["jadx_decompile"]["exit_code"] == 0 and jadx_out.is_dir()
    bundletool_ok = index["bundletool_version"]["exit_code"] == 0
    unity_ok = index["unitypy_parse"]["exit_code"] == 0
    metadata_result = parse_metadata(metadata)
    elf_result = parse_elf(elf)
    checks = [
        {"module": "apk", "format": "APK/AndroidManifest", "status": "supported" if aapt_ok and apktool_ok and jadx_ok else "failed", "evidence": {"aapt2": index["aapt2_badging"], "apktool": index["apktool_decode"], "jadx": index["jadx_decompile"]}},
        {"module": "apk", "format": "APK splits/APKS", "status": "supported_with_limits" if bundletool_ok else "failed", "evidence": {"bundletool": index["bundletool_version"], "limit": "No split/APKS fixture is bundled; the launcher and version command are the bounded health check."}},
        {"module": "assets", "format": "UnityFS", "status": "supported" if unity_ok else "failed", "evidence": {"unitypy": index["unitypy_parse"], "fixture_kind": "zero-entry uncompressed UnityFS v6"}},
        {"module": "il2cpp", "format": "standard metadata header", "status": "supported" if metadata_result["recognized"] else "failed", "evidence": metadata_result},
        {"module": "native", "format": "ARM64 ELF", "status": "supported" if elf_result["recognized"] else "failed", "evidence": elf_result},
    ]
    passed = all(item["status"] in {"supported", "supported_with_limits"} for item in checks)
    report = {
        "schema_version": 1,
        "scope": "toolchain_health",
        "target_client_data_used": False,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "inputs": inputs,
        "checks": checks,
        "commands": commands,
        "limits": [
            "Fixtures validate bounded parser and tool-launch paths only.",
            "A successful fixture does not establish support for proprietary, encrypted, or version-specific target data.",
            "AssetRipper is documented as an optional adapter and is not claimed as exercised by this health run.",
        ],
    }
    (output / "toolchain_health.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 7


if __name__ == "__main__":
    raise SystemExit(main())
