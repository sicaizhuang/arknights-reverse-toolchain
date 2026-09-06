# Arknights PC Adapter

This profile is isolated from the Android/MuMu profile. It consumes copied
Windows x64 files only: `GameAssembly.dll`, standard IL2CPP metadata, the game
executable, and Unity version files.

The PC adapter supplies the code-side index and bounded native pseudocode.
It never transfers a PC RVA/VA to Android. Cross-platform joins use exact
logical names, method/type names, tokens, configuration IDs, and asset paths;
every join remains a candidate until stronger evidence exists.

The adapter is offline report-only by default. It does not start ADB, the game,
anti-cheat, a debugger, or runtime instrumentation.

The `code_reference_index.py` route can consume the isolated generated
C# tree and the current PC `dump.cs`. Use `pc-code-index` to build the SQLite
index, then `pc-code-query`, `pc-code-map`, `pc-code-trace`, or `pc-code-verify` from the unified
CLI. `pc-code-surface` checks exact operator-skill class presence before any
cross-version claim. Legacy `Calls/CalledBy` and RVA values remain historical reference; a
current PC RVA is stored separately and no address is transferred to Android.
