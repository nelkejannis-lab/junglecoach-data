"""Meta data from ranked games of ALL elos + "hidden gem" detection by simulation.

1. Crawler: samples ranked solo players evenly from Iron to Challenger via the official
   Riot API and stores the two junglers of every game (with the elo bracket) in SQLite.
2. Stats per champion & patch: meta items/keystones (most picked), matchups, tier list.
3. Hidden gems: rarely picked items/keystones are compared against the rest with a
   Bayesian Monte-Carlo simulation (Beta posteriors, 4000 draws). Only if the rare
   option beats the alternative with >= 90 % probability AND by >= 2 % winrate is it
   recommended. That is a statistical simulation on real games – not a fight simulator.
"""
import json
import math
import os
import random
import sqlite3
import threading
import time
import zlib

from .paths import DATA_DIR
from .review import RiotAPI

DB_PATH = os.path.join(DATA_DIR, "meta.sqlite")
TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
APEX = ["MASTER", "GRANDMASTER", "CHALLENGER"]
ALL_TIERS = TIERS + APEX
# elo brackets for filtering (enough games per bracket, similar play style)
BRACKETS = {"low": ["IRON", "BRONZE", "SILVER"], "mid": ["GOLD", "PLATINUM"],
            "high": ["EMERALD", "DIAMOND"], "apex": APEX}


def bracket_of(tier):
    tier = (tier or "").upper()
    return next((b for b, ts in BRACKETS.items() if tier in ts), None)


class _Rows(list):
    """Materialised query result (works like a cursor for fetchone / fetchall / iteration)."""
    rowcount = 0

    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class SafeDB:
    """One SQLite connection shared by the poll loop, HTTP threads and background simulations:
    every statement runs under a lock and returns its rows directly, so threads never interleave."""

    def __init__(self, conn):
        self._c = conn
        self._lock = threading.RLock()

    def execute(self, sql, args=()):
        with self._lock:
            cur = self._c.execute(sql, args)
            rows = _Rows(cur.fetchall()) if cur.description else _Rows()
            rows.rowcount = cur.rowcount
            return rows

    def executemany(self, sql, seq):
        with self._lock:
            return self._c.executemany(sql, seq)

    def commit(self):
        with self._lock:
            self._c.commit()

    def close(self):
        with self._lock:
            self._c.close()

    def __getattr__(self, name):
        return getattr(self._c, name)


def connect(path=None):
    db = SafeDB(sqlite3.connect(path or DB_PATH, check_same_thread=False, timeout=60))
    try:  # WAL: several crawler threads can write while others read
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    schema = """CREATE TABLE IF NOT EXISTS {t}(match_id TEXT, champ TEXT, team INT, win INT,
                  items TEXT, keystone INT, enemy TEXT, patch TEXT, created INT, tier TEXT, perks TEXT, enemies TEXT,
                  role TEXT, stats TEXT, build TEXT, PRIMARY KEY(match_id, champ))"""
    db.execute(schema.format(t="games"))
    db.execute("CREATE TABLE IF NOT EXISTS seen(match_id TEXT PRIMARY KEY)")
    # players found on a focus champion (e.g. Gwen / Graves mains): every crawl starts with them
    db.execute("""CREATE TABLE IF NOT EXISTS focus_players(puuid TEXT, region TEXT, tier TEXT, champ TEXT,
                  last_seen INT, PRIMARY KEY(puuid, region))""")
    cols = [r[1] for r in db.execute("PRAGMA table_info(games)")]
    if cols and "stats" not in cols and "role" in cols:
        db.execute("ALTER TABLE games ADD COLUMN stats TEXT")
        cols.append("stats")
    if cols and "build" not in cols and "role" in cols:  # purchase order from the match timeline
        db.execute("ALTER TABLE games ADD COLUMN build TEXT")
        cols.append("build")
    pk = [r[1] for r in sorted(db.execute("PRAGMA table_info(games)"), key=lambda r: r[5]) if r[5]]
    if pk != ["match_id", "champ"] or "role" not in cols:  # migrate older databases
        db.execute(schema.format(t="games_new"))
        keep = [c for c in cols if c in ("match_id", "champ", "team", "win", "items", "keystone", "enemy", "patch",
                                         "created", "tier", "perks", "enemies", "role", "stats", "build")]
        db.execute(f"INSERT OR IGNORE INTO games_new ({','.join(keep)}) SELECT {','.join(keep)} FROM games")
        if "role" not in cols:
            db.execute("UPDATE games_new SET role='JUNGLE' WHERE role IS NULL")
        db.execute("DROP TABLE games")
        db.execute("ALTER TABLE games_new RENAME TO games")
        db.commit()
    return db


def patch_of(version):
    return ".".join(str(version).split(".")[:2])


def _page(p):
    try:
        st = p["perks"]["styles"]
        sp = p["perks"].get("statPerks", {})
        return st[0]["selections"][0]["perk"], {
            "primary": st[0]["style"], "sub": st[1]["style"],
            "perks": [x["perk"] for x in st[0]["selections"]] + [x["perk"] for x in st[1]["selections"]]
            + [sp.get("offense", 5008), sp.get("flex", 5008), sp.get("defense", 5011)]}
    except (KeyError, IndexError):
        return 0, None


# Detail values per player for benchmarks ("how good is this compared to Gwen players in my elo").
# key -> (label, higher_is_better, unit)
METRICS = {
    "jcs10": ("Jungle-CS vor 10 Min", True, ""), "cs10": ("Lane-CS vor 10 Min", True, ""),
    "kp": ("Kill-Beteiligung", True, "%"), "vpm": ("Vision pro Minute", True, ""),
    "cw": ("Control Wards", True, ""), "dpm": ("Schaden pro Minute", True, ""),
    "gpm": ("Gold pro Minute", True, ""), "deaths": ("Tode", False, ""),
    "early_kills": ("Frühe Kills auf Lanern", True, ""), "scuttles": ("Scuttle-Krabben", True, ""),
    "enemy_jungle": ("Gegnerische Camps gefarmt", True, ""), "drag": ("Drachen-Beteiligung", True, ""),
    "dmg_share": ("Schadensanteil im Team", True, "%"), "solo_kills": ("Solo-Kills", True, ""),
    "dead_share": ("Zeit tot", False, "%"), "cc": ("CC-Zeit auf Gegnern (s)", True, ""),
}


