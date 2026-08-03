# Zero-result broker audit

## Current direct-network status (2026-08-03)

The original direct-network inventory contained 23 zero-result brokers. Since then:

- `jalea`: repaired; live fetch returns 5 listings.
- `engel`: repaired; the registered embedded retry returns 12 listings.
- `teambim`: repaired; the current `/immobilien` page exposes stable object slugs and the source-specific retry returns 6 listings.
- `hallinger`: source-specific parser and fixture are implemented, but the live domain currently has a TLS certificate failure, so live extraction is not confirmed.
- `mar`, `dahler`, `krimbacher`, `egger`, `neuesnest`, `dalexis`, `ausdemhaeuschen`, `feuerlein`, `lebenstraum`, `joseffrei`, `reischl`, and `gattinger`: retry paths exist, but no additional parser change is justified without a stable live payload or fixture.
- `bunzco`, `cki`, `windisch`, `harinali`, and `muenchnerimmobilien`: blocked or unreachable during the direct audit; treat as access problems, not parser failures.
- `weber`, `andreasschmid`, and `wandl`: reachable but no stable listing payload was identified; keep unresolved until a public feed or representative HTML fixture is available.

Audit status: local Python fetches are not a reliable source of zero-result evidence in this environment. The configured HTTPS proxy returned `403 Tunnel connection failed` for some domains, while the existing fetchers convert most other fetch failures into an empty list. The run therefore reported 137/137 zero-result brokers, but that is not a valid production inventory.

## Verified parser repairs

These sources expose static listing data and now have source-specific retry handling in `app.py`:

- `egger`: `/immobilien/objekt/?oid=...` cards with price and area
- `parkavenue`: `/apartment/.../` cards with price and area
- `elvira`: `/immobilienangebote/...` cards with price and area
- `sozius`: `/detailseite/...` cards with price and area
- `hoser`: static price blocks on the offers page
- `ritter`: source-specific property-block/detail parsing (already repaired)
- `vorstadtmakler`: embedded listing data with strict property-link filtering (already repaired)
- `wurmseder`: strict listing-slug and detail-page parsing (already repaired)
- `teambim`: current `/immobilien/<slug>` links with detail-page price, area, and location parsing

## Confirmed unresolved or blocked

These brokers remain unresolved after repeated parser attempts, with the stated reason:

- `bunzco` (Bunz & Co Immobilien): access blocked with HTTP 403 on the root and `/immobilien/`; no safe public feed identified.
- `neuesnest` (Neues Nest): own pages provide weak static listing signals; no stable public listing feed identified.
- `cki` (CKI Immobilien): current response/parser path does not expose a stable listing payload; requires a fresh source inspection.

## Not classifiable from this machine

The following six sources returned proxy tunnel 403 errors during the audit and must not be classified as parser failures:

- `schloss` (Schloss)
- `rogers` (Rogers)
- `firstplace` (First Place)
- `bartsch` (Bartsch)
- `schneider` (Schneider)
- `ritter` (Ritter Bautraeger Immobilien)

The remaining configured brokers also returned zero normalized rows in the same run, but their fetch-stage status was swallowed by the current generic error handling. They require an accessible network run or saved HTML fixtures before a parser change is justified. Do not use the 137/137 result as a claim that every broker has no listings.

## Next audit requirement

Run the broker inventory from an environment without the local HTTPS tunnel, or collect HTML fixtures. The audit should preserve fetch-stage categories (`HTTP 403`, timeout, empty HTML, parser empty, normalized rows) so blocked sources are separated from genuine parser defects.
