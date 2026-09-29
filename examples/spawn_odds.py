#!/usr/bin/env python3
"""
Spawn-odds finder: given a Pokemon (default: pumpkaboo), use ONLY
output/spawn_weights.json to answer: where does it appear, under what
conditions, and how likely per spawn roll given that condition holds?

Model (meta.json "probability_model"), simplified:
    P(row @ location, state S) =
        bucket_weights[bucket] / sum(bucket weights of buckets active at location in S)
        * weight(row,S) / sum(weight(row',S) over rows active at location, bucket b, in S)
    a row is active when its condition holds; unhandled/exotic condition keys
    (lure, dimensions, ...) are treated as satisfiable; weight-0 rows are
    event/trigger spawns - never rolled, but reported.

Rows are grouped by spawn rule (bucket + weight + condition set), so e.g.
53 identical biomes are one line with "+N more".

Usage:
    python3 examples/spawn_odds.py            # pumpkaboo
    python3 examples/spawn_odds.py zekrom
    python3 examples/spawn_odds.py unown      # prefix matches all forms
"""
import argparse
import json
import math
import pathlib


def base_state() -> dict:
    return {
        "time": "day", "raining": False, "thundering": False, "can_see_sky": True,
        "skylight": {"min": 8, "max": 15}, "moon": None, "slime_chunk": False, "fishing": False,
        "near_blocks": None, "context": None, "structures": None,
    }


def state_of_row(row: dict) -> dict:
    """The state in which this row rolls: neutral baseline pinned by the row's gates."""
    s = base_state()
    for c in row.get("conditions") or []:
        w = c.get("world") or {}
        for k in ("time", "skylight", "can_see_sky", "raining", "thundering",
                  "moon", "slime_chunk", "near_blocks"):
            if k in w:
                s[k] = w[k]
        if any(k in w for k in ("rod", "bait", "bobber", "lure_min", "lure_max")):
            s["fishing"] = True
        if "structures" in c:
            s["structures"] = c["structures"]
        if "presets" in c:
            specials = [p for p in c["presets"] if p != "natural"]
            if specials:
                s["context"] = specials[0].lower()
    return s


def gate_holds(w: dict, s: dict) -> bool:
    """Does a world gate hold in state s? Untouched keys count as satisfiable."""
    checks = {
        "time": lambda v: s.get("time") == v,
        "raining": lambda v: bool(s.get("raining")) is v,
        "thundering": lambda v: bool(s.get("thundering")) is v,
        "can_see_sky": lambda v: bool(s.get("can_see_sky")) is v,
        "skylight": lambda v: (s.get("skylight") or {}) == {k: val for k, val in (v or {}).items() if val is not None},
        "near_blocks": lambda v: list(s.get("near_blocks") or []) == list(v),
        "moon": lambda v: s.get("moon") == v,
        "slime_chunk": lambda v: bool(s.get("slime_chunk")) is v,
        "fishing": lambda v: bool(s.get("fishing")) is v,
    }
    for k, v in (w or {}).items():
        if k in ("y", "x", "base_blocks", "max_light"):
            continue  # spatial: where-info, always satisfiable
        fn = checks.get(k)
        if fn and not fn(v):
            return False
        if k in ("rod", "bait", "bobber") and not s.get("fishing"):
            return False
    return True


def active_in(row: dict, s: dict, loc: str) -> bool:
    for c in row.get("conditions") or []:
        if "anti_biomes" in c and loc in c["anti_biomes"]:
            return False
        if "anti_structures" in c and s.get("structures") == c["anti_structures"]:
            return False
        if "anti_world" in c and not gate_holds(c["anti_world"], s):
            return False
        if "presets" in c:
            specials = [p for p in c["presets"] if p != "natural"]
            if specials:
                if s.get("context") not in [x.lower() for x in specials]:
                    return False
            elif s.get("context") is not None:
                return False
        if "world" in c and not gate_holds(c["world"], s):
            return False
        if "structures" in c and s.get("structures") and list(s["structures"]) != list(c["structures"]):
            return False
    return True


