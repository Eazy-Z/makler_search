# Makler Search

Local scraper and listing browser for multiple real-estate brokers.

## Features

- Scrapes several broker websites and groups results by broker
- Caches listing data for 5 minutes
- The Azure Timer Function refreshes listings asynchronously at the start of every hour from 06:00 through 19:00
- Keeps listing history with first-found date, age in days, and a `Gelöscht` note after two consecutive non-empty successful scrapes no longer publish an offer
- Allows sorting offers from newest to oldest first-found date
- Serves a simple browser UI with broker and listing filters

## Run

```bash
python3.14 app.py
```

Open the app at:

```text
http://127.0.0.1:8000/
```

On Azure App Service, open `https://maklerapp.azurewebsites.net/`.

## Azure Blob Storage

The app persists the current and historical listings in `latest.json` in the
`maklerapp` container of `maklerappstorageaccount`. The App Service must have a system-
assigned managed identity with the `Storage Blob Data Contributor` role on
that storage account. The app reads a fresh blob for up to three hours and
refreshes it from the broker sites when it expires.

The Azure Timer Function calls the protected `/internal/refresh` endpoint at
the start of every UTC hour. The Function only forwards the refresh during
06:00 through 19:00 in `Europe/Berlin`; automatic refreshes are paused from
20:00 through 05:00. Manual refreshes remain available.

The storage account must also allow the App Service to reach its public Blob
endpoint. In Storage account -> Networking, set `Public network access` to
`Enabled from all networks`, or add the App Service outbound IP addresses to
the allowed networks. A `403 (AuthorizationFailure)` without
`AuthorizationPermissionMismatch` usually means that the Storage firewall is
blocking the request rather than that the managed identity role is missing.

For local runs, set `AZURE_STORAGE_SAS_TOKEN` or provide an Azure managed
identity. The container URL can be overridden with
`LISTINGS_BLOB_CONTAINER_URL`.

## Notes

- The project uses only the Python standard library.
- Broker pages differ, so parsers are source-specific.

## Yahoo Mail-Auswertungen

Die Yahoo-Gegenstücke zu den Gmail-Skripten verwenden IMAP. Empfohlen ist ein
Yahoo-App-Passwort:

```bash
export YMAIL_ADDRESS='melinamarrek@ymail.com'
export YMAIL_APP_PASSWORD='DEIN_YAHOO_APP_PASSWORT'
```

Alternativ kann OAuth 2.0 mit `XOAUTH2` verwendet werden. Dafür vor dem ersten
Aufruf eine Yahoo-App registrieren und folgende Variablen setzen:

```bash
export YMAIL_ADDRESS='melinamarrek@ymail.com'
export YMAIL_CLIENT_ID='...'
export YMAIL_CLIENT_SECRET='...'
# Muss exakt als Redirect-URL in der Yahoo-App registriert sein.
export YMAIL_REDIRECT_URI='http://127.0.0.1:8765/'
```

Beim ersten Start öffnet sich der Yahoo-Login im Browser. Der Token wird
danach lokal in `ymail_token.json` gespeichert und bei Ablauf automatisch mit
dem Refresh Token erneuert. Die Datei darf nicht ins Repository gelangen.

Danach stehen `ymail_test.py`, `scripts/ymail_contacts.py`,
`scripts/ymail_immoscout_brokers.py`, `scripts/ymail_immowelt_brokers.py` und
`scripts/ymail_kleinanzeigen_inquiries.py` zur Verfügung. Die beiden
Broker-Skripte akzeptieren optional `--folder`; die Absender- bzw.
Betrefffilter können mit `--from-address` bzw. `--subject` überschrieben
werden. `ymail_contacts.py` durchsucht standardmäßig `INBOX` und `Sent`.