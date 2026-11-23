"""Turns the raw game files of the current patch into compact simulation data.

Input:  gamedata/ folder from fetch_gamedata.py (Data Dragon + CommunityDragon game files).
Output: dict with champions (base stats, ability damage formulas as linear stat coefficients,
        damage types, cooldowns), items (stats + passive data values) and jungle camps.

Everything is read from the game files, so numbers follow every patch automatically.
"""
import json
import os
import re

# CommunityDragon stat enum -> our stat keys (formula 0 = total, 1 = base, 2 = bonus)
STAT = {0: "ap", 1: "armor", 2: "ad", 4: "as", 6: "mr", 7: "ms", 12: "hp"}
DMG_TAG = re.compile(r"<(magic|physical|true)Damage>(.*?)</\1Damage>", re.S | re.I)
TOKEN = re.compile(r"@([A-Za-z0-9_.:]+?)(\*-?[\d.]+)?@")
SLOTS = ["Q", "W", "E", "R"]


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ formula evaluation
def _dv(spell, name, rank):
    for d in spell.get("DataValues") or spell.get("mDataValues") or []:
        if str(d.get("name", d.get("mName", ""))).lower() == name.lower():
            vals = d.get("values")
            if vals is None:
                return float(d.get("mValue", 0.0))
            return float(vals[min(rank, len(vals) - 1)])
    return None


def _level_value(part, level):
    t = part.get("__type")
    if t == "ByCharLevelInterpolationCalculationPart":
        a, b = part.get("mStartValue", 0.0), part.get("mEndValue", 0.0)
        return a + (b - a) * (level - 1) / 17.0
    if t == "ByCharLevelBreakpointsCalculationPart":
        v = part.get("mLevel1Value", 0.0)
        per = part.get("mInitialBonusPerLevel", 0.0)
        bps = sorted(part.get("mBreakpoints", []), key=lambda b: b.get("mLevel", 99))
        for lv in range(2, level + 1):
            for bp in bps:
                if bp.get("mLevel") == lv:
                    v += bp.get("mAdditionalBonusAtThisLevel", 0.0)
                    per = bp.get("{d5fd07ed}", bp.get("mBonusPerLevelAtAndAfter", per))
            v += per
        return v
    if t == "ByCharLevelFormulaCalculationPart":
        vals = part.get("mValues") or []
        return float(vals[min(level, len(vals) - 1)]) if vals else 0.0
    return None


def _add(acc, other, scale=1.0):
    for k, v in other.items():
        acc[k] = acc.get(k, 0.0) + v * scale
    return acc


def _stat_key(part):
    s = STAT.get(part.get("mStat", 0))
    if not s:
        return None
    form = part.get("mStatFormula", 0)
    return ("b" + s) if form == 2 else (("base_" + s) if form == 1 else s)