def metrics_of(p, duration_s):
    """Comparable detail values of one participant (Riot match-v5 incl. 'challenges')."""
    ch = p.get("challenges") or {}
    mins = max(duration_s / 60, 1)
    v = {
        "jcs10": ch.get("jungleCsBefore10Minutes"), "cs10": ch.get("laneMinionsFirst10Minutes"),
        "kp": round(100 * ch["killParticipation"], 1) if ch.get("killParticipation") is not None else None,
        "vpm": round(ch["visionScorePerMinute"], 2) if ch.get("visionScorePerMinute") is not None
        else round(p.get("visionScore", 0) / mins, 2),
        "cw": ch.get("controlWardsPlaced", p.get("detectorWardsPlaced")),
        "dpm": round(ch["damagePerMinute"]) if ch.get("damagePerMinute") is not None
        else round(p.get("totalDamageDealtToChampions", 0) / mins),
        "gpm": round(ch["goldPerMinute"]) if ch.get("goldPerMinute") is not None else round(p.get("goldEarned", 0) / mins),
        "deaths": p.get("deaths"), "early_kills": ch.get("killsOnLanersEarlyJungleAsJungler"),
        "scuttles": ch.get("scuttleCrabKills"), "enemy_jungle": ch.get("enemyJungleMonsterKills"),
        "drag": ch.get("dragonTakedowns"), "solo_kills": ch.get("soloKills"),
        "dmg_share": round(100 * ch["teamDamagePercentage"], 1) if ch.get("teamDamagePercentage") is not None else None,
        "dead_share": round(100 * p.get("totalTimeSpentDead", 0) / max(duration_s, 1), 1),
        "cc": p.get("timeCCingOthers"),
    }
    return {k: (round(x, 2) if isinstance(x, float) else x) for k, x in v.items() if x is not None}


NOT_BUILD = {2003, 2010, 2031, 2033, 2055, 2138, 2139, 2140, 3340, 3363, 3364, 2052, 2422}  # potions, wards, trinkets, elixirs


def purchases(timeline):
    """participantId -> [[item_id, minute], ...]: first purchase of each item in order (undos removed)."""
    out = {}
    for fr in (timeline or {}).get("info", {}).get("frames", []):
        for ev in fr.get("events", []):
            t, pid = ev.get("type"), ev.get("participantId")
            if not pid:
                continue
            lst = out.setdefault(pid, [])
            if t == "ITEM_PURCHASED" and ev.get("itemId") not in NOT_BUILD:
                if all(x[0] != ev["itemId"] for x in lst):
                    lst.append([ev["itemId"], round(ev.get("timestamp", 0) / 60000, 1)])
            elif t == "ITEM_UNDO" and ev.get("beforeId"):
                for i in range(len(lst) - 1, -1, -1):
                    if lst[i][0] == ev["beforeId"]:
                        del lst[i]
                        break
    return out


def store_match(db, match, tier=None, focus=None, all_players=True, timeline=None):
    """Stores all 10 players of a ranked match (champion, role, items, runes, lane opponent),
    so statistics exist for every champion and role. focus only steers the crawler (snowball).
    all_players=False keeps the old behaviour (only the two junglers)."""
    info = match["info"]
    mid = match["metadata"]["matchId"]
    db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (mid,))
    if info.get("gameDuration", 0) < 15 * 60:  # skip remakes / early FF
        db.commit()
        return 0
    parts = info["participants"]
    if all_players:
        chosen = parts
    else:
        chosen = [p for p in parts if p.get("teamPosition") == "JUNGLE"]
        if len(chosen) != 2:
            return 0
    patch = patch_of(info.get("gameVersion", ""))
    bought = purchases(timeline) if timeline else {}
    dcause = {}
    if timeline:
        try:
            from .deep import death_causes_all
            dcause = death_causes_all(match, timeline)
        except Exception:  # noqa: BLE001 - analysis is optional
            dcause = {}
    for p in chosen:
        role = p.get("teamPosition") or ""
        opp = next((o for o in parts if o["teamId"] != p["teamId"] and o.get("teamPosition") == role and role), None)
        items = [p.get(f"item{i}", 0) for i in range(7) if p.get(f"item{i}", 0)]
        ks, page = _page(p)
        enemies = [o["championName"] for o in parts if o["teamId"] != p["teamId"]]
        b = bought.get(p.get("participantId"))
        db.execute("INSERT OR REPLACE INTO games (match_id, champ, team, win, items, keystone, enemy, patch, created, "
                   "tier, perks, enemies, role, stats, build) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (mid, p["championName"], p["teamId"], int(bool(p.get("win"))), json.dumps(items), ks,
                    opp["championName"] if opp else "", patch, info.get("gameCreation", 0), tier,
                    json.dumps(page) if page else None, json.dumps(enemies), role,
                    json.dumps({**metrics_of(p, info.get("gameDuration", 0)),
                                **{"dc_" + c: n for c, n in dcause.get(p.get("participantId"), {}).items()}}),
                    json.dumps(b) if b else None))
    db.commit()
    return len(chosen)


