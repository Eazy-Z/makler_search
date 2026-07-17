# Makler Search Automation

This repository uses two custom agents:

- Test Agent: validates the implementation and reports judge-style feedback
- Developer Agent: implements new broker sources and fixes issues from user requests

Workflow:
1. Developer Agent implements the requested source or fix in the scraper and UI.
2. Test Agent validates the result and provides pass/fail feedback.
