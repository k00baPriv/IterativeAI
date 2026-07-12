"""Deterministic, LLM-free step: turns a template selection (produced by the
select-pipeline-template skill) into generated pipeline code. No reasoning
happens here - only lookup, substitution, and schema validation.

Each template has its own params shape, so each gets its own "context builder":
a function that validates the selection against the metadata it actually
touches and returns the dict of values to substitute into that template. This
is the extension point for a new template - add a builder function, register
it in TEMPLATE_CONTEXT_BUILDERS, done. Nothing about diff_engine, run.py, or
the skill's own instructions needs to change for a new template shape."""
import json
import sys
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build.diff_engine import hash_object

BASE = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE / "templates"
SELECTIONS_DIR = BASE / "selections"
METADATA_DIR = BASE / "metadata"
GENERATED_DIR = BASE / "generated"

_AGG_FUNCS = {"sum": "F.sum", "avg": "F.avg", "min": "F.min", "max": "F.max", "count": "F.count"}


def _load_metadata(object_id: str) -> dict:
    layer, name = object_id.split(".", 1)
    path = METADATA_DIR / layer / f"{name}.json"
    return json.loads(path.read_text())


def _load_selection(object_id: str) -> dict:
    path = SELECTIONS_DIR / f"{object_id}.selection.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No selection found for {object_id}. Run the select-pipeline-template "
            f"skill for this object first (it writes {path})."
        )
    return json.loads(path.read_text())


def _target_cols(target_metadata: dict) -> set:
    return {c["name"] for c in target_metadata["columns"]}


def _select_cols_repr(target_metadata: dict) -> str:
    # Final output columns always come straight from the target's own declared
    # schema, never from the AI selection - there's nothing ambiguous about "what
    # columns does this object have," so it isn't left for the skill to guess.
    return ", ".join(f'"{c["name"]}"' for c in target_metadata["columns"])


def _cast_and_dedupe_context(object_id: str, target_metadata: dict, selection: dict) -> dict:
    params = selection["params"]
    source_metadata = _load_metadata(params["source_table"])
    source_cols = {c["name"] for c in source_metadata["columns"]}
    target_cols = _target_cols(target_metadata)

    unknown_source_cols = set(params.get("cast_map", {})) - source_cols
    if unknown_source_cols:
        raise ValueError(
            f"Selection casts columns not present in {source_metadata['object_id']}: "
            f"{unknown_source_cols}"
        )

    unknown_key_cols = set(params.get("key_cols", [])) - target_cols
    if unknown_key_cols:
        raise ValueError(
            f"Selection key_cols not present in {target_metadata['object_id']}: "
            f"{unknown_key_cols}"
        )

    # cast_and_dedupe is pure pass-through + cast + dedupe: every declared target
    # column must exist upstream with the same name, or it can never be produced.
    unreachable_target_cols = target_cols - source_cols
    if unreachable_target_cols:
        raise ValueError(
            f"Target {target_metadata['object_id']} declares columns not present in "
            f"source {source_metadata['object_id']} and not derivable by this "
            f"template: {unreachable_target_cols}"
        )

    cast_lines = "\n".join(
        f'        df = df.withColumn("{col}", F.col("{col}").cast("{cast_type}"))'
        for col, cast_type in params.get("cast_map", {}).items()
    )
    key_cols_repr = ", ".join(f'"{c}"' for c in params["key_cols"])

    return {
        "source_table": params["source_table"],
        "cast_lines": cast_lines,
        "key_cols_repr": key_cols_repr,
        "order_col": params["order_col"],
        "select_cols_repr": _select_cols_repr(target_metadata),
    }


def _aggregate_context(object_id: str, target_metadata: dict, selection: dict) -> dict:
    params = selection["params"]
    source_metadata = _load_metadata(params["source_table"])
    source_cols = {c["name"] for c in source_metadata["columns"]}
    target_cols = _target_cols(target_metadata)
    group_by_cols = params.get("group_by_cols", [])
    agg_map = params.get("agg_map", {})

    unknown_group_source_cols = set(group_by_cols) - source_cols
    if unknown_group_source_cols:
        raise ValueError(
            f"Selection group_by_cols not present in {source_metadata['object_id']}: "
            f"{unknown_group_source_cols}"
        )
    unknown_group_target_cols = set(group_by_cols) - target_cols
    if unknown_group_target_cols:
        raise ValueError(
            f"Selection group_by_cols not present in target {target_metadata['object_id']}: "
            f"{unknown_group_target_cols}"
        )

    unknown_output_cols = set(agg_map) - target_cols
    if unknown_output_cols:
        raise ValueError(
            f"agg_map declares output columns not present in target "
            f"{target_metadata['object_id']}: {unknown_output_cols}"
        )
    for out_col, spec in agg_map.items():
        func = spec.get("func")
        if func not in _AGG_FUNCS:
            raise ValueError(f"agg_map['{out_col}'] uses unsupported func {func!r}")
        source_col = spec.get("source_col")
        if func == "count":
            continue
        if source_col is None or source_col not in source_cols:
            raise ValueError(
                f"agg_map['{out_col}'] source_col {source_col!r} not present in "
                f"{source_metadata['object_id']}"
            )

    # after a groupBy().agg(), only the group_by_cols and the agg_map outputs
    # exist - every declared target column must be one or the other.
    unreachable = target_cols - set(group_by_cols) - set(agg_map)
    if unreachable:
        raise ValueError(
            f"Target {target_metadata['object_id']} declares columns not produced by "
            f"group_by_cols or agg_map: {unreachable}"
        )

    agg_lines = ",\n".join(
        f'            {_AGG_FUNCS[spec["func"]]}("{spec.get("source_col") or "*"}").alias("{out_col}")'
        for out_col, spec in agg_map.items()
    )
    group_by_cols_repr = ", ".join(f'"{c}"' for c in group_by_cols)

    return {
        "source_table": params["source_table"],
        "group_by_cols_repr": group_by_cols_repr,
        "agg_lines": agg_lines,
        "select_cols_repr": _select_cols_repr(target_metadata),
    }


