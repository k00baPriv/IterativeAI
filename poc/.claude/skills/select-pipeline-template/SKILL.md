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
2. Read the metadata for every object listed in its `depends_on` — you need their `columns` to know what's actually available upstream. Note some objects (like `cast_dedupe_join`) depend on more than one object — read all of them, not just the first.
3. Read `poc/templates/registry.json` for the available templates and their `params_schema`.
4. Pick the template whose `matches_transformation_type` includes the object's `transformation.type`. If none match, STOP and report that no template covers this object instead of guessing at code.
5. Extract parameters for the chosen template strictly from fields you read in steps 1-2, guided by that template's `params_schema` in the registry (e.g. a source object_id from `depends_on`, a dedupe/group key from `transformation.grain`, casts/aggregations/join keys from `transformation.logic_description`). For every parameter, record which metadata field justified it.
6. Validate before writing output, using that template's `params_schema` as the checklist — in general:
   - Every column any param references (a cast, a group-by column, a join key, an aggregation's source column) must actually exist in the metadata of the object it's read from.
   - Every column the *target* object declares in its own `columns` must be traceable to something the chosen template can actually produce (a pass-through/cast from a single source, a group-by column or aggregate output, or a column from either side of a join) — never assume a column exists just because the target metadata lists it.
   - If validation fails, STOP and report the mismatch instead of writing a selection file.

Note: the deterministic generator re-checks all of this independently against the metadata before writing any code — your validation here is a first line of defense, not the only one.

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
