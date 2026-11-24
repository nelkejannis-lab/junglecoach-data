"""Deep analysis of one jungle game from the official match-v5 data + timeline."""
from .review import region

OBJ_NAMES = {"FIRE_DRAGON": "Feuer-Drache", "WATER_DRAGON": "Ozean-Drache", "EARTH_DRAGON": "Berg-Drache",
             "AIR_DRAGON": "Wolken-Drache", "HEXTECH_DRAGON": "Hextech-Drache", "CHEMTECH_DRAGON": "Chemtech-Drache",
             "ELDER_DRAGON": "Elder-Drache", "BARON_NASHOR": "Baron", "RIFTHERALD": "Herold", "HORDE": "Voidgrubs",
             "ATAKHAN": "Atakhan", "DRAGON": "Drache"}
DRAGON_PIT, BARON_PIT = (9866, 4414), (5007, 10471)
JUNGLE_CS10_TARGET = 60      # rough solid-elo benchmark
VISION_PER_MIN_TARGET = 1.0


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _clamp(v):
    return max(0, min(100, round(v)))


def _grade(score):
    return "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D"


BENCH = None  # app sets: callable(champion, role) -> MetaStats.benchmarks(...) result or None


def _targets(champ):
    """Same benchmark as the coach (champion + role + elo from real games) instead of fixed numbers."""
    jcs, vpm, scope = JUNGLE_CS10_TARGET, VISION_PER_MIN_TARGET, "Richtwert (zu wenig Vergleichsdaten)"
    try:
        b = BENCH(champ, "JUNGLE") if BENCH else None
    except Exception:  # noqa: BLE001
        b = None
    if b:
        d = b.get("dist", {})
        if d.get("jcs10"):
            jcs = d["jcs10"][len(d["jcs10"]) // 2]
        if d.get("vpm"):
            vpm = d["vpm"][len(d["vpm"]) // 2]
        scope = f"Median {b['scope']} ({b['games']} Games)"
    return jcs, vpm, scope


CAUSES = {  # cause -> (label, what to do differently)
    "trade": ("Tausch", "Dein Team hat im selben Moment etwas geholt – prüfe, ob es das wert war."),
    "outnumbered": ("Unterzahl", "Mehr Gegner als Verbündete in der Nähe: ohne Info nicht nachrücken, früher abdrehen."),
    "deep": ("Tief im Gegner-Jungle", "Invades nur mit Lane-Prio und wenn du weißt, wo der Gegner-Jungler ist."),
    "gold": ("Gold nicht ausgegeben", "Mit viel Gold kein Risiko: erst zurück und kaufen, dann kämpfen."),
    "level": ("Level-Nachteil", "2+ Level hinten: Duelle meiden, farmen und auf Team-Fights warten."),
    "vision": ("Ohne Sicht", "Vor Flussbewegungen ein Ward oder Sweeper nutzen – Gegner im Fog einplanen."),
    "jungler": ("Gegner-Jungler-Gank", "Gegner-Jungler-Position tracken; wenn er unsichtbar ist, vorsichtiger stehen."),
    "duel": ("Duell verloren", "Vor dem Duell: Level, Items, Cooldowns (Smite/Flash) vergleichen."),
}


def _near(frames, minute_a, minute_b, who, xy, radius=2200):
    """Was 'who' within radius of xy at the minute frames around the death? (positions only per minute)."""
    best = None
    for m in (minute_a, minute_b):
        if 0 <= m < len(frames):
            p = frames[m]["participantFrames"].get(str(who), {}).get("position")
            if p:
                d = _dist((p["x"], p["y"]), xy)
                best = d if best is None else min(best, d)
    return best is not None and best <= radius


# respawn timer by level (Summoner's Rift, before the late-game time multiplier)
_BRW = [10, 10, 12, 12, 14, 16, 20, 25, 28, 32.5, 35, 37.5, 40, 42.5, 45, 47.5, 50, 52.5]
OBJ_VALUE = {"Drache": 600, "Elder-Drache": 1500, "Baron": 1500, "Herold": 500, "Voidgrubs": 300, "Atakhan": 900,
             "Turm": 550, "Inhibitor": 700}


def death_cost(ev, ts, frames, pid, events, team, gpm):
    """What a death cost, in gold value: shutdown/bounty for the enemy + income missed while dead
    (respawn timer x own gold/min) + objectives/towers the enemy took within 60 s afterwards."""
    m = min(int(ts // 60), len(frames) - 1)
    lvl = frames[m]["participantFrames"].get(str(pid), {}).get("level") or 1
    dead = _BRW[min(max(lvl, 1), 18) - 1] * (1 + max(0, ts / 60 - 15) * 0.02)   # rough late-game increase
    bounty = (ev.get("bounty") or 300) + (ev.get("shutdownBounty") or 0)
    missed = round(dead * gpm / 60)
    lost = []
    for e in events:
        dt = e.get("timestamp", 0) / 1000 - ts
        if not 0 <= dt <= 60:
            continue
        if e.get("type") == "ELITE_MONSTER_KILL" and e.get("killerTeamId") not in (None, team):
            raw = e.get("monsterSubType") or e.get("monsterType")
            lost.append(OBJ_NAMES.get(raw, raw))
        elif e.get("type") == "BUILDING_KILL" and e.get("teamId") == team:
            lost.append("Turm" if e.get("buildingType") == "TOWER_BUILDING" else "Inhibitor")
    obj = sum(OBJ_VALUE.get(x, 400) for x in lost)
    return {"cost": bounty + missed + obj, "dead_s": round(dead), "bounty": bounty, "missed": missed, "lost": lost}


def death_causes_all(match, timeline):
    """Death causes for all 10 players of a game: {participantId: {cause: count}} (used by the crawler
    to compare your causes with better players)."""
    info, frames = match["info"], timeline["info"]["frames"]
    parts = info["participants"]
    team_of = {p["participantId"]: p["teamId"] for p in parts}
    events = [ev for fr in frames for ev in fr.get("events", [])]
    wards = {}
    for e in events:
        if e.get("type") == "WARD_PLACED" and e.get("wardType") != "UNDEFINED":
            wards.setdefault(e.get("creatorId"), []).append(e["timestamp"] / 1000)
    out = {p["participantId"]: {c: 0 for c in CAUSES} for p in parts}
    for ev in events:
        if ev.get("type") != "CHAMPION_KILL" or ev.get("victimId") not in out:
            continue
        v = ev["victimId"]
        try:
            c = death_context(ev, ev["timestamp"] / 1000, frames, parts, v, team_of[v], events, wards.get(v, []), team_of)["cause"]
        except Exception:  # noqa: BLE001 - incomplete frames
            continue
        out[v][c] += 1
    return out


def death_context(ev, ts, frames, parts, pid, team, events, my_wards, team_of):
    """Why did you die? Built only from official timeline data (no visibility data exists there):
    who was involved, who was roughly nearby (minute positions, ±30 s), unspent gold, level gap,
    own/team wards in the 90 s before, objectives your team took at the same time (trade)."""
    p = ev.get("position") or {}
    xy = (p.get("x", 0), p.get("y", 0))
    m0, m1 = int(ts // 60), int(ts // 60) + 1
    champ = {x["participantId"]: x["championName"] for x in parts}
    killers = [ev.get("killerId")] + list(ev.get("assistingParticipantIds", []))
    killers = [k for k in killers if k and team_of.get(k) != team]
    allies_near = [x["participantId"] for x in parts if team_of[x["participantId"]] == team and x["participantId"] != pid
                   and _near(frames, m0, m1, x["participantId"], xy)]
    me_f = frames[min(m0, len(frames) - 1)]["participantFrames"].get(str(pid), {})
    unspent = me_f.get("currentGold", 0)
    kf = frames[min(m0, len(frames) - 1)]["participantFrames"].get(str(ev.get("killerId")), {}) if ev.get("killerId") else {}
    level_gap = (me_f.get("level") or 0) - (kf.get("level") or 0) if kf else 0
    team_wards = [e for e in events if e.get("type") == "WARD_PLACED" and team_of.get(e.get("creatorId")) == team
                  and e.get("wardType") not in ("UNDEFINED",) and 0 <= ts - e["timestamp"] / 1000 <= 90]
    own_wards = [w for w in my_wards if 0 <= ts - w <= 90]
    took = []
    for e in events:
        if abs(e.get("timestamp", 0) / 1000 - ts) > 60:
            continue
        if e.get("type") == "BUILDING_KILL" and e.get("teamId") not in (None, team):
            took.append("Turm" if e.get("buildingType") == "TOWER_BUILDING" else "Inhibitor")
        elif e.get("type") == "ELITE_MONSTER_KILL" and e.get("killerTeamId") == team:
            raw = e.get("monsterSubType") or e.get("monsterType")
            took.append(OBJ_NAMES.get(raw, raw))
    ej_inv = any(x.get("teamPosition") == "JUNGLE" and x["participantId"] in killers for x in parts)
    area = region(*xy, team) if p else "?"
    # main cause (first match wins – most useful lesson first)
    if took:
        cause = "trade"
    elif len(killers) >= 2 and len(killers) > len(allies_near) + 1:
        cause = "outnumbered"
    elif area.startswith("gegnerischer") and len(killers) >= 1 and not own_wards:
        cause = "deep"
    elif unspent >= 1300:
        cause = "gold"
    elif level_gap <= -2:
        cause = "level"
    elif ej_inv and len(killers) >= 2:
        cause = "jungler"
    elif not own_wards and not team_wards:
        cause = "vision"
    else:
        cause = "duel"
    facts = [f"{len(killers)} Gegner beteiligt ({', '.join(champ[k] for k in killers)})",
             f"ca. {len(allies_near)} Verbündete in der Nähe"]
    if unspent >= 700:
        facts.append(f"{unspent} Gold nicht ausgegeben")
    if level_gap:
        facts.append(f"Level {'+' if level_gap > 0 else ''}{level_gap} vs Killer")
    facts.append(f"{len(own_wards)} eigene / {len(team_wards)} Team-Wards in 90 s davor")
    if took:
        facts.append("gleichzeitig geholt: " + ", ".join(sorted(set(took))))
    return {"cause": cause, "cause_label": CAUSES[cause][0], "tip": CAUSES[cause][1], "facts": facts,
            "killers": [champ[k] for k in killers], "allies_near": len(allies_near), "unspent": unspent,
            "level_gap": level_gap, "trade": sorted(set(took))}


def deep_analyze(match, timeline, puuid):
    info = match["info"]
    parts = info["participants"]
    me = next(p for p in parts if p["puuid"] == puuid)
    pid, team = me["participantId"], me["teamId"]
    ej = next((p for p in parts if p["teamId"] != team and p.get("teamPosition") == "JUNGLE"), None)
    ej_pid = ej["participantId"] if ej else None
    frames = timeline["info"]["frames"]
    pos_by_pid = {p["participantId"]: p.get("teamPosition") for p in parts}
    team_of = {p["participantId"]: p["teamId"] for p in parts}
    minutes = len(frames) - 1
    game_min = max(1, info.get("gameDuration", minutes * 60) / 60)
    ch = me.get("challenges", {})

    def pf(m, who):
        m = min(m, len(frames) - 1)
        return frames[m]["participantFrames"].get(str(who), {})

    def pos_at(ts_sec, who):
        p = pf(round(ts_sec / 60), who).get("position")
        return (p["x"], p["y"]) if p else None

    # ---- curves (per minute)
    curve = []
    for m in range(min(len(frames), 31)):
        a, b = pf(m, pid), pf(m, ej_pid) if ej_pid else {}
        curve.append({"min": m, "gold_diff": a.get("totalGold", 0) - b.get("totalGold", 0),
                      "xp_diff": a.get("xp", 0) - b.get("xp", 0),
                      "jcs": a.get("jungleMinionsKilled", 0), "jcs_enemy": b.get("jungleMinionsKilled", 0)})

    events = [ev for fr in frames for ev in fr.get("events", [])]
    my_wards = [ev["timestamp"] / 1000 for ev in events
                if ev.get("type") == "WARD_PLACED" and ev.get("creatorId") == pid and ev.get("wardType") != "UNDEFINED"]
    enemy_objs = [ev for ev in events if ev.get("type") == "ELITE_MONSTER_KILL" and ev.get("killerTeamId") != team]

    # ---- deaths with context
    deaths = []
    for ev in events:
        if ev.get("type") != "CHAMPION_KILL" or ev.get("victimId") != pid:
            continue
        ts = ev["timestamp"] / 1000
        p = ev.get("position") or {}
        area = region(p.get("x", 0), p.get("y", 0), team) if p else "?"
        n_enemies = 1 + len(ev.get("assistingParticipantIds", []))
        tags = []
        if n_enemies >= 2:
            tags.append(f"mehrere Gegner beteiligt ({n_enemies})")
        if area.startswith("gegnerischer"):
            tags.append("im gegnerischen Jungle")
        if ej_pid and (ev.get("killerId") == ej_pid or ej_pid in ev.get("assistingParticipantIds", [])):
            tags.append("Gegner-Jungler beteiligt")
        if not any(ts - 180 <= w <= ts for w in my_wards):
            tags.append("keine eigenen Wards in den 3 Min davor")
        lost = [o for o in enemy_objs if 0 <= o["timestamp"] / 1000 - ts <= 90]
        if lost:
            raw = lost[0].get("monsterSubType") or lost[0].get("monsterType")
            tags.append(f"≤90 s danach Objective an Gegner ({OBJ_NAMES.get(raw, raw)}) – zeitliche Folge, nicht bewiesen")
        gd = curve[min(round(ts / 60), len(curve) - 1)]["gold_diff"] if curve else 0
        if gd < -1000:
            tags.append(f"{gd} Gold hinten gegen Gegner-Jungler")
        ctx = death_context(ev, ts, frames, parts, pid, team, events, my_wards, team_of)
        cost = death_cost(ev, ts, frames, pid, events, team, me.get("goldEarned", 0) / game_min)
        deaths.append({"min": round(ts / 60, 1), "t": ts, "area": area, "tags": tags or ["kein klares Muster"],
                       **ctx, **cost})
    # most expensive first: that is the death worth fixing
    deaths.sort(key=lambda d: -d["cost"])
    for i, d in enumerate(deaths):
        d["cost_rank"] = i + 1

    # ---- objectives incl. presence
    objectives = []
    for ev in events:
        if ev.get("type") != "ELITE_MONSTER_KILL":
            continue
        ts = ev["timestamp"] / 1000
        pit = DRAGON_PIT if ev.get("monsterType") == "DRAGON" else BARON_PIT
        mp = pos_at(ts, pid)
        raw = ev.get("monsterSubType") or ev.get("monsterType")
        objectives.append({"min": round(ts / 60, 1), "type": OBJ_NAMES.get(raw, raw),
                           "ours": ev.get("killerTeamId") == team,
                           "present": bool(mp and _dist(mp, pit) < 3500)})

    # ---- ganks / early impact
    ganks = {"kills": 0, "by_lane": {}}
    for ev in events:
        ts = ev.get("timestamp", 0) / 1000
        if ev.get("type") == "CHAMPION_KILL" and ts < 14 * 60 and \
                (ev.get("killerId") == pid or pid in ev.get("assistingParticipantIds", [])):
            lane = pos_by_pid.get(ev.get("victimId"), "?")
            ganks["kills"] += 1
            ganks["by_lane"][lane] = ganks["by_lane"].get(lane, 0) + 1

    # ---- scores per category
    gd10 = curve[10]["gold_diff"] if len(curve) > 10 else 0
    jcs10 = curve[10]["jcs"] if len(curve) > 10 else 0
    kp = ch.get("killParticipation", 0) or 0
    vpm = me.get("visionScore", 0) / game_min
    obj_pre20 = [o for o in objectives if o["min"] < 20]
    obj_share = (sum(o["ours"] for o in obj_pre20) / len(obj_pre20)) if obj_pre20 else 0.5
    presence = (sum(o["present"] for o in objectives) / len(objectives)) if objectives else 0.5
    bad_deaths = sum(1 for d in deaths if any(t.startswith(("mehrere Gegner", "im gegnerischen")) for t in d["tags"]))

    t_jcs, t_vpm, t_scope = _targets(me.get("championName"))
    cats = {
        "Tempo & Farm": _clamp(50 + gd10 / 25 + (jcs10 - t_jcs) * 1.2),
        "Gank-Impact": _clamp(kp * 110 + ganks["kills"] * 4),
        "Objectives": _clamp(obj_share * 70 + presence * 30),
        "Vision": _clamp(vpm / max(t_vpm, 0.1) * 70 + me.get("detectorWardsPlaced", 0) * 3),
        "Überleben": _clamp(100 - me["deaths"] * 9 - bad_deaths * 8),
    }
    advice = {
        "Tempo & Farm": f"Gold @10 {gd10:+} vs Gegner-Jungler, {jcs10} Jungle-CS @10 (Vergleich ~{t_jcs}: {t_scope}). "
                        "Clear-Route optimieren, nach Ganks sofort Camps nachholen statt Lane zu stehen.",
        "Gank-Impact": f"Kill-Beteiligung {round(kp * 100)}%. Ganks auf Lanes mit Prio/CC timen, "
                       "Gegner-Jungler auf der anderen Seite tracken und dann gehen.",
        "Objectives": f"{sum(o['ours'] for o in obj_pre20)}/{len(obj_pre20)} Objectives vor Min 20, "
                      f"bei ~{round(presence * 100)}% in der Nähe (Position minütlich, ±30 s ungenau). 60–90 s vorher Wave pushen lassen, warden, Reset timen.",
        "Vision": f"{vpm:.2f} Vision/Min, {me.get('detectorWardsPlaced', 0)} Control Wards. "
                  "Pro Back eine Control Ward, Sweeper ab Objective-Phase.",
        "Überleben": f"{me['deaths']} Tode, davon {bad_deaths} mit mehreren Gegnern/im gegnerischen Jungle. "
                     "Vor Invades Lane-Prio checken, Gegner-Jungler-Position kennen.",
    }
    causes = {}
    for dth in deaths:
        causes[dth["cause"]] = causes.get(dth["cause"], 0) + 1
    if causes:
        top = max(causes, key=causes.get)
        if top != "trade" and causes[top] >= 2:
            advice["Überleben"] += f" Häufigste Todesursache: {CAUSES[top][0]} ({causes[top]}×) – {CAUSES[top][1]}"
    ranked = sorted(cats.items(), key=lambda kv: kv[1])
    return {
        "v": 3,
        "game_start": info.get("gameStartTimestamp", info.get("gameCreation", 0)) / 1000,
        "categories": [{"name": k, "score": v, "grade": _grade(v)} for k, v in cats.items()],
        "overall": _grade(sum(cats.values()) / len(cats)),
        "priorities": [{"name": k, "grade": _grade(v), "advice": advice[k]} for k, v in ranked[:3]],
        "curve": curve, "deaths": deaths, "death_causes": causes, "death_cost": sum(d["cost"] for d in deaths), "objectives": objectives, "ganks": ganks,
        "stats": {"kp": round(kp * 100), "vision_per_min": round(vpm, 2), "jungle_cs_10": jcs10,
                  "gold_diff_10": gd10, "enemy_jungle_cs_taken": ch.get("enemyJungleMonsterKills"),
                  "scuttles": ch.get("scuttleCrabKills"), "damage_share": round((ch.get("teamDamagePercentage") or 0) * 100)},
    }
