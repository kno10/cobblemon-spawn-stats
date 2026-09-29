#!/usr/bin/env python3
"""Extract Cobblemon world-spawn data from the Cobbleverse modpack (Modrinth).

Downloads (sha1-verified) and caches under ./data:
  - the Cobbleverse .mrpack                     (data/mrpacks/)
  - locked mod jars that may hold spawn data    (data/cache/<lockfile path>)
Only the relevant JSON entries are read out of each archive in memory -
nothing is ever unpacked to disk.

Read from each archive (higher tier wins on path conflicts):
  - spawn pools   data/cobblemon/spawn_pool_world/<pool>.json        (tier)
  - biome tags    data/<ns>/tags/worldgen/biome/**/*.json
  - presets       data/cobblemon/spawn_detail_presets/*.json
  - spawner cfg   config|data/.../spawning/best-spawner-config.json  (bucket weights)
tier 2 = datapacks & override content (lockfile datapacks/, archives inside the mrpack)
tier 1 = locked mod downloads (resource packs are never data sources)

Outputs (compact JSON under ./output/<pack>-<version>/):
  meta.json                       pack/versions/sources/probability model
  pokemon_index.json              one row per pokemon id (id, dex, file, biomes)
  pokemon/<id>.json               (A) spawn rules for each pokemon

                                  custom spawn zone) with roll probabilities
  spawn_weights.json              (C) raw spawn weights: location -> bucket ->
                                  [{pokemon, weight, conditions?}] + bucket weights

Naming: ids are kept raw as identifiers (no title-casing); display/translated
names are the UI's job (e.g. from PokeAPI).

Probability model (per location, per requirements):
  P(entry) = bucket_weight[b] / sum(active bucket weights at this location)
           * entry_weight / sum(entry weights in bucket b at this location)
A row's `conditions` (structures, dimensions, rain, skylight, presets, weight
multipliers, anti-biomes, ...) determine whether the spawn is ACTIVE at all in a
given world state - the UI must evaluate them before applying the roll math,
then normalize over the active rows. The only context that does not affect
rolls is `position` (grounded / surface / submerged): it only determines the
spawn's Y-level/placement within the location.
Entries with weight 0 are trigger/event spawns (not rolled).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_SLUG = "cobbleverse"
MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "cobbleverse-spawn-extractor/1.0"
DEFAULT_BUCKET_WEIGHTS = {"common": 1.0, "uncommon": 1.0, "rare": 1.0, "ultra-rare": 1.0}

POOLS_RE = re.compile(r"^data/cobblemon/spawn_pool_world/[^/]+\.json$")
TAG_RE = re.compile(r"^data/([\w-]+)/tags/worldgen/biome/(.+)\.json$")
PRESET_RE = re.compile(r"^data/cobblemon/spawn_detail_presets/.+\.json$")
SPAWNER_CFG_RE = re.compile(r"^(?:config|data)/cobblemon/spawning/best-spawner-config\.json$")
NESTED_ARCHIVE_RE = re.compile(r"^overrides/.+\.(?:jar|zip)$")
DEFAULT_JAR_NAME_RE = re.compile(
    r"cobb|pok[eé]|legend|lumy|zamega|tmcraft|timcore|showdown|fightorflight|"
    r"catch|capture|pasture|bottle|mobsbegone|rct|mega",
    re.IGNORECASE)


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def get_pack_version(version: str | None) -> dict:
    versions = json.loads(http_get(f"{MODRINTH_API}/project/{PROJECT_SLUG}/version"))
    if not versions:
        sys.exit(f"no versions found for {PROJECT_SLUG}")
    picks = versions if version is None else [v for v in versions if v["version_number"] == version]
    if not picks:
        sys.exit(f"version {version} not found on Modrinth")
    v = picks[0]
    files = v.get("files") or []
    primary = next((f for f in files if f.get("primary")), files[0] if files else None)
    if primary is None:
        sys.exit("modpack version has no downloadable file")
    return {
        "version_number": v["version_number"],
        "version_id": v["id"],
        "name": v.get("name"),
        "date_published": v.get("date_published"),
        "game_versions": v.get("game_versions", []),
        "filename": primary["filename"],
        "url": primary["url"],
        "sha1": primary["hashes"]["sha1"],
        "size": primary.get("size"),
    }


def ensure_downloaded(url: str, sha1: str, dest: Path) -> Path:
    if dest.exists():
        cached = hashlib.sha1(dest.read_bytes()).hexdigest()
        if cached == sha1:
            return dest
        print(f"  ! cache hash mismatch for {dest.name}, re-downloading")
        dest.unlink()
    size = dest.name
    print(f"  downloading {url} -> {size}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = http_get(url)
    got = hashlib.sha1(data).hexdigest()
    if got != sha1:
        sys.exit(f"sha1 mismatch for {dest.name}: expected {sha1}, got {got}")
    dest.write_bytes(data)
    return dest


def load_sources(mrpack_path: Path, scan_all_mods: bool, data_dir: Path) -> tuple[list[dict], list[str]]:
    """Discover candidate archives. Returns (sources, skipped locked paths)."""
    sources: list[dict] = []
    skipped: list[str] = []
    with zipfile.ZipFile(mrpack_path) as z:
        index = json.loads(z.read("modrinth.index.json"))
        nested = sorted(n for n in z.namelist()
                        if NESTED_ARCHIVE_RE.match(n) and "resourcepacks" not in n)
        for entry in sorted(index.get("files", []), key=lambda e: e["path"]):
            path = entry["path"]
            if not re.search(r"\.(jar|zip)$", path, re.IGNORECASE) or "resourcepacks/" in path:
                continue
            name = path.rsplit("/", 1)[-1]
            if not scan_all_mods and not DEFAULT_JAR_NAME_RE.search(name):
                skipped.append(path)
                continue
            dest = data_dir / "cache" / path
            ensure_downloaded(entry["downloads"][0], entry["hashes"]["sha1"], dest)
            tier = 1 if path.startswith("mods/") else 2  # datapacks/ = installed datapacks (override layer)
            sources.append({
                "label": path, "tier": tier,
                "kind": "locked-mod" if tier == 1 else "locked-datapack",
                "archive": dest, "url": entry["downloads"][0],
                "sha1": entry["hashes"]["sha1"],
            })
    # nested archives: read directly from the mrpack (no download needed)
    for name in nested:
        sources.append({"label": name, "tier": 2, "kind": "mrpack-override",
                        "archive": (mrpack_path, name), "url": None, "sha1": None})
    sources.sort(key=lambda s: (s["tier"], s["label"]))
    return sources, skipped


def normalize_tag_members(values) -> list[dict]:
    members = []
    for v in values:
        if isinstance(v, str):
            members.append({"id": v, "required": True})
        elif isinstance(v, dict) and "id" in v:
            members.append({"id": v["id"], "required": v.get("required", True)})
    return members


def collect_sources(sources: list[dict]) -> tuple[
        dict[str, tuple[dict, str]],
        dict[str, tuple[list[dict], str]],
        dict[str, tuple[dict, str]],
        dict[str, tuple[dict, str]],
        dict[str, dict[str, int]],]:
    pools: dict[str, tuple[dict, str]] = {}
    biome_tags: dict[str, tuple[list[dict], str]] = {}
    presets: dict[str, tuple[dict, str]] = {}
    spawner_cfgs: dict[str, tuple[dict, str]] = {}
    per_source: dict[str, dict[str, int]] = {}

    for source in sorted(sources, key=lambda s: (s["tier"], s["label"])):
        label = source["label"]
        stats = {"pools": 0, "biome_tags": 0, "presets": 0, "spawner_cfg": 0}

        def scan(z: zipfile.ZipFile) -> None:
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                if not (POOLS_RE.match(name) or TAG_RE.match(name) or PRESET_RE.match(name)
                        or SPAWNER_CFG_RE.match(name)):
                    continue
                try:
                    data = json.loads(z.read(name))
                except (UnicodeDecodeError, ValueError) as e:
                    print(f"      ! {label}: skipping non-JSON entry {name} ({e.__class__.__name__})")
                    continue
                if POOLS_RE.match(name):
                    pools[name] = (data, label)
                    stats["pools"] += 1
                elif (m := TAG_RE.match(name)):
                    members = normalize_tag_members(data.get("values"))
                    key = f"{m.group(1)}:{m.group(2)}"
                    if key in biome_tags and not data.get("replace", False):
                        old_members, old_src = biome_tags[key]
                        known = {x["id"] for x in old_members}
                        biome_tags[key] = (
                            old_members + [x for x in members if x["id"] not in known],
                            f"{old_src}; {label}")
                    else:
                        biome_tags[key] = (members, label)
                    stats["biome_tags"] += 1
                elif PRESET_RE.match(name):
                    key = name.split("spawn_detail_presets/", 1)[1].removesuffix(".json")
                    presets[key] = (data, label)
                    stats["presets"] += 1
                elif SPAWNER_CFG_RE.match(name):
                    spawner_cfgs[name] = (data, label)
                    stats["spawner_cfg"] += 1

        arc = source["archive"]
        if isinstance(arc, tuple):  # (mrpack_path, inner archive name): read in memory
            mrpack_path, inner = arc
            with zipfile.ZipFile(mrpack_path) as outer:
                scan(zipfile.ZipFile(io.BytesIO(outer.read(inner))))
        else:  # downloaded locked mod
            with zipfile.ZipFile(arc) as z:
                scan(z)
        per_source[label] = {k: v for k, v in stats.items() if v}
    return pools, biome_tags, presets, spawner_cfgs, per_source


def parse_level_range(value) -> list[int] | None:
    if isinstance(value, (int, float)):
        v = int(value)
        return [v, v]
    if not isinstance(value, str) or not (value := value.strip()):
        return None
    if re.fullmatch(r"-?\d+", value):
        v = int(value)
        return [v, v]
    if m := re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", value):
        return [int(m.group(1)), int(m.group(2))]
    return None


def parse_time_list(value) -> list[str]:
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(t) for t in value]
    return []


def collect_weight_multipliers(entry: dict) -> list[dict]:
    """Merge `weightMultiplier` (singular) and `weightMultipliers` (list) into
    {"multiplier": x, "when": {raw condition}} items (deduped)."""
    wms = entry.get("weightMultipliers")
    if isinstance(wms, dict):
        wms = [wms]
    wms = list(wms or [])
    single = entry.get("weightMultiplier")
    if isinstance(single, dict):
        wms.append(single)
    out, seen = [], set()
    for wm in wms:
        if not isinstance(wm, dict):
            continue
        when = {k: v for k, v in (wm.get("condition") or {}).items() if v is not None}
        d = {"multiplier": wm.get("multiplier")}
        if when:
            d["when"] = when
        key = json.dumps(d, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# (8..15) skylight is the pack's default "daytime surface" baseline (2990/5971
# entries); only deviations (e.g. 0..7 = caves) count as conditions.
DEFAULT_SKYLIGHT = {"min": 8, "max": 15}
# spatial gates: satisfiable somewhere - they are WHERE info, not state conflicts
SPACE_KEYS = {"y", "x", "base_blocks", "max_light"}

# Raw Cobblemon condition keys -> compact snake_case world-state keys
WORLD_KEY = {
    "timeRange": "time",
    "canSeeSky": "can_see_sky",
    "isRaining": "raining",
    "isThundering": "thundering",
    "neededNearbyBlocks": "near_blocks",
    "neededBaseBlocks": "base_blocks",
    "minLureLevel": "lure_min",
    "maxLureLevel": "lure_max",
    "moonPhase": "moon",
    "isSlimeChunk": "slime_chunk",
    "maxLight": "max_light",
    "rodType": "rod",
    "bait": "bait",
    "bobber": "bobber",
    "dimensions": "dimensions",
}


def map_world(cond: dict) -> dict:
    """Convert a raw Cobblemon condition (or a weightMultiplier `when`) into a
    compact world-state object, e.g.
    {"time": "night", "near_blocks": ["minecraft:pumpkin", ...]}
    Ranges (skylight/y/x) are explicit bounds objects: {"min": a, "max": b} or
    one-sided ({"min": a} / {"max": b}) so a single number never loses whether
    it was a lower or upper bound."""
    w: dict = {}
    sk = {}; y = {}; xv = {}
    binders = {
        "minSkyLight": (sk, "min"), "maxSkyLight": (sk, "max"),
        "minY": (y, "min"), "maxY": (y, "max"),
        "minX": (xv, "min"), "maxX": (xv, "max"),
    }
    for k, v in (cond or {}).items():
        if v is None or k in ("biomes", "structures"):
            continue
        if k in binders:
            binders[k][0][binders[k][1]] = v
        else:
            w[WORLD_KEY.get(k, k.lower())] = v
    if sk and sk != DEFAULT_SKYLIGHT:
        w["skylight"] = sk
    if y:
        w["y"] = y
    if xv:
        w["x"] = xv
    return w


def condition_descriptors(rec: dict) -> list[dict]:
    """Normalize a spawn record's gating conditions into small single-purpose
    descriptor objects (empty list = plain natural spawn, no restrictions).

    Vocabulary: presets / world / weight_mult / requires_mods / anti_biomes /
    anti_structures / anti_world.
    "biomes"/"structures" inside `cond` are NOT emitted here: they define the
    location the row already belongs to (biome table or structure table)."""
    out: list[dict] = []
    presets = rec.get("presets") or []
    if presets and presets != ["natural"]:
        out.append({"presets": presets})
    world = map_world(rec.get("cond") or {})
    if world:
        out.append({"world": world})
    for wm in rec.get("weight_multipliers") or []:
        out.append({"weight_mult": {"multiplier": wm["multiplier"],
                                   "when": map_world(wm.get("when") or {})}})
    if rec.get("requires_mods"):
        out.append({"requires_mods": rec["requires_mods"]})
    anti = rec.get("anti_condition") or {}
    if anti.get("biomes"):
        resolved = rec.get("except_biomes")
        out.append({"anti_biomes": resolved if resolved else anti["biomes"]})
    if anti.get("structures"):
        out.append({"anti_structures": anti["structures"]})
    aws = map_world(anti)
    if aws:
        out.append({"anti_world": aws})
    return out


def build_spawn_record(entry: dict, pool_path: str, pool: dict, source_label: str) -> dict:
    pokemon = entry.get("pokemon")
    if isinstance(pokemon, str):
        pokemon_refs = [pokemon]
    elif isinstance(pokemon, list):
        pokemon_refs = [p for p in pokemon if isinstance(p, str)]
    else:  # pokemon-herd entries: fall back to member ids
        pokemon_refs = [h["pokemon"] for h in entry.get("herdablePokemon", [])
                        if isinstance(h, dict) and isinstance(h.get("pokemon"), str)]

    condition = entry.get("condition") or {}

    required_mods = (pool.get("neededInstalledMods") or []) + (pool.get("neededUninstalledMods") or [])

    return {
        "spawn_id": entry.get("id"),
        "pokemon_refs": pokemon_refs,
        "type": entry.get("type", "pokemon"),
        "bucket": entry.get("bucket"),
        "level": parse_level_range(entry.get("level") or entry.get("levelRange")),
        "weight": entry.get("weight"),
        "position": entry.get("spawnablePositionType"),
        "presets": entry.get("presets") or [],
        "biome_refs": normalize_tag_members(condition.get("biomes") or []),
        "structures": condition.get("structures") or [],
        "cond": {k: v for k, v in condition.items()
                 if k not in ("biomes", "structures") and v is not None},
        "weight_multipliers": collect_weight_multipliers(entry) or None,
        "anti_condition": entry.get("anticondition") or None,
        "requires_mods": required_mods or None,
        "max_herd_size": entry.get("maxHerdSize"),
        "min_distance_between_spawns": entry.get("minDistanceBetweenSpawns"),
        "herd": [{
            "pokemon": h.get("pokemon"),
            "leader": h.get("isLeader", False),
            "level": parse_level_range(h.get("levelRange")),
            "weight": h.get("weight"),
        } for h in entry.get("herdablePokemon", []) if isinstance(h, dict)],
        "source": f"{source_label} :: {pool_path}",
    }


# Built-in fallback for vanilla biome tags (exact 1.21.1 lists from mcmeta),
# since no scanned archive ships `data/minecraft/tags/worldgen/biome/*.json`.
VANILLA_BIOME_TAGS: dict[str, list[dict]] = {
    tag:
        [{"id": b, "required": True} for b in biomes]
    for tag, biomes in {
        "minecraft:is_badlands": ["minecraft:badlands", "minecraft:eroded_badlands",
                                  "minecraft:wooded_badlands"],
        "minecraft:is_beach": ["minecraft:beach", "minecraft:snowy_beach"],
        "minecraft:is_deep_ocean": ["minecraft:deep_frozen_ocean", "minecraft:deep_cold_ocean",
                                    "minecraft:deep_ocean", "minecraft:deep_lukewarm_ocean"],
        "minecraft:is_end": ["minecraft:the_end", "minecraft:end_highlands",
                             "minecraft:end_midlands", "minecraft:small_end_islands",
                             "minecraft:end_barrens"],
        "minecraft:is_forest": ["minecraft:forest", "minecraft:flower_forest",
                                "minecraft:birch_forest", "minecraft:old_growth_birch_forest",
                                "minecraft:dark_forest", "minecraft:grove"],
        "minecraft:is_hill": ["minecraft:windswept_hills", "minecraft:windswept_forest",
                              "minecraft:windswept_gravelly_hills"],
        "minecraft:is_jungle": ["minecraft:bamboo_jungle", "minecraft:jungle",
                                "minecraft:sparse_jungle"],
        "minecraft:is_mountain": ["minecraft:meadow", "minecraft:frozen_peaks",
                                  "minecraft:jagged_peaks", "minecraft:stony_peaks",
                                  "minecraft:snowy_slopes", "minecraft:cherry_grove"],
        "minecraft:is_nether": ["minecraft:nether_wastes", "minecraft:soul_sand_valley",
                                "minecraft:crimson_forest", "minecraft:warped_forest",
                                "minecraft:basalt_deltas"],
        "minecraft:is_ocean": ["#minecraft:is_deep_ocean", "minecraft:frozen_ocean",
                               "minecraft:ocean", "minecraft:cold_ocean",
                               "minecraft:lukewarm_ocean", "minecraft:warm_ocean"],
        "minecraft:is_overworld": [
            "minecraft:mushroom_fields", "minecraft:deep_frozen_ocean", "minecraft:frozen_ocean",
            "minecraft:deep_cold_ocean", "minecraft:cold_ocean", "minecraft:deep_ocean",
            "minecraft:ocean", "minecraft:deep_lukewarm_ocean", "minecraft:lukewarm_ocean",
            "minecraft:warm_ocean", "minecraft:stony_shore", "minecraft:swamp",
            "minecraft:mangrove_swamp", "minecraft:snowy_slopes", "minecraft:snowy_plains",
            "minecraft:snowy_beach", "minecraft:windswept_gravelly_hills", "minecraft:grove",
            "minecraft:windswept_hills", "minecraft:snowy_taiga", "minecraft:windswept_forest",
            "minecraft:taiga", "minecraft:plains", "minecraft:meadow", "minecraft:beach",
            "minecraft:forest", "minecraft:old_growth_spruce_taiga", "minecraft:flower_forest",
            "minecraft:birch_forest", "minecraft:dark_forest", "minecraft:savanna_plateau",
            "minecraft:savanna", "minecraft:jungle", "minecraft:badlands", "minecraft:desert",
            "minecraft:wooded_badlands", "minecraft:jagged_peaks", "minecraft:stony_peaks",
            "minecraft:frozen_river", "minecraft:river", "minecraft:ice_spikes",
            "minecraft:old_growth_pine_taiga", "minecraft:sunflower_plains",
            "minecraft:old_growth_birch_forest", "minecraft:sparse_jungle", "minecraft:bamboo_jungle",
            "minecraft:eroded_badlands", "minecraft:windswept_savanna", "minecraft:cherry_grove",
            "minecraft:frozen_peaks", "minecraft:dripstone_caves", "minecraft:lush_caves",
            "minecraft:deep_dark"],
        "minecraft:is_river": ["minecraft:river", "minecraft:frozen_river"],
        "minecraft:is_savanna": ["minecraft:savanna", "minecraft:savanna_plateau",
                                 "minecraft:windswept_savanna"],
        "minecraft:is_taiga": ["minecraft:taiga", "minecraft:snowy_taiga",
                               "minecraft:old_growth_pine_taiga",
                               "minecraft:old_growth_spruce_taiga"],
    }.items()
}


def sanitize_name(s: str) -> str:
    """Filesystem-safe, injective name: spaces become _, other specials are percent-encoded."""
    return re.sub(r"[^A-Za-z0-9._-]",
                  lambda m: "%%%02X" % ord(m.group(0)), s.replace(" ", "_"))


def clean(o):
    """Strip None values and empty containers from output structures."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            cv = clean(v)
            if cv is not None and cv != {} and cv != []:
                out[k] = cv
        return out
    if isinstance(o, (list, tuple)):
        out = [clean(v) for v in o]
        return [v for v in out if v is not None and v != {} and v != []]
    return o


