# Rewind

Rewind investigates a bad import and restores explicitly approved cells from a good version while preserving every other value in today's snapshot. Keep the website copy concrete and easy to understand.

Uploaded SQLite and CSV files are parsed only by the fixed trusted worker in gVisor. Generated diagnostic and recovery code execute only in separate gVisor runs. Inspect recovery output in a fresh trusted run; never trust generated claims or parse uploaded files or execute generated code in the controller. Validate diagnostic proposals against trusted inspections. Expected values and acceptance checks stay outside candidate execution.

Keep the pinned image, runsc runtime, no network, read-only mounts, nonroot user, CPU/memory/PID/time/output limits, and confirmed teardown. Never introduce a host or runc fallback. Cleanup touches only Rewind-owned containers. A result cannot be downloaded unless every acceptance check and all teardown checks pass.

Live diagnosis and recovery use Vultr Serverless Inference. A full-backup replacement is a clearly labelled reference comparison, never a fallback or a fabricated model response. Keep credentials out of sandboxes, browser code, logs and evidence. Preserve bounded admission, one worker, at most two model calls per workspace (one diagnosis plus one repair, or two manual repair attempts), and capability-scoped workspace access. Do not introduce daily trial quotas or a sign-in gate.

The authorized upgrade adds UTF-8 CSV and an optional incident checkpoint to the manual two-snapshot SQLite workflow. Each file remains bounded to 256 KiB, with matching columns, up to 1,000 rows and one reliable unique key. Approval binds all input hashes, precise cell targets and the workspace version. A checkpoint protects observed later edits, including overlaps; snapshots are not a complete history or authenticated provenance. Without a checkpoint, users choose the damaged cells and receive the missing-history explanation. Never write to a production database or evaluate spreadsheet formulas.

Required checks: unit tests for the recovery contract, actual gVisor integration including rejected whole-backup replacement and containment probes, JavaScript syntax, and the actual browser workflow with one genuine Vultr recovery. Report limitations and observed evidence; do not call this a security audit or claim independent users.
