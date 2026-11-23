"""Theory-crafting engine: simulates a champion with a given build against a concrete enemy team.

Model (deliberately simple and transparent, numbers come from the game files):
  * Offense: damage dealt in a short fight window (default 6 s) to every enemy, as a share of
    that enemy's health: abilities on cooldown (ability haste), basic attacks with attack speed,
    crit, on-hit effects, %-health damage, penetration vs. the enemy's armor / magic resist.
  * Defense: effective health against the enemy team's actual damage mix (physical / magic / true)
    plus shields and healing during the window.
  * Score = offense x defense (Lanchester: winning a fight scales with both).
  * Jungle: full-clear time against the real camp stats.
Item passives are modelled one by one in ITEM_MODELS with the values of the current patch.
"""
import math

WINDOW = 6.0
BURST = 3.0
MIN_COS = 0.85
DEF_EXP = 0.3   # weight of survivability vs. damage (calibrated against real games, see calibrate())
CHECKPOINTS = [(1, 9), (2, 11), (3, 13), (4, 15), (5, 17)]  # (items, level)
CHECK_WEIGHT = [1.0, 1.0, 1.0, 0.7, 0.5]
UNIQUE = [  # only one item per group may be built
    {"3074", "3748", "6698", "6631"},           # hydra / cleave
    {"3036", "6694", "3033"},                   # last whisper
    {"3053", "3156", "6673"},                   # lifeline
    {"3135", "3137"},                           # blight
    {"3078", "3100", "6662", "3508"},           # spellblade
    {"3003", "3004"},                           # mana charge
]
SUPPORT_TAGS = {"GoldPer"}


def growth(base, per, level):
    return base + per * (level - 1) * (0.7025 + 0.0175 * (level - 1))


class Stats(dict):
    def __getattr__(self, k):
        return self.get(k, 0.0)


def champ_stats(c, level, items, item_db):
    s = c["stats"]
    st = Stats(level=level, base_ad=growth(s["ad"], s["ad_l"], level), hp=growth(s["hp"], s["hp_l"], level),
               armor=growth(s["armor"], s["armor_l"], level), mr=growth(s["mr"], s["mr_l"], level),
               bad=0.0, ap=0.0, bhp=0.0, barmor=0.0, bmr=0.0, ah=0.0, crit=0.0, crit_dmg=0.0, lethality=0.0,
               apen_pct=0.0, mpen=0.0, mpen_pct=0.0, lifesteal=0.0, omnivamp=0.0, heal_amp=0.0)
    bas = s["as_l"] * (level - 1) * (0.7025 + 0.0175 * (level - 1))
    for iid in items:
        it = item_db[iid]
        for k, v in it["stats"].items():
            if k == "ad":
                st["bad"] += v
            elif k == "hp":
                st["bhp"] += v
            elif k == "armor":
                st["barmor"] += v
            elif k == "mr":
                st["bmr"] += v
            elif k == "as":
                bas += v
            elif k in ("apen_pct", "mpen_pct"):
                st[k] = 1 - (1 - st[k]) * (1 - v / 100.0)
            else:
                st[k] = st.get(k, 0.0) + v
    st["bas"] = bas
    st["as"] = min(2.5, s["as"] + s.get("as_ratio", s["as"]) * bas / 100.0)
    st["crit"] = min(100.0, st["crit"]) / 100.0
    st["crit_mult"] = 1.75 + st["crit_dmg"]
    return st


def finalize(st, c, items, item_db):
    for iid in items:  # stat-transforming passives
        dv = item_db[iid]["dv"]
        if iid == "3089":
            st["ap"] *= 1 + dv.get("APAmp", 0.3)
        elif iid == "2501":
            st["bad"] += dv.get("HPToADPercentage", 0.025) * st["bhp"]
        elif iid == "4633":
            st["ap"] += dv.get("HealthToAPConversionPercent", 0.02) * st["bhp"]
        elif iid == "3003":
            st["ap"] += dv.get("APFromMana", 0.01) * 1000
        elif iid == "3004":
            st["bad"] += 0.025 * 1000
        elif iid == "6665":
            st["barmor"] += dv.get("BonusResistPercentage", 0.3) * st["barmor"]
            st["bmr"] += dv.get("BonusResistPercentage", 0.3) * st["bmr"]
        elif iid == "3084":
            st["bhp"] += 250  # stacks gained by mid game
        elif iid == "4401":
            st["bmr"] += dv.get("BonusMagicResist", 70) * 0.6
        elif iid == "3161":
            st["ah"] += dv.get("AHBase", 25) * 0.5
    st["ad"] = st["base_ad"] + st["bad"]
    st["max_hp"] = st["hp"] + st["bhp"]
    st["tarmor"] = st["armor"] + st["barmor"]
    st["tmr"] = st["mr"] + st["bmr"]
    st["cdm"] = 100.0 / (100.0 + st["ah"])
    return st


