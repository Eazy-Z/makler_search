# Makler Search Instructions

- Work only in this repository.
- Keep scraper changes source-specific.
- Prefer small, testable edits.
- Prefer token-efficient runs: batch reads/searches, avoid repetitive checks, keep outputs concise.
- Agent outputs must be ultra-compact: max 6 lines, no logs, no long explanations.
- Use the Discovery Agent first for new brokers and for brokers with 0 results to choose the parser path token-efficiently.
- Use the Developer Agent to implement broker sources and fixes.
- Use the Test Agent to validate changes before completion.
- Avoid widening scope when a local fix is enough.
- For `wurmseder` zero-result recovery, prefer strict listing-slug filters and detail-page area signals to avoid nav/asset false positives.
- Treat `bunzco` as currently access-blocked (HTTP 403 on root and `/immobilien/` with default UA); avoid broad parser changes until an accessible public endpoint exists.
- Treat `neuesnest` as currently weak static signal on own pages; prioritize discovery of stable external/public listing feeds before adding broad retries.

- 0-broker triage order:
  1) Check hard blocking first (HTTP 403/401/anti-bot/captcha).
  2) If blocked or JS-heavy, search for stable external/public listing feeds (portal profile, embed endpoint, JSON feed).
  3) Only then add the smallest source-specific retry with strict filters and test validation.
