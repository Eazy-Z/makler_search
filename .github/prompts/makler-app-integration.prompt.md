# Prompt: Neue Maklerquellen in die Makler Search App integrieren

Du arbeitest im Repository `/Users/maximilianzittel/Desktop/Makler_Search` und sollst neu hinzugefügte, noch nicht in der Makler Search App integrierte Maklerquellen in die laufende Python-Anwendung aufnehmen.

## Ziel

Ermittle die neuen Broker-URLs aus `broker-urls.txt`, die noch keine funktionierende Integration in `app.py` besitzen. Implementiere fuer jede geeignete Quelle die kleinste passende Integration, sodass ihre aktuellen Angebote beim normalen App-Lauf gefunden, normalisiert und ausgegeben werden.

## Kandidaten bestimmen

1. Lies zuerst:
   - `broker-urls.txt`
   - `app.py`
   - `tests/test_scrapers.py`
   - `.github/copilot-instructions.md`
   - `zero-result-brokers.md`
2. Ermittle bevorzugt die in der aktuellen Arbeitskopie neu hinzugefuegten URL-Zeilen mit:
   - `git diff -- broker-urls.txt`
   - `git diff --cached -- broker-urls.txt`
   Beruecksichtige dabei sowohl aktive als auch auskommentierte hinzugefuegte URL-Zeilen. Entferne das fuehrende `#` nur fuer die Analyse; veraendere den Status in `broker-urls.txt` nicht ungefragt.
3. Falls kein Diff vorhanden ist, verwende nur eindeutig neue URL-Eintraege am Ende der Datei oder eine vom Auftrag genannte Kandidatenliste. Behandle alle auskommentierten URLs weiterhin als bekannte Quellen, aber integriere sie nur, wenn sie im Auftrag als neue Integrationskandidaten gemeint sind.
4. Vergleiche normalisierte Hostnamen und Angebots-URLs gegen die vorhandene App-Integration. Pruefe insbesondere:
   - URL-Konstanten wie `*_URL`
   - `BROKER_LABELS`
   - Fetch-Funktionen wie `fetch_*_listings`
   - die zentrale Broker-/Fetcher-Registrierung
   - Sonderpfade, Retry-Funktionen und Ausschlussmengen
   Eine vorhandene URL in `app.py` zaehlt nur dann als integriert, wenn sie im normalen App-Lauf tatsaechlich registriert und abrufbar ist.

## Integrationsregeln

- Arbeite source-spezifisch und veraendere nur die benoetigten Dateien.
- Bevorzuge vorhandene Helfer wie `fetch_generic_broker_listings`, `fetch_source_specific_broker_listings`, `fetch_source_specific_with_embedded_retry` oder bestehende statische Familien-Adapter.
- Verwende keinen neuen Adapter, wenn ein bestehender generischer Pfad die Quelle korrekt und stabil abdeckt.
- Fuege bei Bedarf eine kanonische URL-Konstante, ein Label, eine Fetch-Funktion und die Registrierung in der zentralen Fetcher-Struktur hinzu.
- Halte Broker-Schluessel stabil, eindeutig und konsistent mit `BROKER_LABELS`.
- Erlaube nur HTTPS-Quellen und halte den erlaubten Host der Detailseiten ein.
- Respektiere bestehende Filter fuer Navigation, Assets, Referenzen, externe Portale und falsche Detailseiten.
- Nutze bei eingebetteten Feeds nur oeffentliche, ohne Login erreichbare Datenquellen. Umgehe keine CAPTCHA-, WAF-, 401-, 403- oder sonstigen Zugriffssperren.
- Wenn eine Quelle aktuell blockiert, leer, nur portalbasiert oder nicht stabil parsebar ist, integriere sie nicht. Dokumentiere den Grund stattdessen knapp in der Abschlussausgabe.
- Veraendere `broker-urls.txt` nicht, ausser der Auftrag verlangt ausdruecklich eine Aktivierung, Deaktivierung oder Bereinigung der URL.
- Fuege keine Zugangsdaten, Tokens, Cookies oder geheimen Testdaten hinzu.

## Parser-Anforderungen

Eine erfolgreiche Integration muss mindestens:

1. die Angebotsliste der Quelle abrufen;
2. konkrete Detail- oder Expose-Links derselben Quelle erkennen;
3. Titel, Preis, Flaeche, Ort und Link soweit vorhanden in das bestehende Listing-Format ueberfuehren;
4. Host- und Detail-Link-Regeln der App einhalten;
5. mit leerer, blockierter oder veraenderter Quelle robust umgehen, ohne den Gesamtlauf abzubrechen;
6. Duplikate und offensichtliche Navigations-/Asset-Treffer vermeiden.

Bevorzuge Daten aus initialem HTML, JSON-LD, eingebettetem JSON oder einem stabilen oeffentlichen JSON/XML-Feed. Handgeschriebene String-Sonderfaelle sind nur zulaessig, wenn kein vorhandener strukturierter Pfad passt.

## Tests und Validierung

- Fuege fuer jeden neuen Parser einen kleinen, deterministischen Test in der bestehenden Teststruktur hinzu oder erweitere einen passenden Test.
- Verwende keine Live-Netzwerkabhaengigkeit in Unit-Tests; mounte HTTP-Antworten oder verwende lokale HTML-Fixtures im vorhandenen Stil.
- Teste mindestens einen positiven Angebotsfall und einen Fall ohne verwertbare Angebote oder mit ungueltigem Detail-Link.
- Fuehre nach jeder Integrationsgruppe zuerst den engsten Test aus, danach mindestens:
  - `pytest -q tests/test_scrapers.py`
  - `python -m py_compile app.py`
  - `git diff --check -- app.py tests broker-urls.txt`
- Pruefe abschliessend, dass der neue Broker in der zentralen Registrierung vorkommt und nicht durch `IGNORED_BROKERS`, `BLOCKED_BROKER_REASONS` oder einen Sonderpfad versehentlich uebersprungen wird.
- Berichte fehlgeschlagene oder nicht verfuegbare Tests ehrlich; interpretiere einen HTTP-Fehler nicht automatisch als Parserfehler.

## Abschlussausgabe

Gib eine kurze Tabelle aus:

| Broker | URL | App-Status | Parserpfad | Tests | Ergebnis |
|---|---|---|---|---|---|

Danach drei kurze Abschnitte:

### Integriert

Nenne die geaenderten Dateien und den jeweiligen Broker-Schluessel.

### Nicht integriert

Nenne Kandidaten mit dem konkreten Grund: Duplikat, bereits integriert, blockiert, leer, nur Portal, fehlender Detail-Link oder unklarer Feed.

### Validierung

Nenne die ausgefuehrten Befehle und deren Ergebnis.

Aendere keine unrelated Dateien, fuehre keinen Commit aus und fasse keine bestehenden Benutzer-Aenderungen zurueck.
