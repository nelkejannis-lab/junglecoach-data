"""Cloud crawler: collects ranked jungle games of all elos from several regions in parallel
(each region has its own Riot rate limit) until the time budget is used up."""
import argparse
import gzip
import os
import shutil
import threading
import time

from jc import meta

ap = argparse.ArgumentParser()
ap.add_argument("--minutes", type=float, default=330)
ap.add_argument("--db", default="meta.sqlite")
ap.add_argument("--regions", default="euw1,na1,kr,oc1")  # one per Riot routing region = 4x the rate limit
a = ap.parse_args()

from jc.review import RiotAPI

# 1st choice: the key the admin set in the Moosi app (fetched by the workflow), 2nd: the repo secret
key, err = None, None
for cand in (os.environ.get("SERVER_RIOT_KEY", ""), os.environ.get("RIOT_API_KEY", "")):
    cand = cand.strip()
    if not cand:
        continue
    try:
        RiotAPI(cand, "euw1").get_platform("/lol/status/v4/platform-data")
        key = cand
        break
    except Exception as e:  # noqa: BLE001
        err = e
if not key:  # fail fast with a clear message (GitHub then e-mails the repo owner) instead of crawling nothing
    raise SystemExit(f"Kein gültiger Riot-Key ({err}). Neuen Key in der Moosi-App unter Mehr > Admin eintragen.")
# champions to collect (all roles); empty = all junglers
FOCUS = [c.strip() for c in os.environ.get("FOCUS", "Gwen,Graves").split(",") if c.strip()]
deadline = time.time() + a.minutes * 60
MAX_MATCHES = int(os.environ.get("MAX_MATCHES", "250000"))   # Silver+ in 6 equal elo buckets
db = meta.connect(a.db)
before = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
start_rowid = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM games").fetchone()[0]
db.close()


def run(region):
    while time.time() < deadline - 60:  # new player sample per round
        cfg = {"riot_api_key": key, "platform": region, "meta_max_matches": 10 ** 9,
               "meta_players_per_tier": 60, "meta_matches_per_player": 10, "meta_focus": FOCUS,
               "meta_focus_matches": 25, "meta_focus_players": 400,
               "meta_timelines": True,
               "meta_tiers": meta.CRAWL_TIERS, "meta_balance": True}  # Silver+ only, every elo bucket equally  # exact purchase order (1 extra request per match -> ~half the games)
        cr = meta.Crawler(cfg, db_path=a.db, deadline=deadline)
        cr._run()
        print(f"[{region}] {cr.state['message']}", flush=True)
        if "Fehler" in cr.state["message"]:
            if "API Key" in cr.state["message"]:
                return
            time.sleep(30)


regions = a.regions.split(",")
if key == os.environ.get("SERVER_RIOT_KEY", "").strip():
    # same key as the Moosi app: Riot's rate limit is per region, the app's players are on EUW ->
    # leave euw1/europe completely to the app, otherwise everyone gets 429 "too many requests"
    regions = [r for r in regions if r not in ("euw1", "eun1", "tr1", "ru", "me1")] or ["na1"]
    print("Server-Key: crawle ohne Europa, um die App nicht auszubremsen:", regions)
threads = [threading.Thread(target=run, args=(r,)) for r in regions]
for t in threads:
    t.start()
for t in threads:
    t.join()

db = meta.connect(a.db)
meta.prune_balanced(db, max_matches=MAX_MATCHES, enough_current=MAX_MATCHES * 6)
print("Games je Elo:", meta.bucket_counts(db))
db.execute("DELETE FROM seen WHERE match_id NOT IN (SELECT match_id FROM games)")
db.commit()
db.execute("VACUUM")
db.execute("PRAGMA journal_mode=DELETE")  # single self-contained file for the app download
after = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
db.close()
with open(a.db, "rb") as src, gzip.open(a.db + ".gz", "wb", compresslevel=9) as dst:
    shutil.copyfileobj(src, dst)
print(f"Datensätze: {before} -> {after}")

# delta = only the games of this run: the app downloads these small files every hour instead of the whole db
import sqlite3  # noqa: E402
if os.path.exists("delta.sqlite"):
    os.remove("delta.sqlite")
src = sqlite3.connect(a.db)
schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='games'").fetchone()[0]
src.close()
dl = sqlite3.connect("delta.sqlite")
dl.execute(schema)
dl.execute("ATTACH DATABASE ? AS big", (a.db,))
dl.execute("INSERT INTO games SELECT * FROM big.games WHERE rowid > ?", (start_rowid,))
dl.execute("CREATE TABLE seen(match_id TEXT PRIMARY KEY)")
dl.execute("INSERT OR IGNORE INTO seen SELECT DISTINCT match_id FROM games")
dl.commit()
n_new = dl.execute("SELECT COUNT(*) FROM games").fetchone()[0]
dl.execute("DETACH DATABASE big")
dl.close()
stamp = int(time.time())
name = f"delta-{stamp}.sqlite.gz"
with open("delta.sqlite", "rb") as f_in, gzip.open(name, "wb", compresslevel=9) as f_out:
    shutil.copyfileobj(f_in, f_out)
with open("delta_name.txt", "w") as f:
    f.write(name if n_new else "")
print(f"Delta: {n_new} neue Zeilen -> {name}")
