# Rewind

Rewind restores explicitly selected database cells from a good backup while preserving every other value in today's snapshot. Keep the website copy concrete and easy to understand.

Uploaded databases are inspected only by the fixed trusted worker in gVisor. Generated recovery code executes only in a separate gVisor run. Inspect its output in a fresh trusted run; never trust the candidate's claims or run uploaded SQLite files or generated code in the controller. Expected values and acceptance checks stay outside candidate execution.

Keep the pinned image, runsc runtime, no network, read-only mounts, nonroot user, CPU/memory/PID/time/output limits, and confirmed teardown. Never introduce a host or runc fallback. Cleanup touches only Rewind-owned containers. A result cannot be downloaded unless every acceptance check and all teardown checks pass.

Live recovery uses Vultr Serverless Inference. A full-backup replacement is a clearly labelled reference comparison, never a fallback or a fabricated model response. Keep credentials out of sandboxes, browser code, logs and evidence. Preserve bounded admission, one worker, at most two model attempts per recovery, and capability-scoped workspace access.

The first version accepts two consistent SQLite snapshots, up to 256 KiB each, with the same single ordinary user table, matching schema and one stable integer or text primary key. Reject unsupported files and ambiguous scope. The buyer explicitly selects damaged columns and records; later legitimate edits to those same selected cells would also be replaced. Never write to a production database.

Required checks: unit tests for the recovery contract, actual gVisor integration including rejected whole-backup replacement and containment probes, JavaScript syntax, and the actual browser workflow with one genuine Vultr recovery. Report limitations and observed evidence; do not call this a security audit or claim independent users.
