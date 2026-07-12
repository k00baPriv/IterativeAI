"""Hashes metadata objects, compares against the last-known manifest, and
expands changed/new objects forward through the depends_on graph to find
the blast radius that needs (re)generation."""
import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
METADATA_DIR = BASE / "metadata"
MANIFEST_PATH = BASE / "manifest" / "hash_manifest.json"


def _hash_content(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def current_hashes() -> dict:
    return {obj_id: _hash_content(data) for obj_id, data in load_all_metadata().items()}


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

    changed_or_new = [
        obj_id for obj_id, data in objects.items()
        if manifest.get(obj_id) != _hash_content(data)
    ]

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
