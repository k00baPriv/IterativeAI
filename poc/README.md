# Metadata-driven, incremental pipeline generator (POC)

## The problem this solves

The original process was **linear**: metadata goes in, an AI agent writes all the
pipeline/notebook code in one shot, and you hope it's correct end to end. That's fine
for a first build. It breaks down the moment you need to *change* something:

- Every update re-runs the whole thing, so a small change risks silently altering
  code that was already correct and tested.
- Hand-patching generated code to avoid that risk means the metadata stops being the
  real source of truth — the code and the metadata drift apart.
- There's no way to know *how much* of the pipeline is actually affected by a given
  metadata change, so "regenerate everything" becomes the only safe-feeling option,
  which is slow and token-expensive.

This POC replaces "regenerate everything, from a fresh prompt, every time" with a
**compiler model**: metadata is versioned source code, and generation is a build step
that only touches what actually changed.

## Core idea

1. **Metadata is the only source of truth**, and it's diffable (it's just JSON files).
2. Every metadata object records what it **depends on**. That's a dependency graph,
   for free, with no extra bookkeeping.
3. When metadata changes, we don't regenerate everything — we compute the **blast
   radius**: the changed object(s) plus everything downstream of them in the
   dependency graph.
4. Only objects in the blast radius are regenerated. Generation of an object is still
   a clean, one-shot build from its own metadata — so it's **idempotent** (same
   metadata in, same code out), it just isn't run unnecessarily.
5. **AI reasoning is isolated to one small, bounded step**: picking a transformation
   template and extracting its parameters from metadata. It never writes pipeline
   code freehand. Turning that selection into actual code is a deterministic script —
   zero tokens, zero hallucination risk, at that step.

This is why the design directly addresses both of your original asks — "make updates
safe/incremental" and "cut token use and hallucinations" — with one architecture, not
two.

## Directory structure

```
poc/
  metadata/
    bronze/orders_raw.json        source object: schema only, no transformation
    gold/fact_orders.json         derived object: depends_on + transformation spec
  manifest/
    hash_manifest.json            last-known content hash per object_id
  templates/
    registry.json                 catalog of available templates + their param schemas
    cast_and_dedupe.py.tmpl       one vetted, parameterized code template
  selections/
    <object_id>.selection.json    output of the AI skill: {template, params, justification}
  build/
    diff_engine.py                hashes metadata, computes the blast radius (no AI)
    generator.py                  turns a selection.json into generated code (no AI)
  generated/
    <object_id>.py                the final, generated pipeline code
  .claude/skills/select-pipeline-template/SKILL.md
                                   the one AI-reasoning step, scoped to this project
  run.py                          orchestrates diff -> (skill) -> generate
```

## How it works, piece by piece

### 1. Metadata (`metadata/`)

Each object is one JSON file. A **source** object (`bronze.orders_raw`) just declares
its schema. A **derived** object (`gold.fact_orders`) additionally declares:

- `depends_on`: which objects feed it — this *is* the dependency graph.
- `transformation`: a `type` (e.g. `cast_and_dedupe`), a `grain`, and a plain-English
  `logic_description`. This is what the AI skill reads to pick a template and fill in
  parameters — it is never asked to invent transformation logic itself.

```json
// metadata/gold/fact_orders.json
{
  "object_id": "gold.fact_orders",
  "depends_on": ["bronze.orders_raw"],
  "transformation": {
    "type": "cast_and_dedupe",
    "logic_description": "cast amount to decimal(18,2), dedupe on order_id keeping the latest row by order_date",
    "grain": "order_id"
  },
  "columns": [...]
}
```

### 2. Hash manifest (`manifest/hash_manifest.json`)

After every successful generation, each object's metadata is hashed (canonical JSON,
SHA-256) and stored here: `{object_id: hash}`. This is the *only* state the system
remembers between runs. No conversation history, no accumulated context — just "what
did this object's metadata look like last time we built it successfully."

### 3. Diff engine (`build/diff_engine.py`) — deterministic, no AI

On `python run.py diff`:

1. Hash every metadata object right now.
2. Compare against the manifest. Anything with a new or changed hash → `changed_or_new`.
3. Build the **reverse** dependency graph (`object -> things that depend on it`) from
   every `depends_on` field.
4. Starting from `changed_or_new`, walk the reverse graph forward (breadth-first) to
   pull in everything downstream. The result is the **blast radius**.
5. Split the blast radius into:
   - **source objects** (no `transformation` block) — their hash changed, but there's
     nothing to generate; the change is just recorded.
   - **generation targets** — objects that actually need the skill + generator run.

This means a schema change to `bronze.orders_raw` correctly pulls `gold.fact_orders`
into the blast radius even if `fact_orders`'s own transformation logic didn't change —
because something upstream of it did, and that might matter.

### 4. Template library (`templates/`)

`registry.json` is the catalog: each template's `matches_transformation_type` (which
`transformation.type` values it covers) and its `params_schema` (what parameters it
needs, and what each one means). `cast_and_dedupe.py.tmpl` is the actual code, with
`$placeholders` (Python `string.Template` syntax) for the parts that vary per object.

Templates are meant to be small in number, hand-reviewed, and reused across many
objects — the "vetted library" that makes per-object generation cheap and safe.

### 5. The AI step — `select-pipeline-template` skill