def effective(row: dict, s: dict) -> float:
    w = float(row["weight"])
    for c in row.get("conditions") or []:
        if wm := c.get("weight_mult"):
            if gate_holds(wm.get("when") or {}, s):
                w *= float(wm["multiplier"])
    return w


def bounds(prefix: str, r) -> str:
    """Render a {min,max} bounds object (possibly one-sided) as text."""
    if isinstance(r, dict):
        mn, mx = r.get("min"), r.get("max")
        if mn is not None and mx is not None:
            return f"{prefix} {mn}..{mx}"
        if mn is not None:
            return f"{prefix} >= {mn}"
        if mx is not None:
            return f"{prefix} <= {mx}"
    return ""


def describe(row: dict) -> str:
    parts = []
    for c in row.get("conditions") or []:
        w = c.get("world") or {}
        if t := w.get("time"):
            parts.append(f"{t} only" if t != "day" else "day only")
        if w.get("can_see_sky") is False:
            parts.append("indoor")
        if b := bounds("skylight", w.get("skylight")):
            parts.append(b)
        if w.get("raining") is True:
            parts.append("raining")
        if w.get("raining") is False:
            parts.append("not raining")
        if w.get("thundering"):
            parts.append("thunderstorm")
        if w.get("near_blocks"):
            parts.append("near " + ", ".join(w["near_blocks"]))
        if b := bounds("y", w.get("y")):
            parts.append(b)
        if b := bounds("x", w.get("x")):
            parts.append(b)
        if w.get("max_light"):
            parts.append(f"light <= {w['max_light']}")
        if w.get("base_blocks"):
            parts.append("on " + ", ".join(w["base_blocks"]))
        if w.get("moon"):
            parts.append(f"moon {w['moon'] or ''}".strip())
        if w.get("slime_chunk"):
            parts.append("slime chunk")
        if w.get("lure_min") or w.get("lure_max"):
            parts.append(f"lure {w.get('lure_min') or '..'}-{w.get('lure_max') or 'max'}")
        if any(k in w for k in ("rod", "bait", "bobber")):
            parts.append("fishing")
        if specials := [p for p in (c.get("presets") or []) if p != "natural"]:
            parts.append("in " + "/".join(specials) + " context")
        if wm := c.get("weight_mult"):
            wh = wm.get("when") or {}
            bits = []
            if t := wh.get("time"):
                bits.append(f"at {t}")
            if b := bounds("skylight", wh.get("skylight")):
                bits.append(b)
            if wh.get("raining") is True:
                bits.append("when raining")
            if wh.get("raining") is False:
                bits.append("when clear")
            if wh.get("thundering"):
                bits.append("during thunderstorm")
            if wh.get("near_blocks"):
                bits.append("near " + ", ".join(wh["near_blocks"]))
            if wh.get("lure_min"):
                bits.append(f"lure >= {wh['lure_min']}")
            parts.append(f"x{wm['multiplier']} " + (" ".join(bits) if bits else "always"))
        if "requires_mods" in c:
            parts.append("needs " + ", ".join(c["requires_mods"]))
    return "; ".join(parts) or "no conditions (plain)"


