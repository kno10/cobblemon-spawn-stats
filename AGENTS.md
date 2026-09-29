# Cobbleverse Spawn Data Extractor

A clean Python tool that extracts Cobblemon world-spawn data from the **Cobbleverse**
modpack (Modrinth, latest version) into clean, UI-ready JSON:

- **(A)** spawn rules per Pokémon → `output/<pack>-<version>/pokemon/<id>.json`
- **(C)** spawn weights + conditions per location →
  `output/<pack>-<version>/spawn_weights.json` — the single odds source
- odds for any world state are computed from (C); `examples/spawn_odds.py` is the
  reference calculator: where a Pokémon appears, when, and how likely per roll
  (event/spawn triggers included)

## Commands

```bash
python3 extract_cobbleverse_spawns.py                     # latest Cobbleverse
python3 extract_cobbleverse_spawns.py --version 1.7.42    # pin a version
python3 extract_cobbleverse_spawns.py --scan-all-mods     # no name filtering

python3 examples/spawn_odds.py                            # odds for pumpkaboo (default)
python3 examples/spawn_odds.py zekrom                     # any pokemon / prefix (unown, ...)
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

Same pool path → higher tier wins. The actual Cobbleverse world spawns live in
`overrides/datapacks/COBBLEVERSE-DP-v31.zip` (1024 pools), which overrides the base
Cobblemon jar (824 pools). `cobblemon-additions` adds structure pools;
`mega_showdown`/`zamega` add extras; Hoenn/Johto/Sinnoh/Terralith DPs are opt-in
tag-only add-ons.

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
