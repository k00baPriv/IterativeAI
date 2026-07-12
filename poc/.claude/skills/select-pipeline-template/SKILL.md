---
name: select-pipeline-template
description: Selects the matching transformation template and extracts parameters for one bronze/silver/gold data object, given its metadata and the template library. Use to (re)generate a single pipeline object identified by object_id.
---

# Select Pipeline Template

You are the template-selection step in a metadata-driven pipeline generator. Your only job is to turn one object's metadata into a structured template selection. You do not write pipeline code yourself, and you do not invent transformation logic that isn't stated in the metadata.

## Input

You are invoked with one `object_id`, e.g. `/select-pipeline-template gold.fact_orders`.

## Steps

1. Read `poc/metadata/<layer>/<name>.json` for the given object_id (layer and name come from splitting object_id on the first `.`).
2. Read the metadata for every object listed in its `depends_on` — you need their `columns` to know what's actually available upstream.
3. Read `poc/templates/registry.json` for the available templates and their `params_schema`.
4. Pick the template whose `matches_transformation_type` includes the object's `transformation.type`. If none match, STOP and report that no template covers this object instead of guessing at code.
5. Extract parameters for the chosen template strictly from fields you read in steps 1-2 (e.g. `source_table` from `depends_on`, `key_cols` from `transformation.grain`, `cast_map`/`order_col` from `transformation.logic_description`). For every parameter, record which metadata field justified it.
6. Validate before writing output:
   - Every column referenced in `cast_map` must exist in the source object's `columns`.
   - Every column in `key_cols` must exist in the target object's `columns`.
   - If validation fails, STOP and report the mismatch instead of writing a selection file.

## Output

Write the result to `poc/selections/<object_id>.selection.json`, for example:

```json
{
  "object_id": "gold.fact_orders",
  "template": "cast_and_dedupe",
  "params": {
    "source_table": "bronze.orders_raw",
    "key_cols": ["order_id"],
    "order_col": "order_date",
    "cast_map": {"amount": "decimal(18,2)"}
  },
  "justification": {
    "source_table": "depends_on[0]",
    "key_cols": "transformation.grain",
    "order_col": "transformation.logic_description: 'keep latest by order_date'",
    "cast_map": "transformation.logic_description: 'cast amount to decimal(18,2)'"
  }
}
```

Do not write any Python code and do not modify files outside `poc/selections/`. The deterministic, non-AI step (`poc/build/generator.py`) turns this JSON into actual code separately — that separation is the whole point of this skill.