# ---------------------------------------------------------------- crawler
class Crawler:
    def __init__(self, cfg, db_path=None, deadline=None):
        self.cfg = cfg
        self.db_path = db_path
        self.deadline = deadline  # unix time: stop collecting after this (cloud crawler)
        self.state = {"running": False, "matches": 0, "players": 0, "message": "bereit"}
        self._stop = False

    def stop(self):
        self._stop = True

    def start(self):
        if self.state["running"]:
            return False
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _players(self, api):
        """Even sample over all tiers: returns list of (puuid, tier), interleaved by tier."""
        per_tier = int(self.cfg.get("meta_players_per_tier", 40))
        buckets = []
        for tier in self.cfg.get("meta_tiers", ALL_TIERS):
            ps = []
            if tier in APEX:
                lg = api.get_platform(f"/lol/league/v4/{tier.lower()}leagues/by-queue/RANKED_SOLO_5x5")
                ps = [e["puuid"] for e in lg.get("entries", []) if e.get("puuid")]
            else:
                for div in ("I", "II", "III", "IV"):
                    page = random.randint(1, 3)
                    ps += [e["puuid"] for e in api.get_platform(
                        f"/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{div}", {"page": page}) if e.get("puuid")]
            random.shuffle(ps)
            buckets.append([(p, tier) for p in ps[:per_tier]])
        out = []  # interleave so a stopped crawl still covers every elo
        for i in range(max((len(b) for b in buckets), default=0)):
            out += [b[i] for b in buckets if i < len(b)]
        return out

    def _run(self):
        st = self.state
        st.update(running=True, message="lade Spielerliste…")
        db = connect(self.db_path)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            api = RiotAPI(self.cfg["riot_api_key"], self.cfg.get("platform", "euw1"))
            region = self.cfg.get("platform", "euw1")
            focus = set(self.cfg.get("meta_focus") or self.cfg.get("champion_pool") or [])
            known = []
            if focus:  # known mains first (most recently seen), then the even elo sample
                known = [(p, t, True) for p, t in db.execute(
                    "SELECT puuid, tier FROM focus_players WHERE region=? ORDER BY last_seen DESC LIMIT ?",
                    (region, int(self.cfg.get("meta_focus_players", 400))))]
                random.shuffle(known)
            players = self._players(api)
            target = int(self.cfg.get("meta_max_matches", 1500))
            seen = {r[0] for r in db.execute("SELECT match_id FROM seen")}
            queue = known + [(p, t, False) for p, t in players]
            done = set()
            while queue:
                puuid, tier, is_focus = queue.pop(0)
                if puuid in done:
                    continue
                done.add(puuid)
                if self.deadline and time.time() > self.deadline:
                    break
                if self._stop or st["matches"] >= target:
                    break
                st["players"] += 1
                ids = api.get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                              {"queue": 420, "count": int(self.cfg.get("meta_focus_matches", 25)) if is_focus
                               else int(self.cfg.get("meta_matches_per_player", 5)),
                               "startTime": int(time.time()) - 10 * 86400})  # only recent games (current patch)
                for mid in ids:
                    if self._stop or st["matches"] >= target or (self.deadline and time.time() > self.deadline):
                        break
                    if mid in seen:
                        continue
                    seen.add(mid)
                    m = api.get(f"/lol/match/v5/matches/{mid}")
                    tl = None
                    if self.cfg.get("meta_timelines") and m.get("info", {}).get("gameDuration", 0) >= 15 * 60:
                        try:  # exact purchase order (costs one extra request per match)
                            tl = api.get(f"/lol/match/v5/matches/{mid}/timeline")
                        except Exception:  # noqa: BLE001
                            tl = None
                    if store_match(db, m, tier, focus or None, timeline=tl):
                        st["matches"] += 1
                        if focus and any(p["championName"] in focus for p in m["info"]["participants"]):  # snowball: players on a focus champion probably play it again
                            for p in m["info"]["participants"]:
                                if p["championName"] in focus:
                                    db.execute("INSERT OR REPLACE INTO focus_players VALUES (?,?,?,?,?)",
                                               (p["puuid"], region, tier, p["championName"], int(time.time())))
                                    db.commit()  # never hold the write lock during API calls
                                    if p["puuid"] not in done:
                                        queue.insert(0, (p["puuid"], tier, True))
                    st["message"] = f"{st['matches']}/{target} Games gesammelt"
            db.commit()
            st["message"] = f"fertig – {st['matches']} neue Games"
        except Exception as e:
            st["message"] = f"Fehler: {e}"
        finally:
            st["running"] = False
            db.close()


def prune(db, keep_patches=2, max_rows=1_000_000, enough_current=150_000):
    """Keep only current data: the newest patch, plus the previous one while the new patch
    has fewer than enough_current rows (start of a patch). At most max_rows rows."""
    patches = [r[0] for r in db.execute("SELECT patch FROM games GROUP BY patch ORDER BY MAX(created) DESC")]
    if patches:
        cur = db.execute("SELECT COUNT(*) FROM games WHERE patch=?", (patches[0],)).fetchone()[0]
        if cur >= enough_current:
            keep_patches = 1
    for old in patches[keep_patches:]:
        db.execute("DELETE FROM games WHERE patch=?", (old,))
    db.execute("""DELETE FROM games WHERE rowid IN (SELECT rowid FROM games ORDER BY created DESC LIMIT -1 OFFSET ?)""",
               (max_rows,))
    db.commit()


def merge_from(db, other_path):
    """Adds all games of another meta database (e.g. the daily cloud download)."""
    db.execute("ATTACH DATABASE ? AS other", (other_path,))
    cols = [r[1] for r in db.execute("PRAGMA other.table_info(games)")]
    mine = {r[1] for r in db.execute("PRAGMA main.table_info(games)")}
    use = ", ".join(c for c in cols if c in mine)
    before = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    db.execute(f"INSERT OR IGNORE INTO games ({use}) SELECT {use} FROM other.games")
    db.execute("INSERT OR IGNORE INTO seen SELECT match_id FROM other.seen")
    db.commit()
    db.execute("DETACH DATABASE other")
    return db.execute("SELECT COUNT(*) FROM games").fetchone()[0] - before


