#!/usr/bin/env python3
"""
Drop-odds finder: given a Pokemon (default: pumpkaboo), use ONLY
output/drops.json to answer: what items does it drop when killed, and what
does it produce while pastured - and how often?

Model (drops.json "drop_model"), verified against the Cobblemon bytecode
(DropTable/ItemDropEntry) and the pastureLoot mixin:
  kill:   `amount` drop budget; per slot the first (list order) entry whose
          percentage% roll passes is picked (percentage default 100), each
          entry at most once per trigger, a slot where nobody passes still
          spends one budget point; each picked entry then drops a random
          count in quantity_range (default 1; min 0 = sometimes nothing).
  pasture: same table, per pasture pokemon, but only when the per-minute roll
          (`chance_per_minute`) passes, and items in `item_blacklist` never drop.

The selection loop is stateful, so odds are estimated by Monte-Carlo with a
deterministic seed (200k triggers by default).

Usage:
    python3 examples/drop_odds.py              # pumpkaboo
    python3 examples/drop_odds.py pikachu
    python3 examples/drop_odds.py unown        # prefix: all forms
    python3 examples/drop_odds.py --trials 500000
"""
import argparse
import json
import math
import pathlib
import random


def amount_range(drops: dict) -> tuple[int, int]:
    amount = drops.get("amount")
    if isinstance(amount, dict):  # one-sided bounds objects
        return amount.get("min", 1), amount.get("max", amount.get("min", 1))
    n = max(1, int(amount or 1))
    return n, n


def bounds_of(row: dict) -> tuple[int, int]:
    r = row.get("quantity_range")
    if r is None:
        return 1, 1
    return r.get("min", 1), r.get("max", 1)


def fmt_qty(row: dict) -> str:
    """Human-readable drop quantity range ('2-4', '0-1', '1') for a table entry."""
    r = row.get("quantity_range")
    if not r:
        return "1"
    lo, hi = r.get("min"), r.get("max")
    if lo is not None and hi is not None:
        return f"{lo}-{hi}" if lo != hi else f"{lo}"
    if lo is not None:  # one-sided bounds: {min} alone = at least min
        return f"{lo}+"
    return f"<={hi}"


def simulate_triggers(drops: dict, n: int, rng: random.Random,
                      blacklist: frozenset[str] | None = None) -> dict:
    """Run n triggers; return {item: [selected_times, dropped_count]}.

    Faithful to DropTable.getDrops: `amount` budget; per slot the first
    (list-order) entry passing its percentage% roll is picked (each at most
    once per trigger); a slot where nobody passes still spends one budget
    point; then each picked entry drops a random count in its quantity_range."""
    lo_amt, hi_amt = amount_range(drops)
    # (item, chance%, budget-cost, (qty_lo, qty_hi), table row); budget-cost = min(qty) floor 1
    pool = [(e["item"], float(e.get("percentage", 100.0)),
             max(1, int(bounds_of(e)[0])), bounds_of(e), e) for e in drops["entries"]]
    out: dict[str, tuple[str, tuple[int, int]]] = {}  # item -> (qty_range_str, [times, count])
    for _ in range(n):
        slots = rng.randint(lo_amt, hi_amt)
        candidates = list(pool)
        selected: list[str] = []
        while slots > 0 and candidates:
            hit = None
            for row in candidates:
                if rng.random() * 100.0 < row[1]:
                    hit = row
                    break
            if hit is None:
                slots -= 1  # failing slots still spend budget
                continue
            candidates.remove(hit)
            selected.append(hit[0])
            slots -= hit[2]
        for item in selected:
            if blacklist and item in blacklist:
                continue
            row = next(r for r in pool if r[0] == item)
            lo, hi = row[3]
            q = hi if hi == lo else rng.randint(lo, hi)
            slot = out.setdefault(item, (fmt_qty(row[4]), [0, 0]))
            slot[1][0] += 1
            slot[1][1] += q
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pokemon", nargs="?", default="pumpkaboo")
    ap.add_argument("--trials", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()
    q = args.pokemon.lower()

    root = pathlib.Path(__file__).resolve().parent.parent
    d = next(root.glob("output/cobbleverse-*"))
    doc = json.loads((d / "drops.json").read_text())
    table = doc.get("pokemon") or {}
    hits = {k: v for k, v in table.items() if q in k.lower()}
    if not hits:
        raise SystemExit(f"{args.pokemon}: no drop data found")

    pasture = doc.get("pasture") or {}
    cpmin = pasture.get("chance_per_minute")
    blacklist = frozenset(pasture.get("item_blacklist") or [])

    for sid, drops in sorted(hits.items(), key=lambda kv: (kv[1].get("dex") or 99999)):
        if not drops["entries"]:
            continue
        n = args.trials
        kill = simulate_triggers(
            drops, n, random.Random(args.seed + sum(map(ord, "kill" + sid))))
        print(f"== {sid} (#{drops.get('dex')}) ==")
        print("  kill (per fainted pokemon) — probability per trigger; qty = items dropped each time it rolls")
        for item, (qty, (times, count)) in sorted(kill.items(), key=lambda kv: -kv[1][1][1]):
            p = times / n
            inv = math.ceil(round(1 / p)) if p > 0 else float("inf")
            print(f"    {item:<36} {qty:>6} x   {p * 100:7.3f}%   (1 in {inv:,})   avg {count / n:.3f} each")
        if not kill:
            print("    (none)")

        if cpmin is not None:
            past = simulate_triggers(
                drops, n, random.Random(args.seed + sum(map(ord, "past" + sid))), blacklist)
            excl = [e["item"] for e in drops["entries"] if e["item"] in blacklist]
            print(f"  pasture (per minute, chance {cpmin:g}; {len(blacklist)} blacklisted items) — "
                  f"'x in 7 min' = expected ITEMS in a 7-minute window:")
            for item, (qty, (times, count)) in sorted(past.items(), key=lambda kv: -kv[1][1][1]):
                p = cpmin * times / n
                per7 = 7 * p * (count / times if times else 0)
                print(f"    {item:<36} {qty:>6} x   {p * 100:7.4f}%   ~{per7:.1f} in 7 min   "
                      f"~{60 * cpmin * count / n:.1f} items/hour")
            if not past:
                print("    (none - all of its items are pasture-blacklisted)")
            if excl:
                print(f"    pasture-excluded: {', '.join(excl)}")
        print(f"  source: {drops['source'].split(' :: ')[0]}")
        print()


if __name__ == "__main__":
    main()
