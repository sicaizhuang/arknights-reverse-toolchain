# Format Capability Matrix

## Status vocabulary

| Status | Meaning |
|---|---|
| `supported` | A bounded fixture or target input was actually parsed by the relevant module. |
| `supported_with_limits` | A bounded parser path worked, with a documented coverage limit. |
| `detected_but_adapter_missing` | The format evidence exists but no applicable adapter produced content. |
| `protected_or_unknown` | Data is nonstandard, protected, or otherwise cannot be classified further from permitted static evidence. |
| `failed` | A required bounded command or parser path failed; its logs retain the reason. |
| `not_applicable` | The input has no applicable representation, such as a capture with no split APK. |
| `unresolved` | Static evidence has not established the requested relationship. |
| `unverified` | An inference or old artifact has not been validated for the current target. |

## Generic fixtures and current target

| Format/module | Generic health fixture | Health interpretation | Retained current-client result |
|---|---|---|---|
| APK and binary manifest | `fixtures\apk\minimal-static-fixture.apk` | AAPT2, Apktool, and JADX must actually open the fixture. | Manifest parsed; Java remains `partial` with 111 recorded JADX errors. |
| Split/APKS | Bundletool version command | Launcher verified; no split fixture is bundled. | `not_applicable`: retained capture has base APK only. |
| UnityFS | `fixtures\unity\empty-unityfs-v6.bundle` | UnityPy must load a standard, zero-entry UnityFS v6 file. | Type-4 data detected; object export is zero and remains `detected_but_adapter_missing`. |
| IL2CPP metadata | `fixtures\il2cpp\standard-metadata-header-v29.dat` | Standard sanity magic/version must be identified. | Raw metadata is `detected_nonstandard` / `protected_or_unknown`; no current static dump. |
| ARM64 ELF | `fixtures\native\minimal-arm64-ret.elf` | ELF64 little-endian AArch64 header must be identified. | Current loader relationship is `unresolved`; no runtime address validation. |

The fixture UnityFS deliberately has no Unity objects. It proves only a parser
entry path, not Texture2D, Sprite, TextAsset, AnimationClip, AudioClip, or Mesh
export coverage. Those types become `supported` only after an actual export from
an appropriate, permitted test fixture or target input.

AssetRipper, Il2CppDumper, Cpp2IL, and Ghidra are separate adapters rather than
fixture-health successes merely because their launchers exist. The zero-object
UnityFS and header-only metadata fixture intentionally mark their object export,
full static dump, and automatic-analysis paths `not_applicable` for health.
