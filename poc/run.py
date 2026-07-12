"""Orchestrates the incremental pipeline build.

Usage:
    python run.py diff        # compute blast radius from metadata/template changes, write build/blast_radius.json
    python run.py generate    # generate code for every object in the blast radius, then update the hash manifest
    python run.py graph       # render the dependency graph + blast radius as blast_radius.md (Mermaid)

Workflow:
    1. python run.py diff
    2. python run.py graph   (optional - visualize what diff just found)
    3. For each object diff lists under "requiring the AI skill", run the printed
       /select-pipeline-template command (in a Claude Code session rooted at poc/)
       to produce poc/selections/<object_id>.selection.json. Objects listed under
       "only need regeneration" already have a valid selection - skip the skill.
    4. python run.py generate
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build.diff_engine import (
    build_manifest,
    change_reason,
    changed_or_new_objects,
    compute_blast_radius,
    load_all_metadata,
    load_manifest,
    load_selection,
    only_template_changed,
    save_manifest,
)
from build.generator import generate
from build.visualize import render as render_graph

BASE = Path(__file__).resolve().parent
BLAST_RADIUS_PATH = BASE / "build" / "blast_radius.json"


def cmd_diff():
    radius = compute_blast_radius()
    BLAST_RADIUS_PATH.write_text(json.dumps(radius, indent=2) + "\n")
    if not radius:
        print("No changes detected. Nothing to regenerate.")
        return

    objects = load_all_metadata()
    manifest = load_manifest()
    directly_changed = set(changed_or_new_objects(objects, manifest))
    generation_targets = [obj_id for obj_id in radius if "transformation" in objects[obj_id]]
    source_only = [obj_id for obj_id in radius if obj_id not in generation_targets]

    def reason_for(obj_id: str) -> str:
        # change_reason() only makes sense for objects that are themselves
        # changed/new. Objects pulled in purely via depends_on propagation
        # (their own metadata and template are untouched) get a different label -
        # otherwise this would print the nonsensical "unchanged" right next to a
        # command telling you to regenerate it.
        if obj_id in directly_changed:
            return change_reason(obj_id, objects[obj_id], manifest)
        return "pulled in via depends_on"

    if source_only:
        print("Source objects with changed metadata (no code generation needed):")
        for obj_id in source_only:
            print(f"  - {obj_id}")

    if generation_targets:
        skill_needed = []
        regen_only = []
        for obj_id in generation_targets:
            # Template-only change: metadata is byte-identical to last successful
            # build, so the existing selection (params derived from that metadata)
            # is still valid - just re-render with the new template, no AI needed.
            # Checked directly rather than parsing change_reason()'s text, since
            # that can now report multiple simultaneous reasons.
            if only_template_changed(obj_id, objects[obj_id], manifest) and load_selection(obj_id) is not None:
                regen_only.append(obj_id)
            else:
                skill_needed.append(obj_id)

        if skill_needed:
            print("Objects requiring the AI skill. Run these in a Claude Code "
                  "session rooted at poc/:\n")
            for obj_id in skill_needed:
                print(f"  /select-pipeline-template {obj_id}   # {reason_for(obj_id)}")

        if regen_only:
            print("\nObjects that only need regeneration - template changed, "
                  "existing selection is still valid, no skill call needed:")
            for obj_id in regen_only:
                print(f"  - {obj_id}")

        print("\nThen run: python run.py generate")
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
    save_manifest(build_manifest())
    print("Hash manifest updated.")


def cmd_graph():
    out_path = render_graph()
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    commands = {"diff": cmd_diff, "generate": cmd_generate, "graph": cmd_graph}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(__doc__)
        sys.exit(1)
    commands[sys.argv[1]]()