# ---------------------------------------------------------------- stats
def percentile(sorted_vals, x):
    """Share of values below x (ties count half), 0-100."""
    import bisect
    if not sorted_vals:
        return None
    lo = bisect.bisect_left(sorted_vals, x)
    hi = bisect.bisect_right(sorted_vals, x)
    return round(100 * (lo + (hi - lo) / 2) / len(sorted_vals))


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def _beta_prob_better(w1, n1, w2, n2, draws=4000, seed=7):
    """Monte-Carlo: P(p1 > p2) with Beta(1+w, 1+l) posteriors, plus mean uplift."""
    rng = random.Random(seed)
    better, up = 0, 0.0
    for _ in range(draws):
        a = rng.betavariate(1 + w1, 1 + n1 - w1)
        b = rng.betavariate(1 + w2, 1 + n2 - w2)
        better += a > b
        up += a - b
    return better / draws, up / draws


# bought BECAUSE you are ahead (stacking items) – their winrate says nothing about the item itself
SNOWBALL_ITEMS = {3041}  # Mejai's Soulstealer


def _stratified_effect(rows, has, stratum):
    """Item winrate corrected for 'winners simply buy more items': holders are compared with
    non-holders WITHIN the same stratum (number of completed items at game end), like a
    Mantel-Haenszel comparison. Returns (prob_better, uplift, n_holders) with a normal approximation."""
    strata = {}
    for r in rows:
        s = strata.setdefault(stratum(r), [0, 0, 0, 0])  # holder wins, holders, other wins, others
        if has(r):
            s[0] += r[0]; s[1] += 1
        else:
            s[2] += r[0]; s[3] += 1
    obs = exp = var = 0.0
    n1 = 0
    for w1, a, w2, b in strata.values():
        if not a or b < 3:  # no comparable non-holders in this stratum -> no evidence from it
            continue
        p = (w2 + 1) / (b + 2)
        obs += w1; exp += a * p; n1 += a
        var += a * p * (1 - p) + a * a * p * (1 - p) / (b + 2)  # holder noise + uncertainty of the baseline
    if not n1 or var <= 0:
        return 0.5, 0.0, n1
    z = (obs - exp) / math.sqrt(var)
    return 0.5 * (1 + math.erf(z / math.sqrt(2))), (obs - exp) / n1, n1


def _shrunk(w, n, base, k=20):
    return (w + k * base) / (n + k) if n + k else base


