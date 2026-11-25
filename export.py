"""Turns the big crawler database into what the apps actually download:

* agg.sqlite.gz – small pre-computed statistics (win rates per champion/role/elo, lane matchups, the champion
  vs. every enemy, role benchmarks, death causes of better players, main roles). Used for champ select ratings,
  counters, tier ranks and coaching comparisons. A few MB instead of the whole database.
* shard-<Champion>.sqlite.gz – all rows of one champion (runes, items, build paths, matchups). The app only
  downloads the champions you actually play / pick.
"""
import gzip
import json
import os
import shutil
import sqlite3
import sys

from jc import meta

BR_OF = {t: b for b, ts in meta.BRACKETS.items() for t in ts}
METRICS = None   # every numeric stat found in the rows


def gz(src, dst):
    with open(src, "rb") as a, gzip.open(dst, "wb", compresslevel=9) as b:
        shutil.copyfileobj(a, b)


def export(db_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    src = sqlite3.connect(db_path)
    cur_patch = (src.execute("SELECT patch FROM games ORDER BY created DESC LIMIT 1").fetchone() or [None])[0]
    agg_path = os.path.join(out_dir, "agg.sqlite")
    if os.path.exists(agg_path):
        os.remove(agg_path)
    a = sqlite3.connect(agg_path)
    a.executescript("""
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE cr(champ TEXT, role TEXT, br TEXT, scope TEXT, w INT, n INT);
      CREATE TABLE lane(champ TEXT, role TEXT, enemy TEXT, br TEXT, scope TEXT, w INT, n INT);
      CREATE TABLE vs(champ TEXT, role TEXT, enemy TEXT, br TEXT, scope TEXT, w INT, n INT);
      CREATE TABLE bench(role TEXT, br TEXT, metric TEXT, n INT, q TEXT);
      CREATE TABLE death(role TEXT, br TEXT, games INT, per_game TEXT);
      CREATE TABLE mainrole(champ TEXT PRIMARY KEY, role TEXT);
    """)
    a.execute("INSERT INTO meta VALUES ('patch', ?)", (cur_patch,))
    a.execute("INSERT INTO meta VALUES ('rows', ?)", (src.execute("SELECT COUNT(*) FROM games").fetchone()[0],))
    a.execute("INSERT INTO meta VALUES ('matches', ?)", (src.execute("SELECT COUNT(DISTINCT match_id) FROM games").fetchone()[0],))

    def rollup(rows, keylen):
        """rows: (*key, tier, patch, w, n) -> {(*key, br, scope): [w, n]} for br in bracket/all, scope cur/all."""
        acc = {}
        for r in rows:
            key, tier, patch, w, n = r[:keylen], r[keylen], r[keylen + 1], r[keylen + 2], r[keylen + 3]
            for br in (BR_OF.get(tier), "all"):
                if not br:
                    continue
                for scope in (("cur", "all") if patch == cur_patch else ("all",)):
                    v = acc.setdefault((*key, br, scope), [0, 0])
                    v[0] += w
                    v[1] += n
        return acc

    rows = src.execute("SELECT champ, role, tier, patch, SUM(win), COUNT(*) FROM games WHERE role != '' "
                       "GROUP BY champ, role, tier, patch").fetchall()
    a.executemany("INSERT INTO cr VALUES (?,?,?,?,?,?)", [(*k, w, n) for k, (w, n) in rollup(rows, 2).items()])
    rows = src.execute("SELECT champ, role, enemy, tier, patch, SUM(win), COUNT(*) FROM games WHERE role != '' AND enemy IS NOT NULL "
                       "GROUP BY champ, role, enemy, tier, patch").fetchall()
    a.executemany("INSERT INTO lane VALUES (?,?,?,?,?,?,?)", [(*k, w, n) for k, (w, n) in rollup(rows, 3).items() if n >= 3])
    rows = src.execute("SELECT g.champ, g.role, j.value, g.tier, g.patch, SUM(g.win), COUNT(*) FROM games g, json_each(g.enemies) j "
                       "WHERE g.role != '' AND g.enemies IS NOT NULL GROUP BY g.champ, g.role, j.value, g.tier, g.patch").fetchall()
    a.executemany("INSERT INTO vs VALUES (?,?,?,?,?,?,?)", [(*k, w, n) for k, (w, n) in rollup(rows, 3).items() if n >= 5])
    for champ, role in src.execute("SELECT champ, role FROM (SELECT champ, role, COUNT(*) c FROM games WHERE role != '' "
                                   "GROUP BY champ, role ORDER BY c ASC)").fetchall():
        a.execute("INSERT OR REPLACE INTO mainrole VALUES (?,?)", (champ, role))   # ascending -> the biggest wins

    # benchmarks per role (+ elo): 101 percentiles of every stat (enough to rebuild quantiles in the app)
    dist = {}
    cause = {}
    for role, tier, win, st in src.execute("SELECT role, tier, win, stats FROM games WHERE stats IS NOT NULL AND role != ''"):
        try:
            d = json.loads(st)
        except ValueError:
            continue
        for br in (BR_OF.get(tier), "all"):
            if not br:
                continue
            for k, v in d.items():
                if isinstance(v, (int, float)) and not k.startswith("dc_"):
                    dist.setdefault((role, br, k), []).append(v)
            if win and any(k.startswith("dc_") for k in d):
                c = cause.setdefault((role, br), [0, {}])
                c[0] += 1
                for k, v in d.items():
                    if k.startswith("dc_"):
                        c[1][k[3:]] = c[1].get(k[3:], 0) + v
    for (role, br, k), vals in dist.items():
        if len(vals) < 30:
            continue
        vals.sort()
        q = [vals[min(len(vals) - 1, int(i / 100 * len(vals)))] for i in range(101)]
        a.execute("INSERT INTO bench VALUES (?,?,?,?,?)", (role, br, k, len(vals), json.dumps(q)))
    for (role, br), (n, tot) in cause.items():
        a.execute("INSERT INTO death VALUES (?,?,?,?)", (role, br, n, json.dumps({k: round(v / n, 2) for k, v in tot.items()})))
    for t in ("cr", "lane", "vs"):
        a.execute(f"CREATE INDEX {t}_i ON {t}(champ, role, br, scope)")
    a.execute("CREATE INDEX lane_e ON lane(enemy, role, br, scope)")
    a.commit()
    a.execute("VACUUM")
    a.close()
    gz(agg_path, os.path.join(out_dir, "agg.sqlite.gz"))
    os.remove(agg_path)

    # one shard per champion (same schema as the big db, so the app's detailed queries work unchanged)
    schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='games'").fetchone()[0]
    champs = [r[0] for r in src.execute("SELECT DISTINCT champ FROM games")]
    src.close()
    for c in champs:
        p = os.path.join(out_dir, f"shard-{c}.sqlite")
        if os.path.exists(p):
            os.remove(p)
        s = sqlite3.connect(p)
        s.execute(schema)
        s.execute("CREATE TABLE seen(match_id TEXT PRIMARY KEY)")
        s.execute("ATTACH DATABASE ? AS big", (db_path,))
        s.execute("INSERT INTO games SELECT * FROM big.games WHERE champ=?", (c,))
        s.commit()
        s.execute("DETACH DATABASE big")
        s.close()
        gz(p, p + ".gz")
        os.remove(p)
    return len(champs)


if __name__ == "__main__":
    n = export(sys.argv[1] if len(sys.argv) > 1 else "meta.sqlite", sys.argv[2] if len(sys.argv) > 2 else "out")
    print(f"agg + {n} shards exportiert")
