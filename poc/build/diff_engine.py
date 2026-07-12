"""Hashes metadata objects and the template each one was last built with,
compares both against the manifest, and expands changed/new objects forward
through the depends_on graph to find the blast radius that needs
(re)generation.

Two independent things can make an object stale:
  - its own metadata changed (or something it depends_on changed - handled by
    the blast-radius expansion), or
  - the template it was built from changed (e.g. a try/except added to
    cast_and_dedupe.py.tmpl) - metadata never moved, but the generated code
    is no longer what the template would now produce.
Both are tracked the same way: as a hash stored in the manifest, compared
against the current hash on every diff.
"""
import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
METADATA_DIR = BASE / "metadata"
TEMPLATES_DIR = BASE / "templates"
SELECTIONS_DIR = BASE / "selections"
MANIFEST_PATH = BASE / "manifest" / "hash_manifest.json"


def hash_object(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_all_metadata() -> dict:
    objects = {}
    for path in METADATA_DIR.rglob("*.json"):
        data = json.loads(path.read_text())
        objects[data["object_id"]] = data
    return objects


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def load_selection(object_id: str) -> dict | None:
    path = SELECTIONS_DIR / f"{object_id}.selection.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def current_template_state(object_id: str) -> tuple[str | None, str | None]:
    """(template_name, template_hash) for the template object_id's current
    selection points at, or (None, None) if it has no selection yet (source
    object, or never built)."""
    selection = load_selection(object_id)
    if not selection:
        return None, None
    template_name = selection["template"]
    template_path = TEMPLATES_DIR / f"{template_name}.py.tmpl"
    if not template_path.exists():
        return template_name, None
    return template_name, hash_file(template_path)


def build_manifest() -> dict:
    """Snapshot of current metadata + template state, in the shape saved to
    hash_manifest.json after a successful generate."""
    manifest = {}
    for obj_id, data in load_all_metadata().items():
        entry = {"metadata_hash": hash_object(data)}
        template_name, template_hash = current_template_state(obj_id)
        if template_name is not None:
            entry["template"] = template_name
            entry["template_hash"] = template_hash
        manifest[obj_id] = entry
    return manifest


def _manifest_entry(obj_id: str, manifest: dict) -> dict | None:
    """Manifest entries are {"metadata_hash": ..., ...} dicts. Older manifests
    (pre template-tracking) stored a bare hash string per object - treat those
    as "no record" so they're picked up as changed and rewritten in the new
    shape on the next generate, instead of crashing on .get()."""
    entry = manifest.get(obj_id)
    return entry if isinstance(entry, dict) else None


def change_reason(obj_id: str, data: dict, manifest: dict) -> str:
    """Human-readable reason a single object is considered changed/new.
    Assumes the caller already knows obj_id is in changed_or_new_objects()."""
    entry = _manifest_entry(obj_id, manifest)
    if entry is None:
        return "new"
    if entry.get("metadata_hash") != hash_object(data):
        return "metadata changed"
    template_name, template_hash = current_template_state(obj_id)
    if template_name is not None and entry.get("template_hash") != template_hash:
        return f"template '{template_name}' changed"
    return "unchanged"


def changed_or_new_objects(objects: dict, manifest: dict) -> list[str]:
    changed = []
    for obj_id, data in objects.items():
        entry = _manifest_entry(obj_id, manifest)
        if entry is None or entry.get("metadata_hash") != hash_object(data):
            changed.append(obj_id)
            continue
        template_name, template_hash = current_template_state(obj_id)
        if template_name is not None and entry.get("template_hash") != template_hash:
            changed.append(obj_id)
    return changed


def build_reverse_graph(objects: dict) -> dict:
    """Maps object_id -> list of objects that depend on it."""
    reverse = {obj_id: [] for obj_id in objects}
    for obj_id, data in objects.items():
        for dep in data.get("depends_on", []):
            reverse.setdefault(dep, []).append(obj_id)
    return reverse


def compute_blast_radius() -> list[str]:
    objects = load_all_metadata()
    manifest = load_manifest()
    changed_or_new = changed_or_new_objects(objects, manifest)

    reverse_graph = build_reverse_graph(objects)
    blast_radius = set(changed_or_new)
    frontier = list(changed_or_new)
    while frontier:
        current = frontier.pop()
        for dependent in reverse_graph.get(current, []):
            if dependent not in blast_radius:
                blast_radius.add(dependent)
                frontier.append(dependent)

    # deterministic order: iterate objects in their natural (upstream-first) order
    return [obj_id for obj_id in objects if obj_id in blast_radius]


if __name__ == "__main__":
    print(json.dumps(compute_blast_radius(), indent=2))
