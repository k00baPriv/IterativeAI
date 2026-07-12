"""Deterministic, LLM-free step: turns a template selection (produced by the
select-pipeline-template skill) into generated pipeline code. No reasoning
happens here - only lookup, substitution, and schema validation."""
import json
from pathlib import Path
from string import Template

BASE = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE / "templates"
SELECTIONS_DIR = BASE / "selections"
METADATA_DIR = BASE / "metadata"
GENERATED_DIR = BASE / "generated"


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


def _validate(selection: dict, target_metadata: dict, source_metadata: dict) -> None:
    source_cols = {c["name"] for c in source_metadata["columns"]}
    target_cols = {c["name"] for c in target_metadata["columns"]}
    params = selection["params"]

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


def generate(object_id: str) -> Path:
    target_metadata = _load_metadata(object_id)
    selection = _load_selection(object_id)

    params = selection["params"]
    source_metadata = _load_metadata(params["source_table"])
    _validate(selection, target_metadata, source_metadata)

    template_name = selection["template"]
    template_path = TEMPLATES_DIR / f"{template_name}.py.tmpl"
    template = Template(template_path.read_text())

    cast_lines = "\n".join(
        f'    df = df.withColumn("{col}", F.col("{col}").cast("{cast_type}"))'
        for col, cast_type in params.get("cast_map", {}).items()
    )
    key_cols_repr = ", ".join(f'"{c}"' for c in params["key_cols"])
    table_name = object_id.split(".", 1)[1]

    rendered = template.substitute(
        table_name=table_name,
        source_table=params["source_table"],
        cast_lines=cast_lines,
        key_cols_repr=key_cols_repr,
        order_col=params["order_col"],
    )

    GENERATED_DIR.mkdir(exist_ok=True)
    out_path = GENERATED_DIR / f"{object_id.replace('.', '_')}.py"
    out_path.write_text(rendered)
    return out_path


if __name__ == "__main__":
    import sys
    print(generate(sys.argv[1]))
