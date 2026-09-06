# Target Profiles

Target profiles isolate application-specific identity, retained capture paths,
capability facts, failure states, and unverified states from generic modules.
The generic `collect`, `apk`, `assets`, `il2cpp`, `native`, `verify`, and
`report` modules do not contain an application's package name or format rule.

`targets\arknights\profile.json` is report-only and references retained,
already-analyzed evidence. `targets\pvz2\profile.json` is deliberately empty:
it has no package name and its CLI route must not read a device, create a
capture, run analysis, or generate a report.

Each active profile owns its own capture reference, report namespace, capability
matrix, hash scope, failure list, and unverified list. Profile reports are
timestamped and never overwrite a previous profile result.
