# Jungle Coach – Meta-Datenbank

Sammelt automatisch Ranked-Games aller Elos (EUW, NA, KR) mit Gwen und Graves (alle Rollen) über die offizielle Riot API
und veröffentlicht sie alle 6 Stunden als `meta.sqlite.gz` im Release „meta-latest“.
Die Jungle-Coach-App lädt die Datei automatisch.

Benötigt das Repo-Secret `RIOT_API_KEY` (Settings → Secrets and variables → Actions).
Ein Dev-Key läuft nach 24 h ab – für Dauerbetrieb einen Personal API Key verwenden.
