# Makler Search Instructions

- Work only in this repository.
- Keep scraper changes source-specific.
- Prefer small, testable edits.
- Prefer token-efficient runs: batch reads/searches, avoid repetitive checks, keep outputs concise.
- Agent outputs must be ultra-compact: max 6 lines, no logs, no long explanations.
- Use the Developer Agent to implement broker sources and fixes.
- Use the Test Agent to validate changes before completion.
- Avoid widening scope when a local fix is enough.