class MetaStats:
    def __init__(self, gd, db=None):
        self.role = None
        self.gd = gd
        self.db = db or connect()

    def patches(self):
        return [r[0] for r in self.db.execute(
            "SELECT patch, COUNT(*) c FROM games GROUP BY patch ORDER BY created DESC")]

    def current_patch(self):
        r = self.db.execute("SELECT patch FROM games ORDER BY created DESC LIMIT 1").fetchone()
        return r[0] if r else None

    def total(self, patch=None):
        q = "SELECT COUNT(*) FROM games" + (" WHERE patch=?" if patch else "")
        return self.db.execute(q, (patch,) if patch else ()).fetchone()[0]

    def main_role(self, champ):
        r = self.db.execute("SELECT role, COUNT(*) c FROM games WHERE champ=? GROUP BY role ORDER BY c DESC LIMIT 1",
                            (champ,)).fetchone()
        return r[0] if r else None

    def _rows(self, champ, bracket=None, min_games=60, role=None):
        """Widens the scope step by step until there is enough data:
        patch+elo bracket -> patch (all elos) -> all patches+bracket -> everything.
        With a role (e.g. Gwen TOP vs Gwen JUNGLE) only games in that role count."""
        role = role or getattr(self, "role", None)
        if role:
            self.role = None
            try:
                rows, scope = self._rows(champ, bracket, min_games)
            finally:
                self.role = role
            rrows = [r for r in rows if r[6] == role]
            if len(rrows) >= min(30, min_games):
                return rrows, scope + f", Rolle {role.title()}"
            return rows, scope
        patch = self.current_patch()
        tiers = BRACKETS.get(bracket) if bracket else None
        cols = "win, items, keystone, enemy, perks, enemies, role"
        tq = f" AND tier IN ({','.join('?' * len(tiers))})" if tiers else ""
        attempts = []
        if tiers:
            attempts.append((f"WHERE champ=? AND patch=?{tq}", [champ, patch, *tiers], f"Patch {patch}, Elo {bracket}"))
        attempts.append(("WHERE champ=? AND patch=?", [champ, patch], f"Patch {patch}, alle Elos"))
        if tiers:
            attempts.append((f"WHERE champ=?{tq}", [champ, *tiers], f"alle Patches, Elo {bracket}"))
        attempts.append(("WHERE champ=?", [champ], "alle gesammelten Games"))
        rows, scope = [], attempts[-1][2]
        for where, args, label in attempts:
            rows = self.db.execute(f"SELECT {cols} FROM games {where}", args).fetchall()
            scope = label
            if len(rows) >= min_games:
                break
        return [(w, json.loads(i), k, e, json.loads(pk) if pk else None, set(json.loads(en)) if en else {e}, ro)
                for w, i, k, e, pk, en, ro in rows], scope

    @staticmethod
    def best_page(rows, keystone):
        """Most played full rune page (with winrate) for a keystone."""
        pages = {}
        for r in rows:
            pg = r[4]
            if pg and pg["perks"] and pg["perks"][0] == keystone:
                key = json.dumps(pg, sort_keys=True)
                c = pages.setdefault(key, [0, 0]); c[0] += r[0]; c[1] += 1
        if not pages:
            return None
        key, (w, n) = max(pages.items(), key=lambda kv: (kv[1][1], kv[1][0]))
        return dict(json.loads(key), games=n, winrate=round(100 * w / n, 1))

    def matchup(self, champ_id, enemy_id, bracket=None):
        """What works best for champ vs this enemy jungler. Small samples, so every option is
        shrunk toward its overall winrate for the champion (empirical Bayes, k=8) and compared
        by simulation against the champion's other options in this matchup."""
        rows, scope = self._rows(champ_id, bracket)
        mrows = [r for r in rows if r[3] == enemy_id]
        if len(mrows) < 8:
            return {"enemy": enemy_id, "games": len(mrows), "enough": False, "scope": scope}
        n, w = len(mrows), sum(r[0] for r in mrows)
        completed = self.gd.completed_items()
        k = 8

        def rank(key_fn, keys, label_fn):
            out = []
            for key in keys:
                g_all = [r for r in rows if key_fn(r, key)]
                g = [r for r in mrows if key_fn(r, key)]
                if len(g) < 4 or not g_all:
                    continue
                prior = sum(r[0] for r in g_all) / len(g_all)
                w1, n1 = sum(r[0] for r in g), len(g)
                rest_w, rest_n = w - w1, n - n1
                rng = random.Random(11)
                better = 0
                for _ in range(3000):
                    a = rng.betavariate(1 + w1 + k * prior, 1 + n1 - w1 + k * (1 - prior))
                    b = rng.betavariate(1 + rest_w, 1 + rest_n - rest_w) if rest_n else 0.5
                    better += a > b
                out.append({"id": key, "name": label_fn(key), "games": n1, "winrate": round(100 * w1 / n1, 1),
                            "adj_winrate": round(100 * (w1 + k * prior) / (n1 + k), 1),
                            "prob_better": round(better / 3000, 2)})
            return sorted(out, key=lambda x: (-x["adj_winrate"], -x["games"]))

        items = rank(lambda r, i: i in r[1], {i for r in mrows for i in r[1] if i in completed
                                               and "Boots" not in completed[i].get("tags", [])}, self.gd.item_name)
        keys = rank(lambda r, ks: r[2] == ks, {r[2] for r in mrows if r[2]},
                    lambda ks: self.gd.rune_disp.get(ks, str(ks)))
        best_ks = keys[0]["id"] if keys else None
        return {"enemy": enemy_id, "games": n, "enough": True, "scope": scope,
                "winrate": round(100 * w / n, 1), "items": items[:4], "keystones": keys[:3],
                "rune_page": self.best_page(mrows, best_ks) or self.best_page(rows, best_ks) if best_ks else None}

    def team_fit(self, champ_id, enemy_ids, bracket=None, min_option_games=15):
        """Which keystone / items fit best against the WHOLE enemy team.

        Additive model with shrinkage: expected winrate of an option =
          its overall winrate for this champion
          + per enemy champion: the option's extra effect vs that champion (interaction),
            shrunk toward 0 when there is little data (k=10).
        Options are compared on the summed estimate; contributions are reported per enemy."""
        rows, scope = self._rows(champ_id, bracket)
        enemy_ids = [e for e in enemy_ids if e]
        if len(rows) < 30 or not enemy_ids:
            return {"enough": False, "games": len(rows), "scope": scope}
        n, w = len(rows), sum(r[0] for r in rows)
        base = w / n
        base_e = {}
        for e in enemy_ids:
            re_ = [r for r in rows if e in r[5]]
            base_e[e] = (_shrunk(sum(r[0] for r in re_), len(re_), base, 20), len(re_))
        completed = self.gd.completed_items()

        def evaluate(key_fn, keys, label_fn):
            out = []
            for key in keys:
                ro = [r for r in rows if key_fn(r, key)]
                if len(ro) < min_option_games:
                    continue
                overall = _shrunk(sum(r[0] for r in ro), len(ro), base, 20)
                score, contrib = overall, []
                for e in enemy_ids:
                    be, ne = base_e[e]
                    roe = [r for r in ro if e in r[5]]
                    expected = overall + (be - base)          # no interaction
                    adj = _shrunk(sum(r[0] for r in roe), len(roe), expected, 10)
                    delta = max(-0.08, min(0.08, adj - expected))  # cap: no single enemy dominates
                    score += delta
                    if abs(delta) >= 0.01:
                        contrib.append({"enemy": e, "delta": round(100 * delta, 1), "games": len(roe)})
                score = max(0.30, min(0.70, score))  # keep estimates in a realistic range
                out.append({"id": key, "name": label_fn(key), "games": len(ro),
                            "overall": round(100 * overall, 1), "score": round(100 * score, 1),
                            "vs": sorted(contrib, key=lambda c: -abs(c["delta"]))[:3]})
            return sorted(out, key=lambda x: -x["score"])

        keys = evaluate(lambda r, k: r[2] == k, {r[2] for r in rows if r[2]},
                        lambda k: self.gd.rune_disp.get(k, str(k)))
        items = evaluate(lambda r, i: i in r[1], {i for r in rows for i in r[1] if i in completed
                                                  and "Boots" not in completed[i].get("tags", [])}, self.gd.item_name)
        boots = evaluate(lambda r, i: i in r[1], {i for r in rows for i in r[1] if i in completed
                                                  and "Boots" in completed[i].get("tags", [])}, self.gd.item_name)
        best_ks = keys[0]["id"] if keys else None
        vs_team = [r for r in rows if r[5] & set(enemy_ids)]
        page = (self.best_page(vs_team, best_ks) or self.best_page(rows, best_ks)) if best_ks else None
        return {"enough": True, "games": n, "scope": scope, "enemies": enemy_ids,
                "keystones": keys[:3], "items": items[:5], "boots": boots[:1], "rune_page": page}

    def _exact_path(self, champ_id, bracket=None, role=None, min_games=40):
        """Build path from real purchase order (timeline data from the crawler), if enough games have it."""
        patch = self.current_patch()
        tiers = BRACKETS.get(bracket) if bracket else None
        rq = " AND role=?" if role else ""
        tries = []
        if tiers:
            tries.append((f" AND patch=? AND tier IN ({','.join('?' * len(tiers))})", [patch, *tiers], f"Patch {patch}, Elo {bracket}"))
        tries += [(" AND patch=?", [patch], f"Patch {patch}, alle Elos"), ("", [], "alle gesammelten Games")]
        rows, scope = [], ""
        for where, args, label in tries:
            rows = self.db.execute(f"SELECT win, build FROM games WHERE champ=? AND build IS NOT NULL{rq}{where}",
                                   [champ_id] + ([role] if role else []) + args).fetchall()
            scope = label + (f", Rolle {role.title()}" if role else "")
            if len(rows) >= min_games:
                break
        if len(rows) < min_games:
            return None
        completed = self.gd.completed_items()
        boots = {i for i, it in completed.items() if "Boots" in it.get("tags", [])}
        legend = {i for i in completed if i not in boots and i not in SNOWBALL_ITEMS}
        games = []
        for w, b in rows:
            seq = json.loads(b)
            games.append((w, [(i, m) for i, m in seq if i in legend], [(i, m) for i, m in seq if i in boots]))
        base = sum(g[0] for g in games) / len(games)
        core, alts = [], []
        for step in range(3):
            prefix = [c["id"] for c in core]
            pool = [g for g in games if len(g[1]) > step and [i for i, _ in g[1][:step]] == prefix]
            cnt = {}
            for g in pool:
                i, mnt = g[1][step]
                c = cnt.setdefault(i, [0, 0, 0.0])
                c[0] += 1; c[1] += g[0]; c[2] += mnt
            if not cnt:
                break
            opts = sorted(({"id": i, "name": self.gd.item_name(i), "share": round(100 * n / len(pool), 1), "games": n,
                            "winrate": round(100 * w / n, 1), "minute": round(t / n), "score": _shrunk(w, n, base, k=40)}
                           for i, (n, w, t) in cnt.items()), key=lambda o: -o["share"])
            best = opts[0]
            for o in opts[1:]:  # consensus first; winrate only between almost equally popular options
                if o["share"] >= 0.75 * best["share"] and o["games"] >= 80 and o["score"] >= best["score"] + 0.02:
                    best = o
            core.append(dict(best, step=step + 1))
            alts.append({"step": step + 1, "options": [o for o in opts if o is not best and o["share"] >= 5][:2]})
        late = {}
        full = [g for g in games if [i for i, _ in g[1][:3]] == [c["id"] for c in core]]
        for g in full:
            for i, _ in g[1][3:5]:
                late[i] = late.get(i, 0) + 1
        bc = {}
        for g in games:
            if g[2]:
                i, mnt = g[2][0]
                c = bc.setdefault(i, [0, 0.0]); c[0] += 1; c[1] += mnt
        bt = max(bc.items(), key=lambda kv: kv[1][0], default=None)
        return {"enough": bool(core), "exact": True, "games": len(rows), "scope": scope, "core": core,
                "alternatives": alts, "next": None,
                "late": [{"id": i, "name": self.gd.item_name(i), "share": round(100 * n / max(1, len(full)), 1)}
                         for i, n in sorted(late.items(), key=lambda kv: -kv[1])[:4]],
                "boots": {"id": bt[0], "name": self.gd.item_name(bt[0]), "share": round(100 * bt[1][0] / len(games), 1),
                          "minute": round(bt[1][1] / bt[1][0])} if bt else None}

    def build_path(self, champ_id, bracket=None, role=None):
        """Ordered core build from real games (Riot stores only the end inventory, no purchase order).

        Order estimate: games that ended with exactly ONE finished item show the usual first item,
        games with two finished items (incl. the chosen first) the second, and so on. Per step the
        popular options (>= 40 % of the most popular) compete on winrate in comparable games
        (same number of finished items, shrunk toward the champion's winrate) – so the build is a
        path real players take, not a list of unrelated high-winrate items."""
        prev = getattr(self, "role", None)
        self.role = role or prev
        try:
            rows, scope = self._rows(champ_id, bracket)
        finally:
            self.role = prev
        exact = self._exact_path(champ_id, bracket, role or prev)
        if exact:
            return exact
        if len(rows) < 40:
            return {"enough": False, "games": len(rows), "scope": scope}
        completed = self.gd.completed_items()
        boots = {i for i, it in completed.items() if "Boots" in it.get("tags", [])}
        legend = {i for i in completed if i not in boots and i not in SNOWBALL_ITEMS}
        games = [(r[0], [i for i in r[1] if i in legend], [i for i in r[1] if i in boots]) for r in rows]
        base = sum(g[0] for g in games) / len(games)

        def pick(step, chosen):
            exact = [g for g in games if len(g[1]) == step and all(c in g[1] for c in chosen)]
            pool = exact if len(exact) >= 25 else [g for g in games if len(g[1]) >= step and all(c in g[1] for c in chosen)]
            counts = {}
            for g in pool:
                for i in g[1]:
                    if i not in chosen:
                        counts[i] = counts.get(i, 0) + (1 if g in exact else 1 / max(1, len(g[1]) - len(chosen)))
            if not counts:
                return None, []
            top = max(counts.values())
            total = sum(counts.values())
            opts = []
            for i, c in counts.items():
                if c < 0.3 * top:
                    continue
                comp = [g for g in games if i in g[1] and all(x in g[1] for x in chosen) and len(g[1]) in (step, step + 1)]
                w, n = sum(g[0] for g in comp), len(comp)
                opts.append({"id": i, "name": self.gd.item_name(i), "share": round(100 * c / total, 1), "games": n,
                             "winrate": round(100 * w / n, 1) if n else None,
                             "score": _shrunk(w, n, base, k=40)})
            # consensus first (robust), winrate only decides between almost equally popular options:
            # games that end with 1-2 items are short/one-sided, their winrates alone are too noisy
            opts.sort(key=lambda o: -o["share"])
            best = opts[0] if opts else None
            for o in opts[1:]:
                if o["share"] >= 0.75 * best["share"] and o["games"] >= 80 and o["score"] >= best["score"] + 0.02:
                    best = o
            return best, [o for o in opts if o is not best][:2]

        core, alts = [], []
        for step in (1, 2, 3):
            best, other = pick(step, [c["id"] for c in core])
            if not best:
                break
            core.append(dict(best, step=step))
            alts.append({"step": step, "options": other})
        late, _ = pick(4, [c["id"] for c in core]) if len(core) == 3 else (None, [])
        options = []
        if len(core) == 3:
            pool = [g for g in games if len(g[1]) >= 4 and all(c["id"] in g[1] for c in core)]
            cnt = {}
            for g in pool:
                for i in g[1]:
                    if all(i != c["id"] for c in core):
                        cnt[i] = cnt.get(i, 0) + 1
            for i, c in sorted(cnt.items(), key=lambda kv: -kv[1])[:4]:
                options.append({"id": i, "name": self.gd.item_name(i), "share": round(100 * c / max(len(pool), 1), 1)})
        bc = {}
        for g in games:
            for b in g[2]:
                bc[b] = bc.get(b, [0, 0]); bc[b][0] += g[0]; bc[b][1] += 1
        bt = max(bc.items(), key=lambda kv: kv[1][1], default=None)
        return {"enough": bool(core), "games": len(rows), "scope": scope, "core": core, "alternatives": alts,
                "late": options, "next": late,
                "boots": {"id": bt[0], "name": self.gd.item_name(bt[0]), "share": round(100 * bt[1][1] / len(games), 1)}
                if bt else None}

    def champion(self, champ_id, min_games=25, bracket=None):
        """Meta build, keystone, hidden gems, matchups for one champion (Data Dragon id)."""
        rows, scope = self._rows(champ_id, bracket)
        n = len(rows)
        if n < 15:
            return {"champion": champ_id, "games": n, "enough": False, "scope": scope}
        wins = sum(r[0] for r in rows)
        base = wins / n
        completed = self.gd.completed_items()
        boots = {i for i, it in completed.items() if "Boots" in it.get("tags", [])}

        def option_stats(key_fn, keys):
            out = []
            for k in keys:
                with_ = [r for r in rows if key_fn(r, k)]
                if not with_:
                    continue
                w1, n1 = sum(r[0] for r in with_), len(with_)
                w2, n2 = wins - w1, n - n1
                prob, uplift = _beta_prob_better(w1, n1, w2, n2) if n2 else (0.5, 0.0)
                out.append({"id": k, "games": n1, "pickrate": round(100 * n1 / n, 1),
                            "winrate": round(100 * w1 / n1, 1), "prob_better": round(prob, 3),
                            "uplift": round(100 * uplift, 1)})
            return out

        all_items = {i for r in rows for i in r[1] if i in completed}
        items = option_stats(lambda r, k: k in r[1], all_items)
        legend_ids = {i for i, it in completed.items() if "Boots" not in it.get("tags", [])}
        # (win, item set, stratum, half): stratum = completed items at game end, half = fixed split
        prep = [(r[0], set(r[1]), sum(1 for i in r[1] if i in legend_ids),
                 zlib.crc32((str(r[1]) + str(r[3])).encode()) % 2) for r in rows]
        halves = [[x for x in prep if x[3] == h] for h in (0, 1)]
        for it in items:  # end-of-game inventory is biased toward long/won games -> compare like with like
            k = it["id"]
            prob, up, _ = _stratified_effect(prep, lambda r: k in r[1], lambda r: r[2])
            it["raw_winrate"] = it["winrate"]
            it["prob_better"], it["uplift"] = round(prob, 3), round(100 * up, 1)
            it["replicates"] = all(_stratified_effect(hh, lambda r: k in r[1], lambda r: r[2])[1] > 0 for hh in halves)
            it["snowball"] = k in SNOWBALL_ITEMS
        for it in items:
            it["name"] = self.gd.item_name(it["id"])
            it["boots"] = it["id"] in boots
        legendary = sorted([i for i in items if not i["boots"]], key=lambda x: -x["pickrate"])
        meta_core = legendary[:3]
        meta_boots = max((i for i in items if i["boots"]), key=lambda x: x["pickrate"], default=None)

        keystones = option_stats(lambda r, k: r[2] == k, {r[2] for r in rows if r[2]})
        for k in keystones:
            k["name"] = self.gd.rune_disp.get(k["id"], str(k["id"]))
        keystones.sort(key=lambda x: -x["pickrate"])

        def concentrated(o, key_fn):
            """Matchup pick, not a general gem? True if the advantage disappears once the games
            against one specific enemy champion (any role) are removed."""
            ro = [r for r in rows if key_fn(r, o["id"])]
            counts = {}
            for r in ro:
                for e in r[5]:
                    counts[e] = counts.get(e, 0) + 1
            for e, c in counts.items():
                share_all = sum(1 for r in rows if e in r[5]) / len(rows)
                if c < 0.15 * len(ro) or share_all >= 0.5:  # rare here, or in most games anyway
                    continue
                a = [r for r in ro if e not in r[5]]
                b = [r for r in rows if not key_fn(r, o["id"]) and e not in r[5]]
                if len(a) < 10 or not b:
                    return e
                prob, up = _beta_prob_better(sum(r[0] for r in a), len(a), sum(r[0] for r in b), len(b), draws=1500)
                if prob < 0.9 or up < 0.015:  # must stay a clear gem without that matchup
                    return e
            return None

        def gems(opts, meta_ids, key_fn):
            cand = [o for o in opts if o["id"] not in meta_ids and o["games"] >= min_games and 1.5 <= o["pickrate"] <= 15]
            # many options are searched -> stricter bar (Bonferroni-like): 90 % for one, up to 99 % for many
            bar = min(0.99, 1 - 0.1 / max(1, len(cand)) ** 0.5)
            out = []
            for o in cand:
                if o.get("replicates") is False or o.get("snowball") or concentrated(o, key_fn):
                    continue
                if o["prob_better"] >= bar and o["uplift"] >= 2.0:
                    o = dict(o, tested=len(cand), verdict=(
                        f"Bei gleicher Item-Anzahl +{o['uplift']}% Winrate (Pickrate {o['pickrate']}%, "
                        f"{o['games']} Games, in beiden Datenhälften bestätigt, {len(cand)} Optionen geprüft). "
                        "Zusammenhang – kein Beweis, dass das Item selbst den Sieg bringt."))
                    out.append(o)
            return sorted(out, key=lambda x: (-x["prob_better"], -x["uplift"]))

        hidden_items = gems([i for i in items if not i["boots"]], {m["id"] for m in meta_core}, lambda r, k: k in r[1])
        hidden_keys = gems(keystones, {keystones[0]["id"]} if keystones else set(), lambda r, k: r[2] == k)

        mu = {}
        for w, _, _, e, _, _, _ in rows:
            m = mu.setdefault(e, [0, 0])
            m[0] += w; m[1] += 1
        matchups = [{"enemy": e, "games": c, "winrate": round(100 * w / c, 1),
                     "adj_winrate": round(100 * _shrunk(w, c, base, 10), 1)}
                    for e, (w, c) in mu.items() if c >= 5]
        matchups.sort(key=lambda x: x["adj_winrate"])
        rec_ks = (hidden_keys[0]["id"] if hidden_keys else None) or (keystones[0]["id"] if keystones else None)
        return {"champion": champ_id, "games": n, "enough": True, "scope": scope,
                "rune_page": self.best_page(rows, rec_ks) if rec_ks else None,
                "winrate": round(100 * base, 1), "meta_core": meta_core, "meta_boots": meta_boots,
                "meta_keystone": keystones[0] if keystones else None, "keystones": keystones[:5],
                "hidden_items": hidden_items[:3], "hidden_keystones": hidden_keys[:2],
                "worst_matchups": matchups[:5], "best_matchups": matchups[::-1][:5]}

    def role_total(self, role):
        r = self.db.execute("SELECT COUNT(*) FROM games WHERE role=? AND patch=?", (role, self.current_patch())).fetchone()
        return r[0] if r else 0

    def benchmarks(self, champ, role="JUNGLE", bracket=None, min_games=30):
        """Distribution of every detail value for this champion + role (+ elo), widening the scope
        (elo -> all elos -> role over all champions) until there is enough data."""
        tiers = BRACKETS.get(bracket) if bracket else None
        tries = []
        if tiers:
            tries.append(("champ=? AND role=? AND tier IN (%s)" % ",".join("?" * len(tiers)), [champ, role, *tiers],
                          f"{champ} {role.title()} in deinem Elo"))
        tries.append(("champ=? AND role=?", [champ, role], f"{champ} {role.title()}, alle Elos"))
        if tiers:
            tries.append(("role=? AND tier IN (%s)" % ",".join("?" * len(tiers)), [role, *tiers],
                          f"alle {role.title()}-Spieler in deinem Elo"))
        tries.append(("role=?", [role], f"alle {role.title()}-Spieler"))
        for where, args, label in tries:
            rows = self.db.execute(f"SELECT stats, win FROM games WHERE stats IS NOT NULL AND {where}", args).fetchall()
            if len(rows) >= min_games:
                dist = {}
                for st, _ in rows:
                    for k, v in json.loads(st).items():
                        dist.setdefault(k, []).append(v)
                return {"scope": label, "games": len(rows), "dist": {k: sorted(v) for k, v in dist.items() if len(v) >= min_games}}
        return None

    def death_benchmark(self, role="JUNGLE", bracket=None, min_games=40):
        """Deaths per game by cause for BETTER players: winners of the next higher elo bracket (or own bracket
        at the top), same role. Only games crawled with timeline carry death causes."""
        order = ["low", "mid", "high", "apex"]
        b = bracket if bracket in order else "mid"
        better = order[min(order.index(b) + 1, len(order) - 1)]
        for tiers, label in ((BRACKETS[better], f"Gewinner in {better.title()}-Elo"), (ALL_TIERS, "Gewinner aller Elos")):
            rows = self.db.execute(f"SELECT stats FROM games WHERE win=1 AND role=? AND stats LIKE '%dc_%' "
                                   f"AND tier IN ({','.join('?' * len(tiers))})", (role, *tiers)).fetchall()
            if len(rows) >= min_games:
                tot = {}
                for (st,) in rows:
                    for k, v in json.loads(st).items():
                        if k.startswith("dc_"):
                            tot[k[3:]] = tot.get(k[3:], 0) + v
                return {"scope": label, "games": len(rows), "per_game": {k: round(v / len(rows), 2) for k, v in tot.items()}}
        return None

    def tierlist(self, min_games=30, bracket=None, role="JUNGLE"):
        patch = self.current_patch()
        tiers = BRACKETS.get(bracket) or ALL_TIERS
        rows = self.db.execute(
            f"SELECT champ, SUM(win), COUNT(*) FROM games WHERE patch=? AND role=? AND (tier IN ({','.join('?' * len(tiers))})"
            f"{' OR tier IS NULL' if not bracket else ''}) GROUP BY champ", (patch, role, *tiers)).fetchall()
        total = sum(r[2] for r in rows) or 1
        matches = max(total / 2, 1)  # two players per role and match
        out = [{"champion": c, "games": n, "winrate": round(100 * w / n, 1),
                "adj_winrate": round(100 * _shrunk(w, n, 0.5), 1), "pickrate": round(100 * n / matches, 1)}
               for c, w, n in rows if n >= min_games]
        conf = {c: round(100 * _shrunk(w, n, 0.5, k=60), 2) for c, w, n in rows}  # tiers need more evidence
        out.sort(key=lambda x: -(conf[x["champion"]] + min(x["pickrate"], 20) * 0.05))
        for i, t in enumerate(out):  # op.gg-like tiers by rank
            q = i / max(len(out), 1)
            t["tier"] = "S" if q < 0.1 else "A" if q < 0.3 else "B" if q < 0.6 else "C" if q < 0.85 else "D"
            t["rank"] = i + 1
        return out
