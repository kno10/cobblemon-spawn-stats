# Cobbleverse Spawn + Drop Data Extractor

A clean Python tool that extracts Cobblemon world-spawn **and item-drop** data
from the **Cobbleverse** modpack (Modrinth, latest version) into clean, UI-ready JSON:

- **(A)** spawn rules per Pokémon → `output/<pack>-<version>/pokemon/<id>.json`
- **(C)** spawn weights + conditions per location →
  `output/<pack>-<version>/spawn_weights.json` — the single odds source
- drops for any world state are computed from (C); `examples/spawn_odds.py` is the
  reference calculator: where a Pokémon appears, when, and how likely per roll
  (event/spawn triggers included)
- **(D)** item drops per Pokémon (kill + pasture) →
  `output/<pack>-<version>/drops.json` — the single drop source
- `examples/drop_odds.py` is the reference drop calculator: what each Pokémon
  drops when killed and while pastured, how often (Monte-Carlo over the
  stateful selection loop)
- **(E)** `lang/<locale>.json` — verbatim (byte-exact) copies of the base
  Cobblemon jar's `assets/cobblemon/lang/*.json` — UI translations for
  non-minecraft items etc. (keys like `item.cobblemon.relic_coin = "Alter Dukat"`)

## Commands

```bash
python3 extract_cobbleverse_spawns.py                     # latest Cobbleverse
python3 extract_cobbleverse_spawns.py --version 1.7.42    # pin a version
python3 extract_cobbleverse_spawns.py --scan-all-mods     # no name filtering

python3 examples/spawn_odds.py                            # odds for pumpkaboo (default)
python3 examples/spawn_odds.py zekrom                     # any pokemon / prefix (unown, ...)

python3 examples/drop_odds.py                             # drops for pumpkaboo (default)
python3 examples/drop_odds.py pikachu                     # any pokemon / prefix
python3 examples/drop_odds.py gholdengo --trials 500000   # more Monte-Carlo trials
```

Stdlib only (Python 3.11+). No `npm`, no `pip install`, no build step.

Downloads are cached under `data/` (sha1-verified); re-runs skip re-downloads.
Outputs are written to `output/<pack>-<version>/` and are fully regenerated.

## Layout

- `extract_cobbleverse_spawns.py` — the whole tool, top to bottom
- `data/` — cache only (mrpack + locked mod jars): gitignored, safe to delete
- `output/` — generated JSON: gitignored, derived artifact

## Data model (important)

### Sources & precedence

1. tier 2 = datapack/override content: lockfile `datapacks/*` entries and archives
   nested in the mrpack (`overrides/datapacks/*.zip`, `overrides/mods/*.jar`).
2. tier 1 = locked mod jars (default: name-matched subset, see
   `DEFAULT_JAR_NAME_RE`; `--scan-all-mods` removes the filter).

Same source path → higher tier wins (same rule applies to spawn pools, species files,
biome tags, and the spawner/pasture configs). The actual Cobbleverse world spawns
live in `overrides/datapacks/COBBLEVERSE-DP-v31.zip` (1024 pools), which overrides
the base Cobblemon jar (824 pools). `cobblemon-additions` adds structure pools;
`mega_showdown` overrides several species drop tables (`overridden_by`); the
`pastureLoot` mod gates pasture drops via `overrides/config/PastureLoot.json`.
Hoenn/Johto/Sinnoh/Terralith DPs are opt-in tag-only add-ons.

### Probability model (per location + per world state, documented in `meta.json`)

```
P(row @ L, state S) = bucket_weights[b] / Σ(bucket weights of buckets active @ L in state S)
                   × w(row,S) / Σ(w(row',S) over rows active @ L in bucket b in state S)
```

- Bucket weights come from the pack's config override
  (`overrides/config/cobblemon/spawning/best-spawner-config.json`):
  common 88.5 / uncommon 10 / rare 1.2 / ultra-rare 0.3.
- **`conditions` determine row activity for a world state S** (time of day, sky,
  weather, nearby blocks, presets, ...): the UI evaluates each row's gates
  against S, applies matching `weight_mult`, and *only then* normalizes over the
  rows that are active. Rows for other states never dilute the roll.
- **Row-state reading** (used by `examples/spawn_odds.py`): a row's odds are
  reported in the state the row's own condition pins down (“chance per roll,
  given the row's condition holds”) — that is the WHERE + WHEN hunting answer;
  plain rows are sampled in a dry-day state.
