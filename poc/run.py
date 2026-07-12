"""Orchestrates the incremental pipeline build.

Usage:
    python run.py diff        # compute blast radius from metadata changes, write build/blast_radius.json
    python run.py generate    # generate code for every object in the blast radius, then update the hash manifest

Workflow:
    1. python run.py diff
    2. For each object printed, run the select-pipeline-template skill (in Claude Code)
       to produce poc/selections/<object_id>.selection.json
    3. python run.py generate
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build.diff_engine import compute_blast_radius, current_hashes, load_all_metadata, save_manifest
from build.generator import generate

BASE = Path(__file__).resolve().parent
BLAST_RADIUS_PATH = BASE / "build" / "blast_radius.json"


def cmd_diff():
    radius = compute_blast_radius()
    BLAST_RADIUS_PATH.write_text(json.dumps(radius, indent=2) + "\n")
    if not radius:
        print("No changes detected. Nothing to regenerate.")
        return

    objects = load_all_metadata()
    generation_targets = [obj_id for obj_id in radius if "transformation" in objects[obj_id]]
    source_only = [obj_id for obj_id in radius if obj_id not in generation_targets]

    if source_only:
        print("Source objects with changed metadata (no code generation needed):")
        for obj_id in source_only:
            print(f"  - {obj_id}")

    if generation_targets:
        print("Objects requiring (re)generation:")
        for obj_id in generation_targets:
            print(f"  - {obj_id}")
        print(
            "\nFor each object above, run the select-pipeline-template skill "
            "(inside Claude Code) to produce a selection file in selections/, "
            "then run: python run.py generate"
        )
    else:
        print("\nNo objects require code generation. Run 'python run.py generate' "
              "to record the updated hashes.")


def cmd_generate():
    if not BLAST_RADIUS_PATH.exists():
        print("Run 'python run.py diff' first.")
        return
    radius = json.loads(BLAST_RADIUS_PATH.read_text())
    objects = load_all_metadata()
    for obj_id in radius:
        if "transformation" not in objects[obj_id]:
            print(f"Skipped {obj_id} (source object, no generation needed)")
            continue
        out_path = generate(obj_id)
        print(f"Generated {obj_id} -> {out_path}")
    save_manifest(current_hashes())
    print("Hash manifest updated.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("diff", "generate"):
        print(__doc__)
        sys.exit(1)
    {"diff": cmd_diff, "generate": cmd_generate}[sys.argv[1]]()