def evaluate(part, spell, calcs, rank, level, depth=0):
    """Return a linear form {stat_key: coefficient, 'base': constant} for a calculation part."""
    if depth > 12 or not isinstance(part, dict):
        return {}
    t = part.get("__type", "")
    if t == "GameCalculationModified":
        ref = calcs.get(part.get("mModifiedGameCalculation", ""))
        acc = evaluate(ref, spell, calcs, rank, level, depth + 1) if ref else {}
        m = evaluate(part.get("mMultiplier", {}), spell, calcs, rank, level, depth + 1).get("base", 1.0)
        return {k: v * m for k, v in acc.items()}
    if t == "GameCalculation" or "mFormulaParts" in part:
        acc = {}
        for p in part.get("mFormulaParts", []):
            _add(acc, evaluate(p, spell, calcs, rank, level, depth + 1))
        mult = part.get("mMultiplier")
        if mult:
            m = evaluate(mult, spell, calcs, rank, level, depth + 1).get("base", 1.0)
            acc = {k: v * m for k, v in acc.items()}
        return acc
    if t == "NamedDataValueCalculationPart":
        return {"base": _dv(spell, part.get("mDataValue", ""), rank) or 0.0}
    if t == "NumberCalculationPart":
        return {"base": float(part.get("mNumber", 0.0))}
    if t in ("StatByNamedDataValueCalculationPart", "StatByCoefficientCalculationPart"):
        k = _stat_key(part)
        c = _dv(spell, part["mDataValue"], rank) if "mDataValue" in part else part.get("mCoefficient", 0.0)
        return {k: c or 0.0} if k else {}
    if t == "StatBySubPartCalculationPart":
        k = _stat_key(part)
        c = evaluate(part.get("mSubpart", {}), spell, calcs, rank, level, depth + 1).get("base", 0.0)
        return {k: c} if k else {}
    if t == "SumOfSubPartsCalculationPart":
        acc = {}
        for p in part.get("mSubparts", []):
            _add(acc, evaluate(p, spell, calcs, rank, level, depth + 1))
        return acc
    if t == "ProductOfSubPartsCalculationPart":
        a = evaluate(part.get("mPart1", {}), spell, calcs, rank, level, depth + 1)
        b = evaluate(part.get("mPart2", {}), spell, calcs, rank, level, depth + 1)
        if set(a) <= {"base"}:
            return {k: v * a.get("base", 0.0) for k, v in b.items()}
        if set(b) <= {"base"}:
            return {k: v * b.get("base", 0.0) for k, v in a.items()}
        return {"base": a.get("base", 0.0) * b.get("base", 0.0)}
    if t == "ClampSubPartsCalculationPart":
        acc = {}
        for p in part.get("mSubparts", []):
            _add(acc, evaluate(p, spell, calcs, rank, level, depth + 1))
        return acc
    if t == "{f3cbe7b2}" and "mSpellCalculationKey" in part:  # reference to another calculation
        ref = calcs.get(part["mSpellCalculationKey"])
        return evaluate(ref, spell, calcs, rank, level, depth + 1) if ref else {}
    lv = _level_value(part, level)
    if lv is not None:
        return {"base": lv}
    return {}


# ------------------------------------------------------------------ champions
def _spell_objects(binf):
    out = {}
    for k, v in binf.items():
        if isinstance(v, dict) and v.get("__type") == "SpellObject":
            out[k] = v
    return out


def _tooltip(spell_obj, strings):
    s = spell_obj.get("mSpell", {})
    keys = (((s.get("mClientData") or {}).get("mTooltipData") or {}).get("mLocKeys") or {})
    k = keys.get("keyTooltip") or f"Spell_{spell_obj.get('mScriptName', '')}_Tooltip"
    return strings.get(k.lower()) or ""


def _damage_parts(spell_obj, strings, levels):
    """Damage components of one ability per (rank, level) from the damage-tagged tooltip values."""
    s = spell_obj.get("mSpell", {})
    calcs = s.get("mSpellCalculations") or {}
    tip = _tooltip(spell_obj, strings)
    parts = []
    seen = set()
    for dtype, inner in DMG_TAG.findall(tip):
        toks = list(TOKEN.finditer(inner))
        for i, m in enumerate(toks):
            name, mul = m.group(1), m.group(2)
            if name in seen:
                continue
            seen.add(name)
            # text that belongs to this value: up to the next value (or 40 chars)
            seg = inner[m.end(): toks[i + 1].start() if i + 1 < len(toks) else len(inner)][:60].lower()
            calc = calcs.get(name) or {}
            as_pct = bool(calc.get("mDisplayAsPercent")) or bool(
                (calcs.get(calc.get("mModifiedGameCalculation", "")) or {}).get("mDisplayAsPercent"))
            kind, pct_scale = "flat", 1.0
            if "health" in seg and (as_pct or seg.lstrip().startswith("%")):
                kind = "pct_cur" if "current" in seg else ("pct_missing" if "missing" in seg else "pct_max")
                pct_scale = 1.0 if (as_pct or mul) else 0.01  # stored as fraction of target health
            around = tip.lower()[max(0, tip.find(inner) - 80): tip.find(inner) + len(inner) + 40]
            parts.append({"name": name, "type": dtype.lower(), "kind": kind, "dot": "per second" in around,
                          "mul": (1.0 if (mul and kind != "flat") else (float(mul[1:]) if mul else 1.0)) * pct_scale})
    if not parts:
        return []
    out = []
    for p in parts:
        table = {}
        for rank in range(0, 7):
            for lv in levels:
                if p["name"] in calcs:
                    form = evaluate(calcs[p["name"]], s, calcs, rank, lv)
                else:
                    v = _dv(s, p["name"], rank)
                    form = {"base": v} if v is not None else {}
                if p["mul"] != 1.0:
                    form = {k: v * p["mul"] for k, v in form.items()}
                table[f"{rank}:{lv}"] = {k: round(v, 5) for k, v in form.items() if v}
        if any(table.values()):
            out.append({"type": p["type"], "kind": p["kind"], "dot": p["dot"], "f": table})
    return out


