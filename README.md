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

## Notes

- The project uses only the Python standard library.
- Broker pages differ, so parsers are source-specific.