def expand_biome_refs(refs: list[dict], biome_tags: dict) -> tuple[set[str], set[str], set[str]]:
    """Expand biome tag references transitively, tracking MC `required` flags.

    Returns (required_biomes, optional_biomes, unresolved_tag_refs).
    `refs` is a list of {"id", "required"}; tags are expanded recursively.
    """
    required: set[str] = set()
    optional: set[str] = set()
    unresolved: set[str] = set()
    stack: list[tuple[str, bool]] = [(r["id"], r.get("required", True)) for r in refs
                                    if isinstance(r, dict) and isinstance(r.get("id"), str)]
    seen_tags: set[str] = set()
    while stack:
        ref, req = stack.pop()
        if ref.startswith("#"):
            tag_id = ref[1:]
            if tag_id in seen_tags:
                continue
            seen_tags.add(tag_id)
            entry = None
            if tag_id.startswith("minecraft:") and tag_id in VANILLA_BIOME_TAGS:
                entry = (VANILLA_BIOME_TAGS[tag_id], "builtin-vanilla-tags")
            if entry is None:
                entry = biome_tags.get(tag_id)
            if entry is None:
                entry = VANILLA_BIOME_TAGS.get(tag_id)
                if entry is not None:
                    entry = (entry, "builtin-vanilla-tags")
            if entry is None:
                unresolved.add(ref)
                continue
            for member in entry[0]:
                stack.append((member["id"], req and member.get("required", True)))
        else:
            (required if req else optional).add(ref)
    return required, optional, unresolved


