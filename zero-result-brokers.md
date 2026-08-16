# Zero-result broker audit

## Current direct-network status (2026-08-03)

The original direct-network inventory contained 23 zero-result brokers. Since then:

- `jalea`: repaired; live fetch returns 5 listings.
- `engel`: repaired; the registered embedded retry returns 12 listings.
- `teambim`: repaired; the current `/immobilien` page exposes stable object slugs and the source-specific retry returns 6 listings.
- `egger`: repaired; the fallback detail crawl returns 1 listing when the legacy `oid` card pattern is absent.
- `lebenstraum`: repaired; the retry path returns 12 listings.
- `joseffrei`: repaired; the detail-page fallback returns 12 listings.
- `schloss`: parser repaired for nested price markup and alternate card segmentation.
- `rogers`: parser repaired with raw-HTML numeric fallbacks for price and area.
- `bartsch`: parser repaired with alternate area and location field patterns.
- `schneider`: parser repaired for variable whitespace in price, area, and location fields.
- `ritter`: direct Mac-network inspection returned 12 complete listings; no parser change required.
- `hallinger`: source-specific parser and fixture are implemented, but the live domain currently has a TLS certificate failure, so live extraction is not confirmed.
- `mar`, `dahler`, `krimbacher`, `neuesnest`, `dalexis`, `ausdemhaeuschen`, `feuerlein`, `reischl`, and `gattinger`: retry paths exist, but no additional parser change is justified without a stable live payload or representative fixture.
- The final terminal verification run was blocked by the local HTTPS proxy (`403 Tunnel connection failed`); this is an environment limitation, not a parser result.
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
- `lebenstraum`: embedded/detail retry with price, area, and location extraction
- `joseffrei`: detail-page fallback for house and apartment offer pages
- `schloss`: nested price markup and fallback card segmentation
- `rogers`: raw-HTML price and area fallback patterns
- `bartsch`: alternate area and location labels
- `schneider`: tolerant field spacing and fallback labels

## Confirmed unresolved or blocked

The 2026-08-16 discovery pass did not add the following sources because their public response was not a stable, verifiable static listing feed: `brunner` (Cloudflare/Captcha), `century21-celeste` (portal/JS surface), `christian-loesch` and `cvb` (ImmoScout24 profiles), `connect-select` (Cloudflare), `cosmos` (iframe/JS surface), `fbc` (access/interstitial response), `falc` (portal-style page needs a source fixture), `finest-realestate` (maintenance response), `fmc` (access/interstitial response), `immobilien-ried` (404), `immobilien-t-raum` (unclear offer boundary), `immobilienprofis.bayern` (access/interstitial response), `immoferreira` (no verified listing record), `immo-space` (Cloudflare), `jhomes` (404), `kskmse` (404/portal), `kreipl` (access response), `lb-bayern` (no verified listing record), `layer` (access/interstitial response), `mia-casa` (Captcha/interstitial response), `remax-living` (portal/JS surface), `kve` (access response), `winterhalter` (no verified listing record), `diamant` (no verified listing record), `myimmoplus24` (no verified listing record), `muenchner-ig` (no verified listing record), `situli` (no verified listing record), `jp-hansen` (404), `fm-immobilien` (no verified listing record), `neukirchinger` (no verified listing record), `planethome` (portal/JS surface), `raiffeisen-immo` (portal/JS surface), `regina-hoepper` (no verified listing record), `rosenberger` (no verified listing record), `rpp` (no verified listing record), `schlagenhauf` (no verified listing record), `sontona` (unclear offer boundary), `steinhardt-am-see` (no verified listing record), `garant` (iframe-only listing surface), `immovision` (access/interstitial response), `immocom` (no verified listing record), `rudek` (Captcha/interstitial response), `hatton` (HTTP source URL is not accepted by the HTTPS-only fetch policy), and `szikora` (no verified listing record). `flemmer`, `kolb`, `mnord`, `lanzl`, `raum`, `seffen`, and `siebenlist` were implemented through the explicitly whitelisted static-card family; `clavis` was likewise added from its static listing page.

- `deutsche-bank-immobilien`: skipped; the inspected search page did not confirm a stable static/API listing feed.
- `homeday`: skipped; the inspected search page did not confirm a stable static/API listing feed.

These brokers remain unresolved after repeated parser attempts, with the stated reason:

- `bunzco` (Bunz & Co Immobilien): access blocked with HTTP 403 on the root and `/immobilien/`; no safe public feed identified.
- `neuesnest` (Neues Nest): own pages provide weak static listing signals; no stable public listing feed identified.
- `cki` (CKI Immobilien): current response/parser path does not expose a stable listing payload; requires a fresh source inspection.

## Not classifiable from this machine

The following six sources returned proxy tunnel 403 errors during the audit and must not be classified as parser failures:

- `schloss` (Schloss)
- `rogers` (Rogers)
- `bartsch` (Bartsch)
- `schneider` (Schneider)
- `ritter` (Ritter Bautraeger Immobilien)

The remaining configured brokers also returned zero normalized rows in the same run, but their fetch-stage status was swallowed by the current generic error handling. They require an accessible network run or saved HTML fixtures before a parser change is justified. Do not use the 137/137 result as a claim that every broker has no listings.

## Next audit requirement

Run the broker inventory from an environment without the local HTTPS tunnel, or collect HTML fixtures. The audit should preserve fetch-stage categories (`HTTP 403`, timeout, empty HTML, parser empty, normalized rows) so blocked sources are separated from genuine parser defects.
