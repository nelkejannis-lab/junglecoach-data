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
# champions to collect (all roles); empty = all junglers
FOCUS = [c.strip() for c in os.environ.get("FOCUS", "Gwen,Graves").split(",") if c.strip()]
deadline = time.time() + a.minutes * 60
db = meta.connect(a.db)
before = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
db.close()


def run(region):
    while time.time() < deadline - 60:  # new player sample per round
        cfg = {"riot_api_key": key, "platform": region, "meta_max_matches": 10 ** 9,
               "meta_players_per_tier": 60, "meta_matches_per_player": 10, "meta_focus": FOCUS}
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
after = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
db.close()
with open(a.db, "rb") as src, gzip.open(a.db + ".gz", "wb", compresslevel=9) as dst:
    shutil.copyfileobj(src, dst)
print(f"Datensätze: {before} -> {after}")
