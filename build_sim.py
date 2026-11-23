"""Baut aus den Roh-Spieldaten die kompakte Simulationsdatei sim.json.gz für die App."""
import gzip
import json
import sys

from jc.sim.data import build

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    d = build(src)
    assert len(d["champions"]) > 150 and len(d["items"]) > 80, "unvollständige Spieldaten"
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    print("sim data", d["version"], len(d["champions"]), "champions", len(d["items"]), "items")