def _cast_dedupe_join_context(object_id: str, target_metadata: dict, selection: dict) -> dict:
    params = selection["params"]
    primary_metadata = _load_metadata(params["primary_source"])
    join_metadata = _load_metadata(params["join_source"])
    primary_cols = {c["name"] for c in primary_metadata["columns"]}
    join_cols = {c["name"] for c in join_metadata["columns"]}
    target_cols = _target_cols(target_metadata)

    unknown_cast_cols = set(params.get("primary_cast_map", {})) - primary_cols
    if unknown_cast_cols:
        raise ValueError(
            f"Selection primary_cast_map casts columns not present in "
            f"{primary_metadata['object_id']}: {unknown_cast_cols}"
        )

    unknown_key_cols = set(params.get("primary_key_cols", [])) - primary_cols
    if unknown_key_cols:
        raise ValueError(
            f"Selection primary_key_cols not present in {primary_metadata['object_id']}: "
            f"{unknown_key_cols}"
        )

    join_key = params["join_key"]
    if join_key not in primary_cols:
        raise ValueError(f"join_key {join_key!r} not present in {primary_metadata['object_id']}")
    if join_key not in join_cols:
        raise ValueError(f"join_key {join_key!r} not present in {join_metadata['object_id']}")
    if join_key not in target_cols:
        raise ValueError(f"join_key {join_key!r} not present in target {target_metadata['object_id']}")

    # after casting the primary side and left-joining the secondary side, the
    # only columns available are primary's own + join's own - every declared
    # target column must be traceable to one side or the other.
    unreachable = target_cols - primary_cols - join_cols
    if unreachable:
        raise ValueError(
            f"Target {target_metadata['object_id']} declares columns not present in "
            f"either {primary_metadata['object_id']} or {join_metadata['object_id']}: "
            f"{unreachable}"
        )

    cast_lines = "\n".join(
        f'        df = df.withColumn("{col}", F.col("{col}").cast("{cast_type}"))'
        for col, cast_type in params.get("primary_cast_map", {}).items()
    )
    primary_key_cols_repr = ", ".join(f'"{c}"' for c in params["primary_key_cols"])

    return {
        "primary_source": params["primary_source"],
        "cast_lines": cast_lines,
        "primary_key_cols_repr": primary_key_cols_repr,
        "primary_order_col": params["primary_order_col"],
        "join_source": params["join_source"],
        "join_key": join_key,
        "select_cols_repr": _select_cols_repr(target_metadata),
    }


TEMPLATE_CONTEXT_BUILDERS = {
    "cast_and_dedupe": _cast_and_dedupe_context,
    "aggregate": _aggregate_context,
    "cast_dedupe_join": _cast_dedupe_join_context,
}


def generate(object_id: str) -> Path:
    target_metadata = _load_metadata(object_id)
    selection = _load_selection(object_id)
    template_name = selection["template"]

    builder = TEMPLATE_CONTEXT_BUILDERS.get(template_name)
    if builder is None:
        raise ValueError(
            f"No generator context builder registered for template {template_name!r}. "
            f"Known templates: {sorted(TEMPLATE_CONTEXT_BUILDERS)}"
        )
    context = builder(object_id, target_metadata, selection)

    template_path = TEMPLATES_DIR / f"{template_name}.py.tmpl"
    template = Template(template_path.read_text())

    rendered = template.substitute(
        **context,
        table_name=object_id.split(".", 1)[1],
        object_id=object_id,
        metadata_hash=hash_object(target_metadata),
    )

    GENERATED_DIR.mkdir(exist_ok=True)
    out_path = GENERATED_DIR / f"{object_id.replace('.', '_')}.py"
    out_path.write_text(rendered)
    return out_path


if __name__ == "__main__":
    print(generate(sys.argv[1]))