def form_value(form, st):
    v = form.get("base", 0.0)
    for k, coef in form.items():
        if k == "base":
            continue
        key = {"ap": "ap", "bap": "ap", "ad": "ad", "bad": "bad", "base_ad": "base_ad", "hp": "max_hp", "bhp": "bhp",
               "base_hp": "hp", "armor": "tarmor", "barmor": "barmor", "mr": "tmr", "bmr": "bmr",
               "as": "as", "bas": "bas", "ms": "ms"}.get(k)
        if key:
            v += coef * st.get(key, 0.0)
    return v


def ranks(level):
    """Typical skill order: R at 6/11/16, main ability maxed first, then second, then third."""
    r = (1 + (level >= 11) + (level >= 16)) if level >= 6 else 0
    pts = level - r
    first = min(5, max(1, pts - 2))
    second = min(5, max(1, pts - first - 1))
    third = min(5, max(1, pts - first - second))
    return first, second, third, r


def ability_order(c):
    """Main ability = highest base damage per cooldown at rank 5 (Q/W/E)."""
    score = {}
    for slot in "QWE":
        a = c["abilities"].get(slot, {})
        dmg = sum(max(x["f"].get("5:13", {}).get("base", 0), 0) for x in a.get("dmg", []))
        cd = (a.get("cd") or [10] * 6)[min(5, len(a.get("cd") or [10]) - 1)] or 10
        score[slot] = dmg / max(cd, 0.5)
    return sorted("QWE", key=lambda s: -score[s])


def lvl_key(level, levels):
    return min(levels, key=lambda lv: abs(lv - level))


# ------------------------------------------------------------------ enemies
ENEMY_ITEM = {  # bonus defensive stats per completed item, by class
    "Tank": (330, 30, 28), "Fighter": (230, 16, 14), "Assassin": (90, 6, 6), "Mage": (90, 6, 8),
    "Marksman": (60, 5, 5), "Support": (150, 12, 12),
}


REAL_ENEMY_ITEMS = {}  # champion name -> list of item ids actually owned (live game); set by best_build


def enemy_target(c, level, n_items, item_db=None):
    s = c["stats"]
    real = REAL_ENEMY_ITEMS.get(c["name"])
    if real is not None and item_db is not None:
        hp = sum(item_db[i]["stats"].get("hp", 0) for i in real if i in item_db)
        ar = sum(item_db[i]["stats"].get("armor", 0) for i in real if i in item_db)
        mr = sum(item_db[i]["stats"].get("mr", 0) for i in real if i in item_db)
        return {"hp": growth(s["hp"], s["hp_l"], level) + hp, "armor": growth(s["armor"], s["armor_l"], level) + ar,
                "mr": growth(s["mr"], s["mr_l"], level) + mr, "bonus_armor": ar, "bonus_mr": mr, "name": c["name"]}
    cls = (c.get("tags") or ["Fighter"])[0]
    hp_i, ar_i, mr_i = ENEMY_ITEM.get(cls, ENEMY_ITEM["Fighter"])
    return {"hp": growth(s["hp"], s["hp_l"], level) + hp_i * n_items,
            "armor": growth(s["armor"], s["armor_l"], level) + ar_i * n_items,
            "mr": growth(s["mr"], s["mr_l"], level) + mr_i * n_items,
            "bonus_armor": ar_i * n_items, "bonus_mr": mr_i * n_items, "name": c["name"]}


