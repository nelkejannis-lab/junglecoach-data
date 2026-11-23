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
import os
import random
import sqlite3
import threading
import time

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


def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, check_same_thread=False, timeout=30)
    schema = """CREATE TABLE IF NOT EXISTS {t}(match_id TEXT, champ TEXT, team INT, win INT,
                  items TEXT, keystone INT, enemy TEXT, patch TEXT, created INT, tier TEXT, perks TEXT, enemies TEXT,
                  role TEXT, PRIMARY KEY(match_id, champ))"""
    db.execute(schema.format(t="games"))
    db.execute("CREATE TABLE IF NOT EXISTS seen(match_id TEXT PRIMARY KEY)")
    cols = [r[1] for r in db.execute("PRAGMA table_info(games)")]
    pk = [r[1] for r in sorted(db.execute("PRAGMA table_info(games)"), key=lambda r: r[5]) if r[5]]
    if pk != ["match_id", "champ"] or "role" not in cols:  # migrate older databases
        db.execute(schema.format(t="games_new"))
        keep = [c for c in cols if c in ("match_id", "champ", "team", "win", "items", "keystone", "enemy", "patch",
                                         "created", "tier", "perks", "enemies", "role")]
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


def store_match(db, match, tier=None, focus=None, all_players=True):
    """Stores all 10 players of a ranked match (champion, role, items, runes, lane opponent),
    so statistics exist for every champion and role. focus only steers the crawler (snowball).
    all_players=False keeps the old behaviour (only the two junglers)."""
    info = match["info"]
    mid = match["metadata"]["matchId"]
    db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (mid,))
    if info.get("gameDuration", 0) < 15 * 60:  # skip remakes / early FF
        return 0
    parts = info["participants"]
    if all_players:
        chosen = parts
    else:
        chosen = [p for p in parts if p.get("teamPosition") == "JUNGLE"]
        if len(chosen) != 2:
            return 0
    patch = patch_of(info.get("gameVersion", ""))
    for p in chosen:
        role = p.get("teamPosition") or ""
        opp = next((o for o in parts if o["teamId"] != p["teamId"] and o.get("teamPosition") == role and role), None)
        items = [p.get(f"item{i}", 0) for i in range(7) if p.get(f"item{i}", 0)]
        ks, page = _page(p)
        enemies = [o["championName"] for o in parts if o["teamId"] != p["teamId"]]
        db.execute("INSERT OR REPLACE INTO games (match_id, champ, team, win, items, keystone, enemy, patch, created, "
                   "tier, perks, enemies, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (mid, p["championName"], p["teamId"], int(bool(p.get("win"))), json.dumps(items), ks,
                    opp["championName"] if opp else "", patch, info.get("gameCreation", 0), tier,
                    json.dumps(page) if page else None, json.dumps(enemies), role))
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
            players = self._players(api)
            target = int(self.cfg.get("meta_max_matches", 1500))
            seen = {r[0] for r in db.execute("SELECT match_id FROM seen")}
            focus = set(self.cfg.get("meta_focus") or self.cfg.get("champion_pool") or [])
            queue = list(players)
            done = set()
            while queue:
                puuid, tier = queue.pop(0)
                if puuid in done:
                    continue
                done.add(puuid)
                if self.deadline and time.time() > self.deadline:
                    break
                if self._stop or st["matches"] >= target:
                    break
                st["players"] += 1
                ids = api.get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                              {"queue": 420, "count": int(self.cfg.get("meta_matches_per_player", 5))})
                for mid in ids:
                    if self._stop or st["matches"] >= target or (self.deadline and time.time() > self.deadline):
                        break
                    if mid in seen:
                        continue
                    seen.add(mid)
                    m = api.get(f"/lol/match/v5/matches/{mid}")
                    if store_match(db, m, tier, focus or None):
                        st["matches"] += 1
                        if focus and any(p["championName"] in focus for p in m["info"]["participants"]):  # snowball: players on a focus champion probably play it again
                            for p in m["info"]["participants"]:
                                if p["championName"] in focus and p["puuid"] not in done:
                                    queue.insert(0, (p["puuid"], tier))
                    st["message"] = f"{st['matches']}/{target} Games gesammelt"
            st["message"] = f"fertig – {st['matches']} neue Games"
        except Exception as e:
            st["message"] = f"Fehler: {e}"
        finally:
            st["running"] = False
            db.close()


def prune(db, keep_patches=2, max_rows=1_000_000):
    """Keep the database small: only the newest patches and at most max_rows games."""
    patches = [r[0] for r in db.execute("SELECT patch FROM games GROUP BY patch ORDER BY MAX(created) DESC")]
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
            out = []
            for o in opts:
                if o["id"] in meta_ids or o["games"] < min_games or not (1.5 <= o["pickrate"] <= 15):
                    continue
                if concentrated(o, key_fn):
                    continue
                if o["prob_better"] >= 0.9 and o["uplift"] >= 2.0:
                    pct = "≥99" if o["prob_better"] >= 0.99 else str(round(o["prob_better"] * 100))
                    o = dict(o, verdict=f"{pct}% Wahrscheinlichkeit besser, "
                                        f"+{o['uplift']}% Winrate bei nur {o['pickrate']}% Pickrate")
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

    def tierlist(self, min_games=30, bracket=None, role="JUNGLE"):
        patch = self.current_patch()
        tiers = BRACKETS.get(bracket) or ALL_TIERS
        rows = self.db.execute(
            f"SELECT champ, SUM(win), COUNT(*) FROM games WHERE patch=? AND role=? AND (tier IN ({','.join('?' * len(tiers))})"
            f"{' OR tier IS NULL' if not bracket else ''}) GROUP BY champ", (patch, role, *tiers)).fetchall()
        total = sum(r[2] for r in rows) or 1
        out = [{"champion": c, "games": n, "winrate": round(100 * w / n, 1),
                "adj_winrate": round(100 * _shrunk(w, n, 0.5), 1), "pickrate": round(100 * n / total, 1)}
               for c, w, n in rows if n >= min_games]
        return sorted(out, key=lambda x: -x["adj_winrate"])
