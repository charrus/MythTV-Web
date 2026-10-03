# MythTV Web Server Agent Guide

## Project Description

This project is a Python web server that will provide a browser-based interface to the MythTV services available locally on the server. The application is built with Flask and is intended to sit behind Apache, which provides external access and authentication.

## Requirements

1. Use Python and Flask for the web application, following the existing app-factory structure in `mythtv_web/`.
2. Keep the project environment local in `.venv/`; declare application dependencies in `requirements.txt`.
3. Bind the application server to `0.0.0.0` on port `5000` so Apache can proxy to it.
4. Use Apache as the public-facing reverse proxy and authentication boundary. Do not assume requests reaching Flask are authenticated unless a feature explicitly verifies that trust boundary.
5. Do not expose port `5000` to untrusted networks. Document or preserve firewall/network restrictions so only Apache and trusted administration hosts can reach the backend directly.
6. Keep MythTV integration local to this server. Add specific MythTV features incrementally as requirements are agreed and documented.
7. Preserve the existing `/` landing route and `/healthz` health endpoint unless a change is intentionally required and documented.
8. Keep this file updated as project requirements, architecture, and operational assumptions evolve.

## Development Guidance

- Follow the current Flask and Python patterns in the repository; keep changes focused on the requested feature.
- Do not commit credentials, tokens, or other secrets. Keep local environment files and `.venv/` out of Git.
- Add or update focused tests for behavior changes. Before finishing a feature, run the relevant tests or checks and review the complete diff.
- Update `README.md` when setup, run, deployment, or operational instructions change.

## Git Workflow

1. Complete one coherent feature or fix at a time.
2. Run the relevant validation and review the staged diff before committing.
3. Commit each completed feature or fix to Git as its own focused commit, using a message that describes the change.
4. Do not include unrelated user changes in a feature commit.
