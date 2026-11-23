"""Post-game jungle review via the official Riot API (match-v5 timeline).

Needs a Riot API key (developer.riotgames.com) in config.json.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

REGION_ROUTING = {"euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
                  "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
                  "kr": "asia", "jp1": "asia", "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea"}


class RiotKeyError(RuntimeError):
    pass


class RiotNotFound(RuntimeError):
    pass


class RiotAPI:
    def __init__(self, key, platform="euw1"):
        self.key = key
        self.platform = platform
        self.routing = REGION_ROUTING.get(platform, "europe")

    def get_platform(self, path, params=None):
        return self.get(path, params, host=self.platform)

    def get(self, path, params=None, host=None):
        url = f"https://{host or self.routing}.api.riotgames.com{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for _ in range(4):
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.key})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:  # rate limit: wait as told
                    time.sleep(int(e.headers.get("Retry-After", "5")) + 1)
                    continue
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("status", {}).get("message", "")
                except Exception:
                    detail = ""
                where = path.split("?")[0]
                if e.code in (401, 403):
                    raise RiotKeyError(f"Riot lehnt den API Key ab ({e.code}{': ' + detail if detail else ''}). "
                                       "Dev-Keys laufen nach 24 h ab und werden ungültig, sobald du auf "
                                       "developer.riotgames.com einen neuen erzeugst. Aktuellen Key in den "
                                       "Einstellungen eintragen.")
                if e.code == 404:
                    raise RiotNotFound(f"Nicht gefunden: {where}")
                raise RuntimeError(f"Riot API Fehler {e.code} bei {where}{': ' + detail if detail else ''}")
        raise RuntimeError("Riot API Rate-Limit – später erneut versuchen.")


def _side(x, y):
    # Blue base bottom-left, red top-right. Above the diagonal = top side.
    return "Top-Seite" if y > x else "Bot-Seite"


MAP_MAX = 14870


def region(x, y, my_team=100):
    """Readable map area for a position. Blue (team 100) base = bottom-left."""
    if x < 4200 and y < 4200:
        return "Base Blau" if my_team != 100 else "eigene Base"
    if x > 10700 and y > 10700:
        return "Base Rot" if my_team == 100 else "eigene Base"
    if x < 2000 or y > 12900:
        return "Top-Lane"
    if y < 2000 or x > 12900:
        return "Bot-Lane"
    if abs(x - y) < 1300:
        return "Mid-Lane"
    if abs(x + y - MAP_MAX) < 1000:
        return "Fluss " + ("oben" if y > x else "unten")
    blue_half = x + y < MAP_MAX
    side = "Top-Seite" if y > x else "Bot-Seite"
    own = blue_half == (my_team == 100)
    return f"{'eigener' if own else 'gegnerischer'} Jungle {side}"


def review_match(match, timeline, puuid):
    info = match["info"]
    me = next(p for p in info["participants"] if p["puuid"] == puuid)
    if me.get("teamPosition") != "JUNGLE":
        return None
    pid, team = me["participantId"], me["teamId"]
    enemy_jg = next((p for p in info["participants"]
                     if p["teamId"] != team and p.get("teamPosition") == "JUNGLE"), None)
    pos_by_pid = {p["participantId"]: p.get("teamPosition") for p in info["participants"]}
    frames = timeline["info"]["frames"]

    def pf(minute, who):
        if minute < len(frames):
            return frames[minute]["participantFrames"].get(str(who), {})
        return {}

    sides = [_side(pf(m, pid).get("position", {}).get("x", 0), pf(m, pid).get("position", {}).get("y", 0))
             for m in range(2, 9) if pf(m, pid).get("position")]
    early_side = max(set(sides), key=sides.count) if sides else "?"

    ganks, deaths_early, objectives, first_action = [], 0, [], None
    for fr in frames:
        for ev in fr.get("events", []):
            ts = ev.get("timestamp", 0) / 1000
            if ev.get("type") == "CHAMPION_KILL" and ts < 14 * 60:
                involved = ev.get("killerId") == pid or pid in ev.get("assistingParticipantIds", [])
                if involved:
                    lane = pos_by_pid.get(ev.get("victimId"), "?")
                    ganks.append({"min": round(ts / 60, 1), "lane": lane})
                    first_action = first_action or round(ts / 60, 1)
                if ev.get("victimId") == pid and ts < 10 * 60:
                    deaths_early += 1
            if ev.get("type") == "ELITE_MONSTER_KILL" and ts < 20 * 60:
                mine = ev.get("killerTeamId") == team
                objectives.append({"min": round(ts / 60, 1), "type": ev.get("monsterType"),
                                   "sub": ev.get("monsterSubType"), "ours": mine})

    # ---- map replay: positions per minute + jungler-involved kills (with coordinates)
    def path(who):
        out = []
        for m in range(0, min(16, len(frames))):
            p = pf(m, who).get("position")
            if p:
                out.append({"min": m, "x": p["x"], "y": p["y"], "area": region(p["x"], p["y"], team)})
        return out
    ej_pid = enemy_jg["participantId"] if enemy_jg else None
    map_events = []
    for fr in frames:
        for ev in fr.get("events", []):
            ts = ev.get("timestamp", 0) / 1000
            if ev.get("type") != "CHAMPION_KILL" or ts >= 15 * 60 or not ev.get("position"):
                continue
            inv = {ev.get("killerId"), *ev.get("assistingParticipantIds", [])}
            who = "me" if pid in inv or ev.get("victimId") == pid else \
                  "enemy_jg" if ej_pid in inv or ev.get("victimId") == ej_pid else None
            if who:
                x, y = ev["position"]["x"], ev["position"]["y"]
                map_events.append({"min": round(ts / 60, 1), "x": x, "y": y, "who": who,
                                   "area": region(x, y, team),
                                   "result": "Kill" if (pid in inv or ej_pid in inv) else "Tod"})
    ej_path = path(ej_pid) if ej_pid else []
    appearances = [{"min": e["min"], "area": e["area"], "what": "Kill-Beteiligung" if e["result"] == "Kill" else "gestorben"}
                   for e in map_events if e["who"] == "enemy_jg"]
    early = [p["area"] for p in ej_path if 2 <= p["min"] <= 4]
    enemy_early = max(set(early), key=early.count) if early else "?"

    def diff(minute, key):
        if not enemy_jg:
            return None
        return pf(minute, pid).get(key, 0) - pf(minute, enemy_jg["participantId"]).get(key, 0)

    return {
        "match_id": match["metadata"]["matchId"], "champion": me["championName"],
        "enemy_jungler": enemy_jg["championName"] if enemy_jg else "?", "win": me["win"],
        "kda": f"{me['kills']}/{me['deaths']}/{me['assists']}",
        "jungle_cs_5": pf(5, pid).get("jungleMinionsKilled"),
        "gold_diff_10": diff(10, "totalGold"), "xp_diff_10": diff(10, "xp"),
        "early_side": early_side, "ganks_pre14": ganks, "first_action_min": first_action,
        "early_deaths": deaths_early,
        "enemy_jg_early_area": enemy_early, "enemy_jg_appearances": appearances,
        "replay": {"me": path(pid), "enemy_jg": ej_path, "events": map_events, "team": team},
        "objectives_pre20": {"ours": sum(o["ours"] for o in objectives),
                             "theirs": sum(not o["ours"] for o in objectives)},
    }


def run(api_key, platform, riot_id, count=20, queue=420):
    api = RiotAPI(api_key, platform)
    name, tag = riot_id.split("#", 1)
    acct = api.get(f"/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}")
    puuid = acct["puuid"]
    ids = api.get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids", {"queue": queue, "count": count})
    games = []
    for mid in ids:
        m = api.get(f"/lol/match/v5/matches/{mid}")
        me = next(p for p in m["info"]["participants"] if p["puuid"] == puuid)
        if me.get("teamPosition") != "JUNGLE":
            continue
        tl = api.get(f"/lol/match/v5/matches/{mid}/timeline")
        r = review_match(m, tl, puuid)
        if r:
            from .deep import deep_analyze
            r["deep"] = deep_analyze(m, tl, puuid)
            r["game_start"] = r["deep"]["game_start"]
            games.append(r)
    return {"games": games, "summary": summarize(games)}


def summarize(games):
    if not games:
        return {"tips": ["Keine Jungle-Games in den letzten Spielen gefunden."]}
    n = len(games)
    wins = sum(g["win"] for g in games)
    top = sum(g["early_side"] == "Top-Seite" for g in games)
    gd10 = [g["gold_diff_10"] for g in games if g["gold_diff_10"] is not None]
    avg_gd10 = round(sum(gd10) / len(gd10)) if gd10 else 0
    cs5 = [g["jungle_cs_5"] for g in games if g["jungle_cs_5"] is not None]
    avg_cs5 = round(sum(cs5) / len(cs5), 1) if cs5 else 0
    lanes = {}
    for g in games:
        for k in g["ganks_pre14"]:
            lanes[k["lane"]] = lanes.get(k["lane"], 0) + 1
    no_early = sum(1 for g in games if not g["ganks_pre14"])
    early_deaths = sum(g["early_deaths"] for g in games)
    obj_ours = sum(g["objectives_pre20"]["ours"] for g in games)
    obj_theirs = sum(g["objectives_pre20"]["theirs"] for g in games)

    ej_areas = {}
    for g in games:
        a = g.get("enemy_jg_early_area")
        if a and a != "?":
            ej_areas[a] = ej_areas.get(a, 0) + 1
    tips = []
    if ej_areas:
        a, c = max(ej_areas.items(), key=lambda kv: kv[1])
        if c / n >= 0.5:
            tips.append(f"Gegner-Jungler war Min 2–4 in {c}/{n} Games im Bereich „{a}“ → dort früh warden.")
    share = round(100 * max(top, n - top) / n)
    if share >= 75:
        tips.append(f"{share}% deiner Early-Games auf der gleichen Seite → du bist vorhersehbar, Start variieren.")
    if avg_gd10 < -300:
        tips.append(f"Ø {avg_gd10} Gold @10 gegen den Gegner-Jungler → Clear/Tempo ist dein Hauptproblem.")
    if avg_cs5 and avg_cs5 < 20:
        tips.append(f"Ø {avg_cs5} Jungle-CS @5 → Full Clear zu langsam oder zu früh abgebrochen.")
    if no_early / n >= 0.4:
        tips.append(f"In {no_early}/{n} Games keine Kill-Beteiligung vor Min 14 → mehr Ganks/Counter-Ganks.")
    if early_deaths / n >= 0.7:
        tips.append("Viele Tode vor Min 10 → Invades/Facechecks reduzieren.")
    if obj_theirs > obj_ours:
        tips.append(f"Objectives vor Min 20: {obj_ours} vs {obj_theirs} → Drachen/Grubs früher setup-en.")
    if not tips:
        tips.append("Solide Werte – nächster Hebel: Objective-Setups und Tracking des Gegner-Junglers.")
    return {"games": n, "winrate": round(100 * wins / n), "top_side_share": round(100 * top / n),
            "avg_gold_diff_10": avg_gd10, "avg_jungle_cs_5": avg_cs5, "ganks_by_lane": lanes, "enemy_jg_early_areas": ej_areas,
            "objectives_pre20": f"{obj_ours}:{obj_theirs}", "tips": tips}
