# Makler Search

Local scraper and listing browser for multiple real-estate brokers.

## Features

- Scrapes several broker websites and groups results by broker
- Caches listing data for 5 minutes
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

The app persists the latest listings in `latest.json` in the `maklerapp`
container of `maklerappstorageaccount`. The App Service must have a system-
assigned managed identity with the `Storage Blob Data Contributor` role on
that storage account. The app reads a fresh blob for up to three hours and
refreshes it from the broker sites when it expires.

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