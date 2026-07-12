"""Shared run-logging utility used by every generated notebook.

This is deliberately NOT a template and NOT AI-generated: every object's
generated code calls the exact same log_run() function the exact same way,
so there's nothing per-object to decide. It's written once, reviewed once,
and reused everywhere - the same "shared infrastructure" extension pattern
as the rest of this system, just for a cross-cutting concern instead of a
per-object transformation.

This is the runtime/operational metadata layer: it records what actually
happened when a notebook ran (run_id, version, rows processed, errors). It is
distinct from metadata/, which is design-time metadata describing what should
be built. Runtime metadata is append-only and high-volume, so it lives in its
own store (control/run_log.jsonl here; a Delta table in a real Fabric
Lakehouse) rather than being versioned alongside the design-time metadata.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "control" / "run_log.jsonl"


def log_run(
    run_id: str,
    object_id: str,
    version: str,
    started_at: str,
    status: str,
    rows_processed: int | None = None,
    error_message: str | None = None,
) -> None:
    """Appends one JSON line describing a single notebook run. Called once per
    build_<table>() invocation - once on success, once on failure - so every
    run leaves exactly one record, regardless of outcome.

    version is the metadata content hash of the object being built, not a
    manually bumped version number - it's the same hash the diff engine
    already computes, so a run log entry can always be traced back to the
    exact metadata state that produced it.
    """
    entry = {
        "run_id": run_id,
        "object_id": object_id,
        "version": version,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "rows_processed": rows_processed,
        "error_message": error_message,
    }
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