def _cooldowns(s):
    cd = s.get("cooldownTime") or []
    return [round(float(x), 2) for x in cd]


def build_champion(binf, ddragon, strings, levels):
    rec = next((v for v in binf.values() if isinstance(v, dict) and v.get("__type") == "CharacterRecord"
                and "spellNames" in v), None)
    if not rec:
        return None
    sp = _spell_objects(binf)
    by_path = {k: v for k, v in sp.items()}

    def mv(key, default=0.0):
        x = rec.get(key)
        if isinstance(x, dict):
            return float(x.get("baseValue", default))
        return float(x) if isinstance(x, (int, float)) else default

    stats = {
        "hp": mv("baseHPModifiable"), "hp_l": mv("hpPerLevelModifiable"),
        "ad": mv("baseDamageModifiable"), "ad_l": mv("damagePerLevelModifiable"),
        "armor": mv("baseArmorModifiable"), "armor_l": mv("armorPerLevelModifiable"),
        "mr": mv("baseMR"), "mr_l": mv("mrPerLevel"),
        "as": mv("attackSpeedModifiable", 0.625), "as_ratio": mv("attackSpeedRatioModifiable", 0.625),
        "as_l": mv("attackSpeedPerLevelModifiable"), "range": mv("attackRangeModifiable", 125),
        "ms": mv("baseMoveSpeedModifiable", 340), "crit_mult": float(rec.get("critDamageMultiplier", 1.75)),
    }
    if ddragon:  # Data Dragon is the official source for base stats when both disagree
        d = ddragon["stats"]
        stats.update({"hp": d["hp"], "hp_l": d["hpperlevel"], "ad": d["attackdamage"], "ad_l": d["attackdamageperlevel"],
                      "armor": d["armor"], "armor_l": d["armorperlevel"], "mr": d["spellblock"],
                      "mr_l": d["spellblockperlevel"], "as": d["attackspeed"], "as_l": d["attackspeedperlevel"],
                      "range": d["attackrange"], "ms": d["movespeed"]})
    abilities = {}
    paths = rec.get("spells") or []
    for slot, path in zip(SLOTS, paths):
        obj = by_path.get(path)
        if not obj:
            continue
        s = obj.get("mSpell", {})
        abilities[slot] = {"cd": _cooldowns(s), "dmg": _damage_parts(obj, strings, levels),
                           "cast": round(float(s.get("mCastTime", 0.25) or 0.25), 2)}
    pobj = by_path.get(rec.get("mCharacterPassiveSpell", ""))
    if pobj:
        tip = _tooltip(pobj, strings).lower()
        onhit = "attack" in tip and any(w in tip for w in ("on hit", "on-hit", "attacks deal", "attacks apply"))
        abilities["P"] = {"cd": [], "dmg": _damage_parts(pobj, strings, levels), "cast": 0, "onhit": onhit}
    return {"stats": stats, "abilities": abilities}


# ------------------------------------------------------------------ items
ITEM_STATS = {
    "mFlatHPPoolMod": "hp", "mAbilityHasteMod": "ah", "mFlatPhysicalDamageMod": "ad", "mFlatMagicDamageMod": "ap",
    "mFlatArmorMod": "armor", "mFlatSpellBlockMod": "mr", "mPercentAttackSpeedMod": "as",
    "mPercentMovementSpeedMod": "ms_pct", "mFlatMovementSpeedMod": "ms", "mFlatCritChanceMod": "crit",
    "PhysicalLethality": "lethality", "mPercentLifeStealMod": "lifesteal",
    "mPercentMagicPenetrationMod": "mpen_pct", "mFlatMagicPenetrationMod": "mpen",
    "mPercentArmorPenetrationMod": "apen_pct", "mFlatCritDamageMod": "crit_dmg", "mPercentHealingAmountMod": "heal_amp",
    "mPercentTenacityItemMod": "tenacity", "flatMPPoolMod": "mana",
}