def short_name(loc: str) -> str:
    return loc.split(":", 1)[1] if loc.startswith(("minecraft:", "cobbleverse:")) else loc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pokemon", nargs="?", default="pumpkaboo")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    q = args.pokemon.lower()

    root = pathlib.Path(__file__).resolve().parent.parent
    d = next(root.glob("output/cobbleverse-*"))
    lookup = json.loads((d / "spawn_weights.json").read_text())
    bw = lookup["bucket_weights"]
    locs = {k: v for k, v in lookup.items()
            if isinstance(v, dict) and all(isinstance(x, list) for x in v.values())}

    def names_of(r):
        return r["pokemon"] if isinstance(r["pokemon"], list) else [r["pokemon"]]

    def hit(r):
        return any(q in str(n).lower() for n in names_of(r))

    rolled: list[tuple[str, str, dict]] = []
    events: list[tuple[str, str, dict]] = []
    for loc, buckets in locs.items():
        for b, rs in buckets.items():
            for r in rs:
                if not hit(r):
                    continue
                (events if r.get("weight") in (0, 0.0) else rolled).append((loc, b, r))
    if not rolled and not events:
        raise SystemExit(f"{args.pokemon}: no spawn data found")

    # P per rolled row, evaluated in the state the row's own condition describes
    scored: list[tuple[float, str, str, dict]] = []
    for loc, b, r in rolled:
        s = state_of_row(r)
        if not active_in(r, s, loc):
            continue
        tot: dict[str, float] = {}
        for bb, rs2 in locs[loc].items():
            for r2 in rs2:
                if r2.get("weight") in (0, 0.0) or not active_in(r2, s, loc):
                    continue
                tot[bb] = tot.get(bb, 0.0) + effective(r2, s)
        act = {bb: v for bb, v in tot.items() if v > 0}
        bw_sum = sum(bw.get(bb, 0.0) for bb in act)
        if bw_sum <= 0 or b not in act:
            continue
        p = (bw.get(b, 0.0) / bw_sum) * (effective(r, s) / act[b])
        scored.append((p, loc, b, r))

    # group by spawn rule: bucket + weight + condition set
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for item in scored:
        b, r = item[2], item[3]
        key = (b, str(r["weight"]), json.dumps(r.get("conditions") or [], sort_keys=True))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    order.sort(key=lambda k: -max(m[0] for m in groups[k]))

    print(f"== {args.pokemon}: {len(scored)} rolled row(s), {len(order)} spawn rule(s) ==")
    print(f"{'P per roll (when condition holds)':>30}  locations - rule")
    print("-" * 112)
    for key in order[: args.limit]:
        b, wstr, _ = key
        members = groups[key]
        ps = [m[0] for m in members]
        lo, hi = min(ps), max(ps)
        pstr = f"{hi:.8f}" if lo == hi else f"{lo:.6f} - {hi:.6f}"
        nin = (f"1 in {math.ceil(round(1 / hi)):,}" if lo == hi
               else f"1 in {math.ceil(round(1 / hi)):,}-{math.ceil(round(1 / lo)):,}")
        per_loc = {m[1]: m[0] for m in members}
        best = sorted(per_loc.items(), key=lambda kv: -kv[1])
        per = len(members) // len(best)
        top = "   ".join(f"{short_name(l)}  {math.ceil(round(1 / p)):,}" for l, p in best[:10])
        rest = f"   +{len(best) - 10} more" if len(best) > 10 else ""
        print(f"{pstr:>30}  ({nin})  {len(best)} location(s) [{b}] - {describe(members[0][3])}")
        herd = f"   [x{per} rows per location]" if per > 1 else ""
        print(f"{'':>32}best: {top}{rest}{herd}")
        if per > 1:
            btotal = max(per_loc.values()) * per
            print(f"{'':>30}  total chance at best location ≈ 1 in {math.ceil(round(1 / btotal)):,} (all rows of this rule)")
    if len(order) > args.limit:
        print(f"... {len(order) - args.limit} more rule(s) (use --limit)")

    if events:
        ev_locs, ev_rules = set(), []
        for loc, b, r in events:
            ev_locs.add(loc)
            ev_rules.append((b, describe(r)))
        shown = ", ".join(sorted(ev_locs)[:4]) + (f" +{len(ev_locs) - 4} more" if len(ev_locs) > 4 else "")
        print(f"\nevent/trigger spawns (weight 0, not rolled - custom encounters): {len(events)} in {shown}")
        for b, desc in ev_rules[:3]:
            print(f"  [{b}] {desc}")
        if len(ev_rules) > 3:
            print(f"  +{len(ev_rules) - 3} more")

    if scored:
        top = groups[order[0]]
        per_loc = {m[1]: m[0] for m in top}
        ranked = sorted(per_loc.items(), key=lambda kv: -kv[1])
        top3 = ", ".join(f"{short_name(l)} ({math.ceil(round(1 / p)):,})" for l, p in ranked[:10])
        more = f"   +{len(ranked) - 10} more" if len(ranked) > 10 else ""
        print(f"\nbest rule: {describe(top[0][3])}")
        print(f"  top spots: {top3}{more}")
    elif events:
        print("\nthis Pokemon only appears via event/trigger spawns (see above).")


if __name__ == "__main__":
    main()