def is_custom_zone(biome_id: str) -> bool:
    return "custom_spawn" in biome_id


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract Cobblemon spawn data from the Cobbleverse modpack (Modrinth).")
    ap.add_argument("--version", default=None, help="cobbleverse version, e.g. 1.7.42 (default: latest)")
    ap.add_argument("--data-dir", default="data", help="cache directory for downloads (default: data)")
    ap.add_argument("--out-dir", default="output", help="directory for generated JSON (default: output)")
    ap.add_argument("--scan-all-mods", action="store_true",
                    help="download+scan every locked mod jar, not just likely candidates")
    args = ap.parse_args()

    data_dir, out_root = Path(args.data_dir), Path(args.out_dir)

    print("[1/5] resolving cobbleverse version via Modrinth API")
    pack = get_pack_version(args.version)
    print(f"      version {pack['version_number']} ({pack['size'] / 1e6:.0f} MB, "
          f"published {pack['date_published']})")

    print("[2/5] downloading modpack + candidate mods (cached, sha1-verified)")
    mrpack_path = ensure_downloaded(
        pack["url"], pack["sha1"],
        data_dir / "mrpacks" / pack["filename"].replace(" ", "-"))
    sources, skipped = load_sources(mrpack_path, args.scan_all_mods, data_dir)
    if not sources:
        sys.exit("no candidate archives found")
    print(f"      {len(sources)} archives, {len(skipped)} locked files skipped (name filter)")

    print("[3/5] reading spawn data (only relevant JSON entries, nothing unpacked)")
    pools, biome_tags, presets, spawner_cfgs, per_source = collect_sources(sources)
    for s in sources:
        stats = per_source.get(s["label"], {})
        bits = [f"{v} {k}" for k, v in stats.items()]
        print(f"      {'datapack' if s['tier'] == 2 else 'mod     '}  {s['label']}: "
              f"{', '.join(bits) if bits else 'nothing spawn-related'}")

    disabled_pools = sorted(p for p, (d, _l) in pools.items() if not d.get("enabled", True))

    bucket_weights = dict(DEFAULT_BUCKET_WEIGHTS)
    bucket_weights_source = "built-in defaults (no spawner config found)"
    # Priority: packed config override (mrpack) > spawner config shipped in archives
    with zipfile.ZipFile(mrpack_path) as z:
        if "overrides/config/cobblemon/spawning/best-spawner-config.json" in z.namelist():
            cfg = json.loads(z.read("overrides/config/cobblemon/spawning/best-spawner-config.json"))
            for b in cfg.get("buckets") or []:
                if isinstance(b, dict) and "name" in b and "weight" in b:
                    bucket_weights[b["name"]] = b["weight"]
            bucket_weights_source = "mrpack: overrides/config/cobblemon/spawning/best-spawner-config.json"
    if bucket_weights_source.startswith("built-in") and spawner_cfgs:
        preferred = "data/cobblemon/spawning/best-spawner-config.json"
        cfg_path = preferred if preferred in spawner_cfgs else sorted(spawner_cfgs)[0]
        cfg = spawner_cfgs[cfg_path][0]
        for b in cfg.get("buckets") or []:
            if isinstance(b, dict) and "name" in b and "weight" in b:
                bucket_weights[b["name"]] = b["weight"]
        bucket_weights_source = f"{cfg_path} (from {spawner_cfgs[cfg_path][1]})"

    print("[4/5] resolving biome tags -> locations")
    # ---- normalize spawn records from effective pools -------------------------
    records: list[dict] = []
    for path in sorted(pools):
        pool, source_label = pools[path]
        if not pool.get("enabled", True):
            continue
        base_name = path.rsplit("/", 1)[-1]
        m = re.match(r"^(\d{4})_", base_name)
        for entry in pool.get("spawns") or []:
            if not isinstance(entry, dict):
                continue
            rec = build_spawn_record(entry, f"spawn_pool_world/{base_name}", pool, source_label)
            rec["pool"] = base_name
            rec["pool_dex"] = int(m.group(1)) if m else None
            records.append(rec)

    # ---- resolve references -> locations --------------------------------------
    locations: dict[str, dict] = {}  # key -> {kind, entries}
    unresolved_tags: dict[str, set] = {}

    def add_location(key: str, kind: str, rec: dict) -> None:
        locations.setdefault(key, {"kind": kind, "entries": []})["entries"].append(rec)

    for rec in records:
        req, opt, unresolved = expand_biome_refs(rec["biome_refs"], biome_tags)
        rec["biomes"] = sorted(b for b in req if not is_custom_zone(b))
        rec["custom_zones"] = sorted(b for b in req if is_custom_zone(b))
        rec["optional_biomes"] = sorted(b for b in opt if not is_custom_zone(b))
        rec["optional_zones"] = sorted(b for b in opt if is_custom_zone(b))
        rec["unresolved_refs"] = sorted(unresolved)
        anti_biomes = (rec.get("anti_condition") or {}).get("biomes")
        if anti_biomes:
            areq, _aopt, aunres = expand_biome_refs(
                normalize_tag_members(anti_biomes), biome_tags)
            rec["except_biomes"] = sorted(areq)
            if aunres:
                rec["unresolved_refs"] = sorted(set(rec["unresolved_refs"]) | aunres)
        rec["conditions"] = condition_descriptors(rec)
        for b in rec["biomes"]:
            add_location(b, "biome", rec)
        for cz in rec["custom_zones"]:
            add_location(cz, "custom_zone", rec)
        for s in rec["structures"]:
            add_location(f"structure:{s}", "structure", rec)
        for u in sorted(unresolved):
            unresolved_tags.setdefault(u, set()).add(rec["spawn_id"])

    out_dir = out_root / f"{PROJECT_SLUG}-{pack['version_number']}"
    pokemon_dir = out_dir / "pokemon"
    pokemon_dir.mkdir(parents=True, exist_ok=True)

    # ---- (A) per-pokemon files =================================================
    by_pokemon: dict[str, dict] = {}
    for rec in records:
        for ref in rec["pokemon_refs"]:
            p = by_pokemon.setdefault(
                ref, {"dex": None, "locations": [], "sources": set()})
            if p["dex"] is None and rec["pool_dex"] is not None:
                p["dex"] = rec["pool_dex"]
            p["locations"].append(rec)
            p["sources"].add(rec["source"].split(" :: ")[0])
        if not rec["pokemon_refs"]:  # herds: map each member
            for member in rec["herd"]:
                ref = member["pokemon"]
                if not ref:
                    continue
                p = by_pokemon.setdefault(ref, {"dex": None, "locations": [], "sources": set()})
                if p["dex"] is None and rec["pool_dex"] is not None:
                    p["dex"] = rec["pool_dex"]
                p["locations"].append(rec)
                p["sources"].add(rec["source"].split(" :: ")[0])

    index_rows = []
    for ref in sorted(by_pokemon, key=lambda r: (
            by_pokemon[r]["dex"] is None, by_pokemon[r]["dex"] or 0, r.lower())):
        p = by_pokemon[ref]
        seen: set[tuple] = set()
        locs = []
        for rec in p["locations"]:
            key = (rec["spawn_id"], rec["source"])
            if key in seen:
                continue
            seen.add(key)
            locs.append({
                "spawn_id": rec["spawn_id"],
                "pool": rec["pool"],
                "bucket": rec["bucket"],
                "level": rec["level"],
                "weight": rec["weight"],
                "position": rec["position"],
                "presets": rec["presets"] or None,
                "biome_refs": rec["biome_refs"] or None,
                "biomes": rec["biomes"] or None,
                "custom_zones": rec["custom_zones"] or None,
                "optional_biomes": rec["optional_biomes"] or None,
                "structures": rec["structures"] or None,
                "condition": rec["cond"] or None,
                "weight_multipliers": rec["weight_multipliers"],
                "anti_condition": rec["anti_condition"],
                "except_biomes": rec.get("except_biomes"),
                "unresolved_refs": rec["unresolved_refs"] or None,
                "herd": rec["herd"] or None,
                "max_herd_size": rec["max_herd_size"],
                "requires_mods": rec["requires_mods"],
                "source": rec["source"],
            })
        doc = {
            "id": ref,
            "dex": p["dex"],
            "spawn_rules": len(locs),
            "biome_count": len({b for l in locs for b in (l["biomes"] or [])}),
            "locations": locs,
            "sources": sorted(p["sources"]),
        }
        (pokemon_dir / f"{sanitize_name(ref)}.json").write_text(
            json.dumps(clean(doc), separators=(",", ":")) + "\n")
        index_rows.append({
            "id": ref,
            "dex": p["dex"],
            "file": f"pokemon/{sanitize_name(ref)}.json",
            "rules": len(locs),
            "biomes": sorted({b for l in locs for b in (l["biomes"] or [])}),
        })
    (out_dir / "pokemon_index.json").write_text(
        json.dumps(clean(index_rows), separators=(",", ":")) + "\n")

    # ---- (C) raw spawn weights table ---------------------------------------------
    # location -> bucket -> [{pokemon, weight, conditions?}] — raw weights only;
    # the UI normalizes per world state by filtering rows with `conditions`.
    lookup: dict = {
        "bucket_weights": bucket_weights,
        "formula": ("P(row @ location, state S) = bucket_weights[b]/sum(bucket weights of buckets active at this location in state S)"
                    " * w(row,S)/sum(w(row',S) over rows active at this location in bucket b in state S)"
                    "  - S = concrete world state (day/night, weather, skylight, near-by blocks, ...);"
                    "  - a row is ACTIVE when every one of its `conditions` holds in S (world gates) and no anti_* excludes it;"
                    "  - w(row,S) = weight * product of `weight_mult` multipliers whose `when` object holds in S;"
                    "  - only active rows count in the denominator; weight 0 rows never roll.")
    }
    for key, loc in sorted(locations.items(), key=lambda kv: (kv[1]["kind"], kv[0])):
        by_bucket: dict[str, dict[tuple, dict]] = {}
        for rec in loc["entries"]:
            if rec["weight"] is None or rec["bucket"] is None:
                continue
            refs = rec["pokemon_refs"] or [h["pokemon"] for h in rec["herd"]]
            ck = json.dumps(rec.get("conditions") or [], sort_keys=True)
            slot = by_bucket.setdefault(rec["bucket"], {}).setdefault(
                (json.dumps(refs, sort_keys=True), ck),
                {"pokemon": refs[0] if len(refs) == 1 else refs,
                 "weight": 0.0,
                 "conditions": rec.get("conditions") or None})
            slot["weight"] = round(slot["weight"] + float(rec["weight"]), 6)
        lookup[key] = {b: list(d.values()) for b, d in sorted(by_bucket.items())}
    (out_dir / "spawn_weights.json").write_text(
        json.dumps(clean(lookup), separators=(",", ":")) + "\n")

    # ---- meta ===================================================================
    meta = {
        "pack": PROJECT_SLUG,
        "version": pack["version_number"],
        "modrinth_version_id": pack["version_id"],
        "modrinth_url": f"https://modrinth.com/modpack/{PROJECT_SLUG}/version/{pack['version_number']}",
        "mrpack": {"filename": mrpack_path.name, "url": pack["url"],
                   "sha1": pack["sha1"], "size": pack["size"]},
        "game_versions": pack["game_versions"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "pokemon": len(by_pokemon),
            "spawn_entries": len(records),
            "locations": len(locations),
            "biome_tags": len(biome_tags),
            "presets": len(presets),
            "unresolved_tags": len(unresolved_tags),
            "disabled_pools": len(disabled_pools),
        },
        "disabled_pools": disabled_pools,
        "bucket_weights": bucket_weights,
        "bucket_weights_source": bucket_weights_source,
        "probability_model": (
            "per location and per world state S: P(row, bucket b) = bucket_weights[b]/sum(active bucket weights at this location in state S)"
            " * w(row,S)/sum(w(row',S) over rows active at this location in bucket b in state S)"
            "  - S = concrete world state (day/night, weather, skylight, near-by blocks, ...)."
            "  - a row is ACTIVE at location L in state S when every gate in its `conditions` holds:"
            "    * `presets` - the active context must be in this list (a natural context = any plain spawn context;"
            "      rows tagged with a specific context like dungeon/vanilla_structure only roll in that detail);"
            "    * `world`   - all keys must hold (time, skylight, can_see_sky, raining, thundering, near_blocks, base_blocks,"
            "      y/x range, max_light, moon, slime_chunk, lure_min/lure_max, rod/bait/bobber, dimensions);"
            "    * no `anti_biomes`/`anti_structures`/`anti_world` of the row may exclude location L or state S."
            "  - `weight_mult` = weight multiplier whose `when` object must hold in state S (effective weight)."
            "  - `requires_mods` = row exists only if the mod is installed (install-time filter, not a runtime state)."
            "  - ONLY ACTIVE rows count in the denominator (rows for other states do not dilute the roll)."
            "  - `position` (grounded/surface/submerged) is placement context only (spawn Y-level) and never modifies the roll."
            "  - Weight 0 = event/trigger spawn (not rolled)."
            "  - 'chance per spawn roll, given the row's condition holds' is what helps you WHERE + WHEN to go hunt;"
            "    spawn_weights.json is the single odds source; `examples/spawn_odds.py` computes exactly this for any"
            "    pokémon (plain rows are sampled in a dry-day state; pools mix day- and night-gated pokémon, so"
            "    plain rows may differ slightly between day and night)."
        ),
        "sources": [{
            "label": s["label"], "tier": s["tier"], "kind": s["kind"],
            "sha1": s["sha1"], "url": s["url"], **per_source.get(s["label"], {}),
        } for s in sources],
        "skipped_locked_files": skipped,
        "scanned_all_mods": args.scan_all_mods,
        "notes": [
            "tier 2 (datapacks: pack overrides, locked datapacks/, and datapacks inside the mrpack) overrides tier 1 (mod jars) for the same pool path",
            "tier-2 locked files under datapacks/ are treated as installed datapacks (override layer)",
            "ids are raw identifiers; no display names are generated",
            "dex is derived from the pool filename prefix (e.g. 0001_); addon pools without a prefix have dex=null",
            "biomes = required members (active in the default install); optional_biomes = members with required:false, i.e. biomes from optional mods (Terralith, Biomes O' Plenty, Wythers, ...) that only exist if those mods are installed; optional biomes/zones are listed in the data, but their pools only matter once those mods are installed",
            "datapacks under overrides/datapacks/extra/ (Hoenn, Johto, Sinnoh, Terralith) are opt-in region add-ons but their tag files are still consulted for tag resolution; treat their biomes as optional",
            "vanilla biome tags (#minecraft:is_*) are resolved from built-in 1.21.1 definitions (mcmeta); unresolved_tags are the rest (mostly #c:* common tags and tags of uninstalled mods) and make a spawn's biome list incomplete",
            "weights 0 entries are trigger/event spawns (custom zones, story spawns) and carry no probability",
            "spawn_weights.json holds raw weights (NOT pre-normalized probabilities, since `conditions` make the active row set world-state-dependent): location -> bucket -> [{pokemon, weight, conditions?}]; bucket_weights + formula are in the same file; rows are grouped per distinct condition set (identical duplicates summed); conditions vocabulary: presets / world {{time: day|night|dusk, skylight:{min,max}, can_see_sky, raining, thundering, near_blocks:[ids], base_blocks, y:{min,max}, x:{min,max}, max_light, moon, slime_chunk, lure_min, lure_max, rod, bait, bobber, dimensions}} - bounds are objects {min,max} and may be one-sided (y:{max:48} = at most 48) - / weight_mult ({multiplier, when{{same world keys}}}) / requires_mods / anti_biomes (resolved; spawn excluded there) / anti_structures / anti_world (same world schema)",
            "spawn_weights.json is the single odds source; examples/spawn_odds.py is a reference odds calculator (any pokémon as CLI arg)",
        ],
    }
    (out_dir / "meta.json").write_text(json.dumps(clean(meta), separators=(",", ":")) + "\n")

    print(f"[5/5] wrote outputs to {out_dir}")
    print(f"      pokemon   : {len(by_pokemon)}")
    print(f"      locations : {len(locations)}")
    print(f"      unresolved tags: {len(unresolved_tags)}")
    if skipped:
        print(f"      skipped   : {len(skipped)} locked files (use --scan-all-mods to include them)")


if __name__ == "__main__":
    main()
