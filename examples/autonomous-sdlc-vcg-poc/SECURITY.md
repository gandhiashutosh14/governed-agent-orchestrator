# Safety and security

This is a research simulation, **not** a service for public deployment. `server.py` serves a local unauthenticated dashboard; only run it on the loopback interface. Never expose it to the internet, ingest production logs or customer data, or grant agents production secrets. Source content, issue descriptions, external retrieval data and log events must be treated as untrusted input.

No real LLM cloud agents, GitHub PR merges, Linear integration, vector database, or deployment operations are executed by this demo. Tokens are synthetic accounting units.
