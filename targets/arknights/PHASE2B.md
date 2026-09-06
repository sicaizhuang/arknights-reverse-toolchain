# Arknights Phase 2B Static Recovery

Phase 2B uses only the retained capture and frozen Phase 1 indexes. It does not start ADB, the game, Ghidra, Frida, or runtime observation.

## Commands

```powershell
# Formal single-Bundle export. Dependency CABs are resolved from the frozen deep inventory.
.\scripts\toolchain.ps1 assets -Profile arknights -InputBundle <capture-bundle> -AssetTypes Material

# Re-run the bounded two-Bundle-per-type validation into a new directory.
.\scripts\toolchain.ps1 unity-types-test -Profile arknights -Output <new-directory>

# Build Java method-level smali fallbacks without rerunning full JADX.
.\scripts\toolchain.ps1 recover-java -Profile arknights -Output <new-directory>

# Lift a bounded set of current AArch64 symbol functions into call/return summaries.
.\scripts\toolchain.ps1 recover-native -Profile arknights -MaxFunctionsPerElf 64 -Output <new-directory>

# Read-only search. Use -Json for structured output.
.\scripts\toolchain.ps1 query -Profile arknights -Query kroos -QueryKind role -Limit 10
.\scripts\toolchain.ps1 query -Profile arknights -Query JNI_OnLoad -QueryKind native -Json
```

Unity output is sample-bounded. `parsed` object metadata is not the same as `exported`. Native addresses are static ELF virtual addresses/RVAs and are never reported as runtime addresses. Exact-name cross-layer matches remain candidates, not semantic proof.