def damage_profile(c, levels):
    """Share of physical / magic / true damage a champion deals (mid game, generic build)."""
    lk = str(lvl_key(11, levels))
    tot = {"physical": 0.0, "magic": 0.0, "true": 0.0}
    st = {"ap": 120, "bad": 60, "ad": growth(c["stats"]["ad"], c["stats"]["ad_l"], 11) + 60, "max_hp": 2200,
          "bhp": 400, "base_ad": growth(c["stats"]["ad"], c["stats"]["ad_l"], 11)}
    is_ap = c.get("partype") in ("Mana",) and "Mage" in c.get("tags", [])
    for slot, a in c["abilities"].items():
        rk = "3" if slot == "R" else "4"
        for p in a.get("dmg", []):
            f = p["f"].get(f"{rk}:{lk}") or {}
            v = 0.06 * 2500 if p["kind"] != "flat" else form_value(f, st)
            if p["kind"] != "flat":
                v = f.get("base", 0) * 2500
            tot[p["type"]] += max(v, 0)
    tot["physical"] += st["ad"] * (2.5 if not is_ap else 1.0) * 2
    s = sum(tot.values()) or 1
    return {k: v / s for k, v in tot.items()}


# ------------------------------------------------------------------ core simulation
def mitigate(dmg_type, amount, target, st, extra_apen=0.0):
    if dmg_type == "true":
        return amount
    if dmg_type == "physical":
        res = target["armor"] * (1 - extra_apen) * (1 - st["apen_pct"]) - st["lethality"]
    else:
        res = target["mr"] * (1 - st["mpen_pct"]) - st["mpen"]
    res = max(res, 0.0) if res > 0 else res
    return amount * (100.0 / (100.0 + res) if res >= 0 else 2 - 100.0 / (100.0 - res))