This is the **only** place an LLM does any reasoning, and its job is deliberately
narrow: given one `object_id`, read its metadata (and its dependencies' metadata),
read the template registry, and output one small JSON file —

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

It writes this to `selections/<object_id>.selection.json` and stops. It does not
write Python. It does not touch any file outside `selections/`. If no template in the
registry matches the object's `transformation.type`, it's required to say so instead
of guessing — that's the designed fallback point for "this needs a genuinely new
template," which is a human-in-the-loop event, not a silent generation.

This is what keeps token cost low (one small structured JSON per object, not a full
script) and keeps hallucination surface small (the model can only select from
pre-vetted templates and fill declared parameters, with every parameter traceable to
a specific metadata field).

### 6. Generator (`build/generator.py`) — deterministic, no AI

Takes a `selection.json`, loads the template it names, and:

1. **Validates** — every column in `cast_map` must actually exist in the source
   object's metadata; every column in `key_cols` must exist in the target object's
   metadata. A mismatch aborts generation rather than producing code that references
   a column that doesn't exist.
2. **Renders** — substitutes the parameters into the template and writes the result
   to `generated/<object_id>.py`.

No LLM call happens in this step. Same selection in → same code out, every time.

### 7. Orchestration (`run.py`)

```
python run.py diff       # compute + persist the blast radius, print what needs the skill
python run.py generate   # run the deterministic generator over every generation
                          # target in the blast radius, then update the hash manifest
```

The loop in practice:

1. `python run.py diff` — see what changed and what's downstream of it.
2. For each generation target it lists, run `/select-pipeline-template <object_id>`
   in Claude Code — this is the one AI-reasoning step, and it only touches
   `selections/`.
3. `python run.py generate` — deterministically turn those selections into code and
   update the manifest.

## Walkthrough: what we actually ran

This exact sequence was executed against the two tables in this repo, in order:

1. **Cold start.** Manifest was empty. `diff` flagged both `bronze.orders_raw`
   (source, no generation needed) and `gold.fact_orders` (generation target).
2. **Selection.** A `selections/gold.fact_orders.selection.json` was produced
   (selecting the `cast_and_dedupe` template, per the JSON shown above).
3. **Generate.** `python run.py generate` validated the selection against both
   objects' metadata, rendered the template, and wrote
   `generated/gold_fact_orders.py`:

   ```python
   def build_fact_orders(spark):
       df = spark.table("bronze.orders_raw")

       df = df.withColumn("amount", F.col("amount").cast("decimal(18,2)"))

       window = Window.partitionBy("order_id").orderBy(F.col("order_date").desc())
       df = df.withColumn("_rn", F.row_number().over(window)).filter(F.col("_rn") == 1).drop("_rn")

       return df
   ```

   The manifest was updated with both objects' hashes.
4. **Idempotency check.** Running `diff` again immediately reported *"No changes
   detected"* — proving the system doesn't regenerate anything unless metadata
   actually changed.
5. **Upstream schema change.** A `currency` column was added to
   `metadata/bronze/orders_raw.json` — `fact_orders`'s own transformation logic was
   untouched. `diff` correctly flagged `bronze.orders_raw` as a changed source *and*
   pulled `gold.fact_orders` back into the generation targets, purely via the
   `depends_on` edge — proving the blast-radius propagation works, not just direct
   hash comparison.

**Note:** step 5 is currently unresolved in this repo — `fact_orders` is flagged for
regeneration but hasn't been rebuilt yet. That's a deliberate leftover: run the skill
and `python run.py generate` yourself to see the loop close.

## Why this addresses token cost and hallucination specifically

- **Tokens**: the AI step never emits a full script — only a small JSON object
  (template name + a handful of parameters). All the verbose code (imports, window
  functions, error handling) lives once in the template, reused across every object
  that matches, and costs zero tokens per regeneration.
- **Hallucination containment**: the model can't invent join logic, column names, or
  cast expressions from nothing — it can only select from a pre-approved template and
  fill parameters it can point back to a specific metadata field (`justification`).
  The generator then independently re-validates those parameters against the actual
  metadata before writing any code, so even a wrong selection can't silently produce
  code referencing a nonexistent column.
- **Blast radius scoping**: because regeneration is scoped to what actually changed
  (plus what's downstream), most runs touch one or two objects, not the whole
  pipeline — so the token/hallucination savings compound instead of being paid fresh
  on every single update.

## Extending the system

- **New object (additive change)**: add its metadata file with a `depends_on`. Next
  `diff` will flag it as new; nothing else regenerates.
- **Changed transformation on an existing object**: edit its `transformation` block.
  `diff` flags it and everything downstream.
- **New transformation shape not covered by any template**: the skill will refuse to
  guess and report the gap. At that point a human (or a separate, more expensive
  free-form generation + heavier review pass) adds a new template to
  `templates/registry.json` + a new `.py.tmpl`. Once reviewed and added, it becomes
  reusable for every future object of that shape — the fallback path shrinks over
  time instead of recurring.

## Current limitations of this POC

- Only one template (`cast_and_dedupe`) exists; anything else would hit the
  "no template matches" fallback by design.
- The skill hasn't yet been run *for real* through Claude Code on this repo — the
  existing `selections/gold.fact_orders.selection.json` was written by hand to prove
  the generator side works. The real test is deleting it and running
  `/select-pipeline-template gold.fact_orders` to see whether the skill reproduces
  the same output from metadata alone.
- No SCD2/merge/aggregation templates yet — those are the natural next additions
  given the bronze/silver/gold/star-schema scope discussed for the full system.
