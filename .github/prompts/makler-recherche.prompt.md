# Recherche-Prompt: Neue Maklerquellen fuer Makler Search

Du recherchierst im Internet nach Immobilienmaklern, die Immobilien in Muenchen oder der naeheren Umgebung anbieten und noch nicht in der Maklersearch-App erfasst sind.

## Ziel

Finde neue, belastbare Quellen fuer den Scraper. Entscheidend ist nicht nur die Makler-Homepage, sondern die konkrete URL, auf der die aktuellen Angebote als Liste oder Suchergebnis erreichbar sind und von der sich einzelne Inserate direkt aufrufen lassen.

## Ausschlussmenge

Lies zuerst im Repository:

- `broker-urls.txt`: Alle URLs, auch auskommentierte URLs, gelten als bereits bekannt und duerfen nicht erneut vorgeschlagen werden.
- `app.py`: Beruecksichtige die dort registrierten Broker und Quellen in `BROKER_SOURCES`, `STATIC_FAMILY_SOURCES` sowie vergleichbaren Quellensammlungen.
- `.github/copilot-instructions.md` und `zero-result-brokers.md`: Beachte bekannte Sperren, 0-Ergebnis-Quellen und Sonderfaelle.

Ein Treffer ist nur neu, wenn weder der Makler noch dieselbe Angebotsquelle bereits erfasst ist. Varianten derselben Domain, URL mit anderem Query-String, Spiegel- oder Detailseiten desselben Angebots zaehlen nicht als neue Quelle. Pruefe ausserdem, ob mehrere Domains zum selben Unternehmen gehoeren.

## Suchgebiet

Beruecksichtige Muenchen und das Umland, insbesondere die Landkreise Muenchen, Starnberg, Ebersberg, Erding, Freising, Dachau, Fuerstenfeldbruck, Landsberg am Lech, Bad Toelz-Wolfratshausen und Rosenheim. Nimm einen Makler nur auf, wenn auf der Angebotsseite selbst ein Objekt in diesem Gebiet angeboten wird oder der regionale Schwerpunkt eindeutig belegt ist.

## Recherche und Validierung

1. Suche mit verschiedenen Kombinationen aus Makler, Immobilien, kaufen, verkaufen, Angebote, Expose, Muenchen und den genannten Umlandgemeinden. Nutze auch regionale Verzeichnisse, Suchmaschinen, Immobilienportale und öffentlich sichtbare Anbieterprofile als Entdeckungsquellen.
2. Durchsuche als eigene Discovery-Runde die offizielle IVD-Seite `https://ivd.net/experten-finden/`. Falls sie weiterleitet oder die Expertensuche auslagert, folge nur der offiziellen Weiterleitung, insbesondere zur IVD-Expertensuche bei `immobilie1.de`. Nutze dort die Suchfunktion beziehungsweise Ortsfilter fuer Muenchen und die genannten Umlandgemeinden sowie die Landkreise Muenchen, Starnberg, Ebersberg, Erding, Freising, Dachau und Fuerstenfeldbruck.
3. Erfasse aus der IVD-Expertensuche fuer jeden regional passenden Eintrag mindestens den Firmennamen, den Ort, die IVD-/Anbieterprofil-URL, die verlinkte Angebotsseite und die offizielle Maklerdomain, sofern angegeben. Pruefe jeden Namen und jede Domain gegen die Ausschlussmenge, einschliesslich Schreibvarianten, Konzern- und Partnerdomains.
4. Ein IVD-Eintrag ist nur eine Discovery-Quelle und noch kein verifizierter Treffer. Oeffne fuer jeden neuen Kandidaten die offizielle Maklerdomain und ermittle die spezifischste Angebots- oder Suchergebnis-URL. Bevorzuge eine URL wie `/immobilien/`, `/angebote/`, `/kaufen/` oder eine stabile Suchergebnis-URL gegenueber der Startseite. Ist nur ein IVD-/Portalprofil vorhanden, fuehre den Kandidaten nicht als verifizierte eigene Quelle.
5. Pruefe, ob die Angebotsseite mindestens ein echtes Immobilienangebot enthaelt. Ein echtes Angebot hat mindestens einen plausiblen Titel oder eine Objektart, einen Ort beziehungsweise eine Lage und einen Link zu einer Detailseite oder einem Expose.
6. Oeffne mindestens einen Detail-Link und pruefe, ob er ohne Login erreichbar ist und auf ein konkretes Inserat desselben Maklers fuehrt.
7. Beurteile die technische Scrapebarkeit: Die Angebotskarten oder relevanten Daten muessen im initialen HTML, in eingebettetem JSON/JSON-LD oder ueber einen ohne Browser-Login nachvollziehbaren oeffentlichen JSON/XML-Endpunkt vorhanden sein. Seiten, die erst nach Benutzeraktion, CAPTCHA, Login oder ausschliesslich durch nicht ermittelbare Browser-JavaScript-Requests Angebote anzeigen, sind keine direkt scrape-baren Quellen.
8. Pruefe den HTTP-Zugriff mit einem normalen GET. Dokumentiere 401/403, CAPTCHA, robots-/WAF-Sperren, leere HTML-Antworten und andere Einschraenkungen. Versuche keine Umgehung von Zugriffsschutz.
9. Bevorzuge Quellen mit mehreren aktuellen Angeboten und stabilen, kanonischen URLs. Nimm keine reine IVD-/Portalprofilseite, Referenz-, Blog-, Bautraeger- oder Projektseite ohne konkrete aktuelle Inserate auf.

