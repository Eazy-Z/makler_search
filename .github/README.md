# Makler Search Automation

This repository uses three custom agents:

- Crawler Agent: discovers new Munich broker websites and identifies their sales listings pages
- Test Agent: validates the implementation and reports judge-style feedback
- Developer Agent: implements new broker sources and fixes issues from crawler or user requests

Workflow:
1. Crawler Agent finds a new broker and hands the source to the Developer Agent.
2. Developer Agent implements the source in the scraper and UI.
3. Test Agent validates the result and provides pass/fail feedback.
