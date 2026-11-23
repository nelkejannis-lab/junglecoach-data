# Jungle Coach – Meta-Datenbank

Sammelt automatisch Ranked-Games aller Elos (EUW, NA, KR, OCE) mit Gwen und Graves (alle Rollen) über die offizielle Riot API
und veröffentlicht sie alle 6 Stunden als `meta.sqlite.gz` im Release „meta-latest“.
Die Jungle-Coach-App lädt die Datei automatisch.

Benötigt das Repo-Secret `RIOT_API_KEY` (Settings → Secrets and variables → Actions).
Ein Dev-Key läuft nach 24 h ab – für Dauerbetrieb einen Personal API Key verwenden.

## Website-Meta

Workflow `website-meta.yml` ruft alle sechs Stunden (00:43, 06:43, 12:43, 18:43 UTC; GitHub kann verzögern) die Jungle-Seiten für Gwen und Graves von Mobalytics, OP.GG und LoLalytics ab. Veröffentlichung: Release `website-latest`, Datei `website-meta.json`. Kein Riot-Key erforderlich.

Jeder Lauf verwendet neue Browser-Kontexte ohne HTTP-Cache. Daten enthalten Quelle, Abrufzeit, Patch, extrahierte Abschnitte und Fehlerstatus. Die Website-Daten sind Patch-Aggregate, keine separat verifizierten Tages-Matchdaten. Fehlgeschlagene Quellen werden nicht durch alte Snapshots ersetzt. Die App filtert auf aktuellen Patch und maximal 24 Stunden Abrufalter. CAPTCHA/Sperren werden nicht umgangen.

Die bisherigen Riot- und Spieldaten-Workflows bleiben unabhängig. Champion-Liste im Aufruf unter „Collect fresh pages“ ändern. Bei Strukturänderungen der Anbieter muss die Bereichsextraktion angepasst werden.
