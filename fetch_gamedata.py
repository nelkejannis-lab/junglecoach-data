"""Lädt die Spieldaten des aktuellen Patches (Champion-Kits, Items, Jungle-Camps) und packt sie zu gamedata-raw.tar.gz.

Quellen: Data Dragon (Riot), CommunityDragon (Spieldateien), Meraki Analytics (strukturierte Skill-Formeln).
"""
import json, os, sys, tarfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "JungleCoach-Data/1.0 (+https://github.com/nelkejannis-lab/junglecoach-data)"}
OUT = "gamedata"
CD = "https://raw.communitydragon.org/latest"
MONSTERS = ["sru_gromp", "sru_blue", "sru_red", "sru_murkwolf", "sru_murkwolfmini", "sru_razorbeak",
            "sru_razorbeakmini", "sru_krug", "sru_krugmini", "sru_krugminimini", "sru_crab"]


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print("FAIL", url, e, file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def save(name, data):
    if data is None:
        return False
    path = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return True


def main():
    os.makedirs(OUT, exist_ok=True)
    ver = json.loads(get("https://ddragon.leagueoflegends.com/api/versions.json"))[0]
    jobs = {
        "ddragon/championFull.json": f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/championFull.json",
        "ddragon/item.json": f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/item.json",
        "ddragon/item_de.json": f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/de_DE/item.json",
        "ddragon/runesReforged.json": f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/runesReforged.json",
        "meraki/champions.json": "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions.json",
        "meraki/items.json": "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/items.json",
        "cdragon/champion-summary.json": f"{CD}/plugins/rcp-be-lol-game-data/global/default/v1/champion-summary.json",
        "cdragon/items.json": f"{CD}/plugins/rcp-be-lol-game-data/global/default/v1/items.json",
        "cdragon/items.cdtb.bin.json": f"{CD}/game/items.cdtb.bin.json",
    }
    for m in MONSTERS:
        jobs[f"cdragon/monsters/{m}.bin.json"] = f"{CD}/game/data/characters/{m}/{m}.bin.json"
    summary = json.loads(get(jobs["cdragon/champion-summary.json"]) or b"[]")
    for c in summary:
        a = (c.get("alias") or "").lower()
        if a and c.get("id", -1) > 0:
            jobs[f"cdragon/champions/{a}.bin.json"] = f"{CD}/game/data/characters/{a}/{a}.bin.json"
    with ThreadPoolExecutor(8) as ex:
        ok = list(ex.map(lambda kv: save(kv[0], get(kv[1])), jobs.items()))
    meta = {"ddragon_version": ver, "fetched": int(time.time()), "files": sum(ok), "failed": len(ok) - sum(ok)}
    save("meta.json", json.dumps(meta).encode())
    with tarfile.open("gamedata-raw.tar.gz", "w:gz") as t:
        t.add(OUT, arcname="gamedata")
    print(meta, os.path.getsize("gamedata-raw.tar.gz") // 1024, "KB")


if __name__ == "__main__":
    main()