- **The only thing that never touches the roll is `position`**
  (grounded/surface/submerged): it only picks the spawn's Y-level/placement
  within the location — kept as reference info.
- `weight: 0` rows are trigger/event spawns (custom zones, story spawns); they are
  never rolled — `examples/spawn_odds.py` lists them separately per Pokémon.
- The pack sometimes duplicates entries in a pool (e.g. basculin ×6); in-game each
  copy adds weight, so the lookup aggregates by **(pokemon, bucket, conditions)**
  — one row = one coherent world state (identical duplicates summed).

### Spawn weights table (`spawn_weights.json`)

The UI-facing raw-weight table **for odds math** (deliberately *not*
pre-normalized — `conditions` make the active row set world-state-dependent):

```json
{
  "bucket_weights": {"common": 88.5, "uncommon": 10, "rare": 1.2, "ultra-rare": 0.3},
  "minecraft:badlands": {
    "common": [
      {"pokemon": "maschiff", "weight": 4.0},
      {"pokemon": "pumpkaboo", "weight": 5.7,
       "conditions": [{"world": {"time": "night", "near_blocks": ["minecraft:pumpkin"]}}]}
    ]
  }
}
```

- top-level keys = location ids (`minecraft:<biome>`, `structure:<...>`,
  `#minecraft:<tag>`); each maps bucket → rows `{pokemon, weight, conditions?}`.
- `weight` is the aggregated in-game weight; **no probability in the lookup** —
  the UI computes it from `bucket_weights` + row weights, filtered by `conditions`.
- a row is *active* at (location L, state S) when every gate holds: `world` keys
  match S, `presets` allow the active spawn context, no `anti_*` excludes L/S.
- `weight_mult` adjusts the effective weight when its `when` object (same world
  vocabulary) holds — can go up **or down** (e.g. machamp ×0.25 at night).
- `conditions` vocabulary (one of these per concern, emitted only when nonzero):
  - `presets`: context list (natural context = plain; specific contexts like
    `dungeon` only roll in that spawn detail).
  - `world`: `{time: day|night|dusk, skylight:{min,max} (baseline {min:8,max:15} is
    suppressed), can_see_sky, raining, thundering, near_blocks:[ids],
    base_blocks, y:{min,max}, x:{min,max}, max_light, moon, slime_chunk,
    lure_min, lure_max, rod, bait, bobber, dimensions}` — bounds objects
    `{min,max}` may be one-sided (e.g. `y:{max:48}` = at most 48; a lone number
    would be ambiguous and is never emitted).
  - `weight_mult`: `{multiplier, when:{...world keys}}`.
  - `requires_mods`, `anti_biomes` (resolved — spawn excluded there),
    `anti_structures`, `anti_world` (same keys as `world`).
- `position` (grounded/surface/submerged) is NOT a condition: it only determines
  the spawn's Y-level/placement within the location and never modifies rolls.

### Drops (item drops on kill + pasture)

Raw drop tables for `data/cobblemon/species/<gen>/<id>.json` →
`output/<pack>-<version>/drops.json` (`drop_model`, `source_of_truth`, `pasture`,
`pokemon{}`):

```json
{
  "drop_model": "...",
  "pasture": {"chance_per_minute": 0.15, "tick_per_minute": 1200, "item_blacklist": ["minecraft:chicken", ...]},
  "pokemon": {
    "pikachu": {"dex": 25, "name": "Pikachu", "amount": 3,
      "entries": [
        {"item": "cobblemon:light_ball", "percentage": 5.0},
        {"item": "minecraft:feather", "quantity_range": {"min": 2, "max": 4}},
        {"item": "minecraft:chicken"}
      ],
      "overridden_by": ["mods/Cobblemon-....jar"],
      "source": "mods/mega_showdown-....jar :: data/cobblemon/species/generation1/pikachu.json"}
  }
}
```

- `percentage` defaults to **100** (omitted = guaranteed) and `quantity_range`
  defaults to **{1,1}** (omitted = exactly 1). Both are omitted when they equal
  their defaults. `quantity_range` uses the same one-sided `{min,max}` bounds
  convention as spawn y/x. `min:0` = may drop nothing.
- `amount` = per-trigger drop budget (number of slots). Selection is stateful:
  per slot the first (list-order) `percentage`-passing entry is picked, each
  entry at most once, a slot where nobody passes still spends one budget point.
  `examples/drop_odds.py` Monte-Carlo simulates this exactly.