def simulate(c, st, items, item_db, target, levels, window=WINDOW, order=None, melee=True):
    """Damage dealt to one target in the fight window, split by source."""
    lv = st["level"]
    lk = str(lvl_key(lv, levels))
    order = order or ability_order(c)
    r1, r2, r3, rr = ranks(lv)
    rank = {order[0]: r1, order[1]: r2, order[2]: r3, "R": rr}
    out = {"physical": 0.0, "magic": 0.0, "true": 0.0}
    casts_total = 0
    amp = 1.0
    ids = set(items)
    if "4633" in ids:
        amp += item_db["4633"]["dv"].get("EternityDamageIncreaseMax", 0.08) * 0.75
    if "6653" in ids:
        amp += item_db["6653"]["dv"].get("DamageIncreaseMax", 0.06) * 0.7
    spell_amp = 1.0 + (0.03 * 3 if "3161" in ids else 0.0)
    if "4645" in ids:
        spell_amp += 0.2 * 0.35  # bonus damage vs. targets below 40 % health
    extra_apen = 0.0
    if "3071" in ids:
        dv = item_db["3071"]["dv"]
        extra_apen = dv.get("ShredPerStack", 0.06) * dv.get("MaxStacks", 5) * (1 if melee else 0.5) * 0.8
    if "3302" in ids:
        extra_apen = max(extra_apen, item_db["3302"]["dv"].get("PenMax", 0.3) * 0.7)
    cast_time = 0.0
    for slot, a in c["abilities"].items():
        if slot == "P" or not a.get("dmg"):
            continue
        rk = rank.get(slot, 0)
        if rk <= 0:
            continue
        cds = a.get("cd") or [10.0] * 6
        cd = cds[min(rk, len(cds) - 1)] * st["cdm"]
        n = 1 if slot == "R" else 1 + int(max(0.0, window - 0.5) // max(cd, 0.75))
        casts_total += n
        cast_time += n * min(a.get("cast", 0.25) or 0.25, 0.5)
        parts = []
        for p in a["dmg"]:
            f = p["f"].get(f"{rk}:{lk}") or {}
            if p["kind"] == "flat":
                v = form_value(f, st)
            else:
                frac = form_value(f, st)
                hp = target["hp"] * {"pct_max": 1.0, "pct_cur": 0.7, "pct_missing": 0.35}[p["kind"]]
                v = frac * hp
            if p.get("dot"):
                v *= 2.0
            parts.append((p["type"], max(v, 0.0)))
        if not parts:
            continue
        parts.sort(key=lambda x: -x[1])
        for i, (t, v) in enumerate(parts):  # main hit counts fully, additional values half
            out[t] += mitigate(t, v * (1.0 if i == 0 else 0.5), target, st, extra_apen) * n * spell_amp
    # basic attacks
    n_auto = max(0.0, st["as"] * (window - cast_time))
    auto_mult = 1.5 if c["name"] == "Graves" else 1.0  # multi-bullet shotgun close range
    crit_factor = 1 + st["crit"] * (st["crit_mult"] - 1)
    out["physical"] += mitigate("physical", st["ad"] * crit_factor * auto_mult, target, st, extra_apen) * n_auto
    p = c["abilities"].get("P")
    if p and p.get("dmg"):
        for part in p["dmg"]:
            f = part["f"].get(f"1:{lk}") or part["f"].get(f"0:{lk}") or {}
            v = form_value(f, st) * (target["hp"] if part["kind"] != "flat" else 1)
            hits = n_auto if p.get("onhit") else 1.5
            out[part["type"]] += mitigate(part["type"], max(v, 0), target, st, extra_apen) * hits
    # item passives
    for iid in items:
        for t, v in item_damage(iid, item_db[iid]["dv"], st, target, n_auto, casts_total, melee, window).items():
            out[t] += mitigate(t, v, target, st, extra_apen)
    total = sum(out.values()) * amp
    return total, out, n_auto, casts_total


def item_damage(iid, dv, st, tgt, n_auto, casts, melee, window):
    g = dv.get
    sb = min(casts, n_auto, window / 3.0)  # spellblade procs need an attack after a cast
    m = {
        "3153": lambda: {"physical": n_auto * g("MeleeValue" if melee else "RangedValue", 0.09) * tgt["hp"] * 0.6},
        "6672": lambda: {"physical": (n_auto / g("AttackCount", 3)) * (150 + 0.4 * st["bad"]) * (1 if melee else 0.8) * 1.4},
        "3078": lambda: {"physical": sb * g("SpellbladeMultiplier", 2.0) * st["base_ad"]},
        "3100": lambda: {"magic": sb * (g("SpellbladeADRatio", 0.75) * st["base_ad"] + g("LichBaneAPValue", 0.45) * st["ap"])},
        "6662": lambda: {"physical": sb * g("SpellbladeMultiplier", 1.5) * st["base_ad"]},
        "3508": lambda: {"physical": sb * g("BaseADRatio", 1.25) * st["base_ad"]},
        "3115": lambda: {"magic": n_auto * (g("NashorsBaseValue", 15) + g("NashorsAPValue", 0.15) * st["ap"])},
        "3091": lambda: {"magic": n_auto * 45},
        "3124": lambda: {"magic": n_auto * g("OnHitDamage", 30) * 1.33},
        "3302": lambda: {"magic": n_auto * 30},
        "6653": lambda: {"magic": min(casts + 1, 3) * g("BurnPercentHealthDamage", 0.02) * tgt["hp"] * g("BurnDuration", 3)},
        "2503": lambda: {"magic": (g("BurnFlatDamagePerSecond", 20) + g("APRatio", 0.02) * st["ap"]) * g("BurnDuration", 3) * min(casts, 3)},
        "3748": lambda: {"physical": n_auto * g("PrimaryTargetHPRatio", 0.01) * st["max_hp"] * (1 if melee else 0.5)
                         + g("ActivePrimaryTargetHPRatio", 0.04) * st["max_hp"]},
        "3074": lambda: {"physical": g("ActiveADRatio", 0.8) * st["ad"]},
        "6698": lambda: {"physical": 0.5 * st["ad"]},
        "6631": lambda: {"physical": g("ADRatio", 0.8) * st["ad"]},
        "6692": lambda: {"physical": g("MeleePercMaxHP", 0.08) * tgt["hp"] * (1 if melee else 0.625)},
        "6655": lambda: {"magic": g("BaseDamage", 75) * 1.8 + g("APRatio", 0.05) * st["ap"] * 6},
        "4646": lambda: {"magic": g("BaseDamage", 125) + g("APRatio", 0.1) * st["ap"]},
        "3152": lambda: {"magic": g("BaseDamage", 135) + g("APRatio", 0.15) * st["ap"]},
        "3068": lambda: {"magic": window * (20 + 0.015 * st["bhp"])},
        "6664": lambda: {"magic": window * (10 + 0.0175 * st["bhp"])},
        "3084": lambda: {"physical": g("BaseDamage", 70) + g("MaxHPRatio", 0.06) * st["bhp"]},
        "3181": lambda: {"physical": 0.25 * n_auto * 0.2 * st["base_ad"]},
        "3087": lambda: {"magic": g("ChainDamage", 60) * 1.5},
        "3094": lambda: {"magic": g("BonusDamage", 40)},
        "6699": lambda: {"physical": g("PercentCurrentHPMelee" if melee else "PercentCurrentHPRanged", 8) / 100 * tgt["hp"] * 0.8},
        "3036": lambda: {"physical": 0.08 * st["ad"] * n_auto},
        "6676": lambda: {"true": g("ExecuteThreshold", 0.05) * tgt["hp"]},
        "6697": lambda: {"physical": 0.15 * st["bad"] * 2},
        "3142": lambda: {"physical": 0.12 * st["ad"] * n_auto * 0.5},
        "2512": lambda: {"true": n_auto * 0.3 * g("BonusTrueDamage", 0.15) * st["ad"]},
        "6610": lambda: {"physical": st["ad"] * (st["crit_mult"] - 1) * min(1, n_auto)},
        "3179": lambda: {},
        "2520": lambda: {"physical": 0.1 * st["bad"] * 3},
        "3742": lambda: {"physical": g("BonusDamagePerStack", 0.4) * 100 * 0.5 + st["base_ad"] * 0.5},
        "3075": lambda: {"magic": g("BaseDamage", 20) + g("BonusArmorDamageRatio", 0.1) * st["barmor"]},
        "2502": lambda: {"magic": (window / g("Cooldown", 4)) * g("BonusHealthDrainPercentage", 0.03) * st["bhp"]},
        "3065": lambda: {},
    }
    f = m.get(iid)
    return f() if f else {}


def item_defense(iid, dv, st):
    """Extra effective health from an item passive (shields, revives, damage reduction)."""
    g = dv.get
    return {
        "3053": g("BaseShieldRatio", 0.6) * st["bhp"] * 0.8,
        "3156": g("ShieldSize", 200) + g("ShieldADScaling", 1.5) * st["bad"] * 0.5,
        "6673": 300 + 0.6 * st["bad"],
        "3026": 0.12 * st["max_hp"],
        "3157": 0.12 * st["max_hp"],
        "6333": 0.18 * st["max_hp"],
        "2504": g("ShieldAmount", 0.15) * st["max_hp"] * 0.6,
        "6692": g("MeleeBaseShield", 150) + g("MeleeBonusADShieldRatio", 0.4) * st["bad"],
        "3102": 0.12 * st["max_hp"],
        "3143": 0.06 * st["max_hp"],
        "2502": 0.1 * st["bhp"],
        "3065": 0.04 * st["max_hp"],
    }.get(iid, 0.0)


def defense(st, items, item_db, enemy_mix):
    ehp_mult = (enemy_mix.get("physical", 0.5) * 100 / (100 + st["tarmor"])
                + enemy_mix.get("magic", 0.4) * 100 / (100 + st["tmr"]) + enemy_mix.get("true", 0.1))
    shield = sum(item_defense(i, item_db[i]["dv"], st) for i in items)
    return (st["max_hp"] + shield) / max(ehp_mult, 0.05)


def evaluate(c, items, item_db, enemies, levels, level, enemy_items, enemy_mix=None, melee=True, order=None):
    st = finalize(champ_stats(c, level, items, item_db), c, items, item_db)
    targets = [enemy_target(e, level, enemy_items, item_db) for e in enemies]
    off = 0.0
    dealt = 0.0
    for t in targets:  # half burst (first 3 s), half extended fight
        dmg, _, _, _ = simulate(c, st, items, item_db, t, levels, melee=melee, order=order)
        burst, _, _, _ = simulate(c, st, items, item_db, t, levels, window=BURST, melee=melee, order=order)
        dealt += dmg
        off += 0.5 * dmg / t["hp"] + 0.5 * 2.0 * burst / t["hp"]
    off /= max(len(targets), 1)
    heal = (st["lifesteal"] / 100.0 + st["omnivamp"] / 100.0) * dealt / max(len(targets), 1) * 0.6
    if "3065" in items:
        heal *= 1.25
    if enemy_mix is None:
        enemy_mix = {"physical": 0.5, "magic": 0.4, "true": 0.1}
    dfn = defense(st, items, item_db, enemy_mix) + heal * 1.5
    return off, dfn, st


# ------------------------------------------------------------------ jungle clear
def clear_time(c, items, item_db, jungle, levels, level=4):
    st = finalize(champ_stats(c, level, items, item_db), c, items, item_db)
    total = 0.0
    smite_used = False
    for camp, mons in jungle["camps"].items():
        if camp == "scuttle":
            continue
        hp_total = sum(jungle["monsters"][m]["hp"] * n for m, n in mons.items())
        mons_list = [jungle["monsters"][m] for m in mons]
        armor = sum(m["armor"] for m in mons_list) / len(mons_list)
        mr = sum(m["mr"] for m in mons_list) / len(mons_list)
        tgt = {"hp": hp_total / max(sum(mons.values()), 1), "armor": armor, "mr": mr}
        dmg, parts, _, _ = simulate(c, st, items, item_db, tgt, levels, window=WINDOW)
        aoe = min(sum(mons.values()), 3)
        ability_share = 1 - (parts["physical"] * 0.5 / max(sum(parts.values()), 1))
        dps = dmg / WINDOW * (1 + (aoe - 1) * ability_share * 0.8)
        dps += 35  # jungle pet
        need = hp_total - (600 if not smite_used and camp in ("red", "blue") else 0)
        smite_used = smite_used or camp in ("red", "blue")
        total += need / max(dps, 1)
    return total


# ------------------------------------------------------------------ item class (what a champion builds)
STAT_GOLD = {"ad": 35, "ap": 20, "as": 25, "crit": 40, "lethality": 30, "apen_pct": 30, "mpen_pct": 25, "mpen": 30,
             "hp": 2.67, "armor": 20, "mr": 18, "ah": 26, "lifesteal": 40}
DIMS = ["ad", "ap", "as", "crit", "pen", "hp", "armor", "mr", "ah"]


def item_vector(it):
    v = dict.fromkeys(DIMS, 0.0)
    for k, x in it["stats"].items():
        g = STAT_GOLD.get(k, 0) * x
        if k in ("lethality", "apen_pct", "mpen_pct", "mpen"):
            v["pen"] += g
        elif k == "lifesteal":
            v["ad"] += g
        elif k in v:
            v[k] += g
    if "OnHit" in it.get("tags", []):
        v["as"] += 300
    return v


def _cos(a, b):
    num = sum(a[k] * b[k] for k in DIMS)
    den = math.sqrt(sum(x * x for x in a.values())) * math.sqrt(sum(x * x for x in b.values()))
    return num / den if den else 0.0


def kit_profile(c):
    """Fallback without game data: stat profile from the champion's own ability scalings and class."""
    v = dict.fromkeys(DIMS, 0.0)
    for slot, a in c["abilities"].items():
        for p in a.get("dmg", []):
            f = p["f"].get("5:13") or p["f"].get("3:13") or {}
            v["ap"] += 600 * (f.get("ap", 0) + f.get("bap", 0))
            v["ad"] += 1000 * (f.get("bad", 0) + 0.5 * f.get("ad", 0))
            v["hp"] += 300 * (f.get("bhp", 0) + f.get("hp", 0)) * 100
    tags = c.get("tags", [])
    if "Marksman" in tags:
        v["ad"] += 600; v["crit"] += 500; v["as"] += 400
    if "Tank" in tags:
        v["hp"] += 900; v["armor"] += 500; v["mr"] += 500
    if "Fighter" in tags:
        v["hp"] += 400; v["ah"] += 300
    if "Assassin" in tags:
        v["pen"] += 500
    return v


def champion_profile(item_db, popularity=None, c=None):
    """Average stat profile of what players build on this champion (falls back to the kit)."""
    if popularity and sum(popularity.values()) >= 30:
        v = dict.fromkeys(DIMS, 0.0)
        for iid, n in popularity.items():
            if iid in item_db:
                for k, x in item_vector(item_db[iid]).items():
                    v[k] += x * n
        return v
    return kit_profile(c)


def item_pool(item_db, c, popularity=None, min_cos=MIN_COS):
    prof = champion_profile(item_db, popularity, c)
    played = popularity or {}
    tot = sum(played.values()) or 1
    out = []
    for iid in candidate_items(item_db, c):
        sim = _cos(item_vector(item_db[iid]), prof)
        if sim >= min_cos or played.get(iid, 0) / tot >= 0.02:
            out.append(iid)
    return out


# ------------------------------------------------------------------ build search
def candidate_items(item_db, c):
    out = []
    for iid, it in item_db.items():
        if it["boots"] or SUPPORT_TAGS & set(it["tags"]):
            continue
        if it["gold"] < 2000:
            continue
        out.append(iid)
    return out


def boots_items(item_db):
    return [i for i, it in item_db.items() if it["boots"]]


def conflicts(a, b):
    return any(a in g and b in g for g in UNIQUE if g)


def score_build(c, items, item_db, enemies, levels, enemy_mix, melee, order=None, jungle=None, clear_ref=None):
    total = 0.0
    detail = []
    for (n, lv), w in zip(CHECKPOINTS, CHECK_WEIGHT):
        if len(items) < n:
            break
        off, dfn, _ = evaluate(c, items[:n], item_db, enemies, levels, lv, n, enemy_mix, melee, order)
        s = off * (dfn / 1000.0) ** DEF_EXP
        detail.append((n, off, dfn))
        total += w * s
    if jungle and clear_ref and items:
        t = clear_time(c, items[:1], item_db, jungle, levels, level=6)
        total *= (clear_ref / max(t, 1)) ** 0.35
    return total, detail


def best_build(c, item_db, enemies, levels, enemy_mix=None, melee=True, jungle=None, n_items=5, beam=24,
               pool=None, boots=None, fixed=None, popularity=None, real_enemy_items=None):
    """Beam search over item orders. Returns (items, score, explanation).
    fixed: items already owned (live game) – the search continues from there.
    real_enemy_items: {enemy name: [item ids]} – uses the enemies' real resistances instead of estimates."""
    REAL_ENEMY_ITEMS.clear()
    REAL_ENEMY_ITEMS.update(real_enemy_items or {})
    order = ability_order(c)
    base_clear = clear_time(c, [], item_db, jungle, levels, level=6) if jungle else None
    cands = pool or item_pool(item_db, c, popularity)
    # prefilter: keep items that help this champion on their own
    single = []
    for i in cands:
        s, _ = score_build(c, [i], item_db, enemies, levels, enemy_mix, melee, order, jungle, base_clear)
        single.append((s, i))
    single.sort(reverse=True)
    cands = [i for _, i in single[:34]]
    fixed = [i for i in (fixed or []) if i in item_db and not item_db[i]["boots"]][:n_items - 1]
    states = [((fixed or []), 0.0)]
    for depth in range(len(fixed or []), n_items):
        nxt = []
        seen = set()
        for items, _ in states:
            for i in cands:
                if i in items or any(conflicts(i, j) for j in items):
                    continue
                new = items + [i]
                key = (frozenset(new[:-1]), new[-1]) if depth < 3 else frozenset(new)
                if key in seen:
                    continue
                seen.add(key)
                s, _ = score_build(c, new, item_db, enemies, levels, enemy_mix, melee, order, jungle, base_clear)
                nxt.append((new, s))
        nxt.sort(key=lambda x: -x[1])
        states = nxt[:beam]
    items, score = states[0]
    # boots: best marginal pair for the 2nd checkpoint
    b_best = None
    for b in (boots or boots_items(item_db)):
        s, _ = score_build(c, items[:1] + [b] + items[1:3], item_db, enemies, levels, enemy_mix, melee, order)
        if b_best is None or s > b_best[0]:
            b_best = (s, b)
    return {"items": items, "boots": b_best[1] if b_best else None, "score": score, "order": order,
            "alternatives": [s[0] for s in states[1:6]],
            "explain": explain(c, items, item_db, enemies, levels, enemy_mix, melee, order)}


def explain(c, items, item_db, enemies, levels, enemy_mix, melee, order):
    """Per item: how much damage / survivability it adds at the 3-item point."""
    n = min(3, len(items))
    lv = CHECKPOINTS[n - 1][1]
    off, dfn, _ = evaluate(c, items[:n], item_db, enemies, levels, lv, n, enemy_mix, melee, order)
    out = {}
    for i in items[:n]:
        rest = [x for x in items[:n] if x != i]
        o2, d2, _ = evaluate(c, rest, item_db, enemies, levels, lv, n, enemy_mix, melee, order)
        out[i] = {"dmg": round(100 * (off / max(o2, 1e-9) - 1)), "tank": round(100 * (dfn / max(d2, 1e-9) - 1))}
    return out
