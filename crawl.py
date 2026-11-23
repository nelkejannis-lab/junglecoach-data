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

key = os.environ["RIOT_API_KEY"].strip()
# fail fast with a clear message (GitHub then e-mails the repo owner) instead of crawling nothing
try:
    from jc.review import RiotAPI
    RiotAPI(key, "euw1").get_platform("/lol/status/v4/platform-data")
except Exception as e:  # noqa: BLE001
    raise SystemExit(f"RIOT_API_KEY abgelaufen oder ungültig ({e}). Neuen Key unter developer.riotgames.com "
                     "holen und im Daten-Repo unter Settings > Secrets > RIOT_API_KEY eintragen.")
# champions to collect (all roles); empty = all junglers
FOCUS = [c.strip() for c in os.environ.get("FOCUS", "Gwen,Graves").split(",") if c.strip()]
deadline = time.time() + a.minutes * 60
db = meta.connect(a.db)
before = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
db.close()


def run(region):
    while time.time() < deadline - 60:  # new player sample per round
        cfg = {"riot_api_key": key, "platform": region, "meta_max_matches": 10 ** 9,
               "meta_players_per_tier": 60, "meta_matches_per_player": 10, "meta_focus": FOCUS,
               "meta_focus_matches": 25, "meta_focus_players": 400,
               "meta_timelines": True}  # exact purchase order (1 extra request per match -> ~half the games)
        cr = meta.Crawler(cfg, db_path=a.db, deadline=deadline)
        cr._run()
        print(f"[{region}] {cr.state['message']}", flush=True)
        if "Fehler" in cr.state["message"]:
            if "API Key" in cr.state["message"]:
                return
            time.sleep(30)


threads = [threading.Thread(target=run, args=(r,)) for r in a.regions.split(",")]
for t in threads:
    t.start()
for t in threads:
    t.join()

db = meta.connect(a.db)
meta.prune(db, keep_patches=2, max_rows=400_000)
db.execute("DELETE FROM seen WHERE match_id NOT IN (SELECT match_id FROM games)")
db.commit()
db.execute("VACUUM")
db.execute("PRAGMA journal_mode=DELETE")  # single self-contained file for the app download
after = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
db.close()
with open(a.db, "rb") as src, gzip.open(a.db + ".gz", "wb", compresslevel=9) as dst:
    shutil.copyfileobj(src, dst)
print(f"Datensätze: {before} -> {after}")