def build_items(item_bin, ddragon_items, ddragon_items_de):
    out = {}
    bins = {v["itemID"]: v for v in item_bin.values()
            if isinstance(v, dict) and v.get("__type") == "ItemData" and isinstance(v.get("itemID"), int)}
    for sid, d in ddragon_items.items():
        iid = int(sid)
        if iid >= 10000 or not d["maps"].get("11") or not d["gold"]["purchasable"]:
            continue
        if d.get("requiredChampion") or d.get("requiredAlly") or d.get("hideFromAll"):
            continue
        tags = d.get("tags", [])
        boots = "Boots" in tags and d["gold"]["total"] > 300
        legendary = not d.get("into") and d["gold"]["total"] >= 2000
        if not (legendary or boots):
            continue
        b = bins.get(iid, {})
        stats = {}
        for k, key in ITEM_STATS.items():
            v = b.get(k)
            if v:
                stats[key] = round(float(v) * (100 if key in ("as", "crit", "ms_pct", "lifesteal", "mpen_pct",
                                                               "apen_pct", "heal_amp", "tenacity") and abs(v) <= 3 else 1), 3)
        dv = {x["mName"]: round(float(x.get("mValue", 0)), 5) for x in b.get("mDataValues", []) if "mName" in x}
        out[sid] = {"name": d["name"], "name_de": (ddragon_items_de.get(sid) or {}).get("name", d["name"]),
                    "gold": d["gold"]["total"], "tags": tags, "boots": boots, "stats": stats, "dv": dv}
    return out


# ------------------------------------------------------------------ jungle camps
CAMPS = {  # camp -> monsters in it (counts)
    "blue": {"sru_blue": 1}, "red": {"sru_red": 1}, "gromp": {"sru_gromp": 1},
    "wolves": {"sru_murkwolf": 1, "sru_murkwolfmini": 2}, "raptors": {"sru_razorbeak": 1, "sru_razorbeakmini": 5},
    "krugs": {"sru_krug": 1, "sru_krugmini": 1}, "scuttle": {"sru_crab": 1},
}


def build_monsters(folder):
    mons = {}
    for f in os.listdir(folder):
        b = _load(os.path.join(folder, f))
        rec = next((v for v in b.values() if isinstance(v, dict) and v.get("__type") == "CharacterRecord"), None)
        if not rec:
            continue

        def mv(key, default=0.0):
            x = rec.get(key)
            return float(x.get("baseValue", default)) if isinstance(x, dict) else (float(x) if isinstance(x, (int, float)) else default)
        mons[f.split(".")[0]] = {"hp": mv("baseHPModifiable"), "armor": mv("baseArmorModifiable"),
                                 "mr": mv("baseMR"), "ad": mv("baseDamageModifiable"),
                                 "as": mv("attackSpeedModifiable", 0.6)}
    return {"monsters": mons, "camps": CAMPS}


# ------------------------------------------------------------------ main
def build(folder, levels=(3, 6, 9, 11, 13, 16, 18)):
    j = lambda *p: os.path.join(folder, *p)  # noqa: E731
    strings = {k.lower(): v for k, v in _load(j("cdragon", "lol.stringtable.json"))["entries"].items()}
    dd = _load(j("ddragon", "championFull.json"))["data"]
    dd_by_alias = {k.lower(): v for k, v in dd.items()}
    champs = {}
    for f in sorted(os.listdir(j("cdragon", "champions"))):
        alias = f.split(".")[0]
        ddc = dd_by_alias.get(alias)
        if not ddc:
            continue
        c = build_champion(_load(j("cdragon", "champions", f)), ddc, strings, levels)
        if c:
            c.update({"name": ddc["name"], "key": int(ddc["key"]), "tags": ddc.get("tags", []),
                      "partype": ddc.get("partype", "")})
            champs[ddc["id"]] = c
    items = build_items(_load(j("cdragon", "items.cdtb.bin.json")), _load(j("ddragon", "item.json"))["data"],
                        _load(j("ddragon", "item_de.json"))["data"])
    meta = _load(j("meta.json"))
    return {"version": meta.get("ddragon_version"), "levels": list(levels), "champions": champs, "items": items,
            "jungle": build_monsters(j("cdragon", "monsters"))}