## Abbruchbedingungen

Beende die Recherche, sobald mindestens 10 neue, verifizierte und scrape-bare Quellen gefunden wurden. Beende sie ausserdem, wenn drei aufeinanderfolgende Suchrunden keine neuen geeigneten Makler ergeben oder wenn insgesamt 50 Kandidaten geprueft wurden. Gib auch bei einem vorzeitigen Ende alle bis dahin verifizierten Quellen, Ausschlussgruende und offenen Kandidaten aus.

## Ergebnisformat

Gib nur neue und verifizierte Quellen als Haupttabelle aus. Sortiere sie nach Scrape-Eignung und regionaler Relevanz. Verwende genau diese Spalten:

| Makler / Unternehmen | Angebots-URL | Beispiel-Inserat-URL | Region / Nachweis | Angebotsanzahl oder Signal | Technischer Zugriffsweg | Scrape-Eignung | Bemerkung |
|---|---|---|---|---:|---|---|---|

Regeln fuer die Tabelle:

- Verwende vollstaendige, kanonische URLs ohne Tracking-Parameter, sofern diese funktionieren.
- Die Spalte `Angebots-URL` muss die URL enthalten, die ein Scraper zuerst abrufen sollte.
- `Beispiel-Inserat-URL` muss auf ein konkretes aktuelles Objekt zeigen, nicht auf die Startseite.
- Beschreibe bei `Technischer Zugriffsweg`, ob die Daten in HTML, JSON-LD, eingebettetem JSON, XML/Feed oder einem oeffentlichen API-Endpunkt gefunden wurden.
- Setze `Scrape-Eignung` nur auf `hoch`, wenn die Angebotsliste und mindestens ein Detail-Link direkt oeffentlich abrufbar sind. Sonst `mittel` oder `ungeeignet`.
- Fuege keine URL ein, die du nicht geoeffnet und inhaltlich geprueft hast.
- Gib keine erfundenen Angebotszahlen, Objektdaten oder technischen Details an. Verwende `nicht ermittelt`, wenn eine Angabe nicht verifiziert werden konnte.

Fuehre danach zwei kurze Abschnitte aus:

### Ausgeschlossen

Nenne wichtige gefundene Kandidaten, die wegen Duplikat, fehlendem Muenchen-Bezug, 403/401, CAPTCHA, Login, leerer Angebotsseite oder fehlender direkter Inserat-URL ausgeschlossen wurden. Je Kandidat ein kurzer Grund und die gepruefte URL.

### Nachrecherche

Nenne hoechstens fuenf interessante Kandidaten, die regional plausibel wirken, aber noch nicht ausreichend verifiziert werden konnten. Gib die Domain und den konkreten offenen Pruefpunkt an. Diese Kandidaten duerfen nicht in der Haupttabelle als verifizierte Quelle erscheinen.

Arbeite faktenbasiert und mit dem Recherchezeitpunkt. Verlinke, wo moeglich, die Belege direkt in der Ausgabe. Die Aufgabe ist die Quellensuche; implementiere keinen Scraper und veraendere keine Repository-Dateien.