- **pasture vs kill**: same table. kill always triggers. A pasture only rolls
  when its per-minute check (`chance_per_minute`) passes, and any entry whose
  item is in `item_blacklist` is never dropped. `pasture.cfg` is the pack's
  `overrides/config/PastureLoot.json` (pasturLoot mod).
- **No `probability` field anywhere in `drops.json`** — like `spawn_weights.json`,
  the raw tables are the single source and `examples/drop_odds.py` is the
  reference calculator (per-species probability = function of `percentage`,
  `amount`, `quantity_range`, and pasture `chance_per_minute`/`item_blacklist`).
- A species file that lacks a `drops` table, or an overriding file that removes it,
  means "no drops for that Pokémon" (e.g. legendaries often have `drops: null`).
- `overridden_by` lists the lower-tier source files that lost to `source` for the same
  species id (see Sources & precedence). `source_of_truth` points at the species files.

### Biomes & tags

- Cobblemon biome tags (`#cobblemon:is_jungle`, …) resolve **transitively**.
- Members carry `required` flags; `biomes` (default install) vs
  `optional_biomes` (optional mods: Terralith, Biomes O' Plenty, Wythers, ...) are
  reported separately; only `biomes` enter probability tables.
- `#minecraft:is_*` tags resolve from **built-in vanilla 1.21.1 definitions**
  (`VANILLA_BIOME_TAGS` in the source) — they take priority over tag files shipped
  by third-party archives (e.g. Terralith-DP rewriting `minecraft` namespace tags),
  because third-party `minecraft:` overrides are not active in a vanilla world.
- `unresolved_refs` lists tags with no definition anywhere (mostly `#c:*` common
  tags and uninstalled mods) — a spawn depending on them is still emitted, just
  with an incomplete biome list.

### Conventions

- **ids are raw identifiers** (e.g. `basculin striped=blue`, `unown character=!`) —
  never title-case in tools; display names are the UI's job (PokeAPI).
- **Compact JSON, no `null` values, no empty containers**: `clean()` strips them at
  write time; absence of a key means "not present". Do not re-add `None` defaults
  or `json.dumps(..., indent=...)` to the output writers.
- Filenames: `sanitize_name()` is injective (space→`_`, specials→`%XX`).
- Per-Pokémon files + `pokemon_index.json` (sorted by dex, then id).

## Gotchas (learned the hard way — see also source comments)

- Nested archives inside the mrpack must be opened via `io.BytesIO` (zip-in-zip).
- `TAG_RE` must match top-level tag files (`…/biome/(.*)json`), not only
  subdirectories — `is_jungle` etc. live at the top level.
- Some "jars" are actually plain files / XML, not zips — the scanner must tolerate
  non-zip entries without crashing.
- The vanilla `minecraft:is_*` tag lists are hardcoded from mcmeta 1.21.1; when the
  game version changes, update `VANILLA_BIOME_TAGS`.
- Modrinth version resolution: latest release is selected unless `--version` is
  given; the version's `modrinth.index.json` lists every locked file with download
  URL + sha1.

### Drops gotchas

- Drop tables are NOT in the spawn pools — they live in
  `data/cobblemon/species/<gen>/<id>.json` → `drops`. `spawn_weights.json` says
  nothing about items; `drops.json` is the only item-drop source.
- A species with `drops: null`/absent legitimately drops nothing. Don't
  "fix" it by inferring items from biome or type — absence is the data.
- `percentage` is a chance **per drop slot**, and there are `amount` slots,
  so a `5%` entry with `amount: 6` is more likely than 5% per trigger.
  The `examples/drop_odds.py` Monte-Carlo captures this; don't hand-compute it.
- `quantity_range` `min:0` means the item is guaranteed to *roll* but *drops
  nothing* half the time — it is **not** a 0% entry.
- Pasture ≠ kill: the same `drops` table, but pasture is gated by
  `pasture.chance_per_minute` and `pasture.item_blacklist` (from the pack's
  `overrides/config/PastureLoot.json`). Two different output rates from one table.
- `lang/<locale>.json` are verbatim copies of the base Cobblemon jar's
  `assets/cobblemon/lang/*` — the only source used (label filter), do not
  re-serialize; zamega/mega_showdown ship a few of the same files but are
  intentionally ignored. Item names are `item.cobblemon.<item>`; missing keys
  (not every item has a translation in every locale) must fall back to the raw id.
