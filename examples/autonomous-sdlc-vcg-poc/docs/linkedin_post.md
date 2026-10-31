# LinkedIn post — ready to publish

**Autonomous SDLC: Fewer tokens. More verified work.**

What if the next shift in software engineering isn't a bigger team—or more coding agents—but an *adaptive army of agents* that knows when to delegate, retrieve, review, replan and stop?

I've been exploring a research PoC combining agentic patterns, adaptive RAG and human-supervised SDLC/STLC.

The objective: **maximize verified engineering outcomes per unit of compute**, not agent count or token savings in isolation.

A Linear issue, GitHub change or sanitized production signal starts the workflow. A budget-aware orchestrator decomposes work into a dependency graph, deduplicates investigations, and dispatches coding, test and independent review agents when parallelism makes sense. Definition-of-Done checks bind to an issue revision and commit; changes invalidate stale reviews.

The shared context layer is a **Versioned Context Graph (VCG)**: code symbols, dependencies, issues, commits, PRs, tests, incidents and verified decisions connected by versioned relationships. Agents receive focused, fresh evidence instead of repeatedly rereading the repository.

The same optimization applies to **RAG**. Exact identifiers suggest lexical search; ambiguous questions may need semantic search; cross-service incidents can require graph traversal. The goal is adaptive retrieval depth and context size, subject to recall, access control and freshness.

The vision is an **agentic delivery flywheel**: production telemetry becomes triage work; agents investigate, implement and test; independent reviewers and CI verify; a human dashboard exposes DoD progress, billable tokens, duplicate work, context staleness, retries, latency and cloud-worker memory.

One inspiration came from recent AI-assisted 3-SAT research: *sometimes changing the representation of a problem matters more than searching harder*. This is an analogy, not a mathematical guarantee for agents or RAG.

**What the early PoC demonstrates:** simulated workers, real fixture tests and synthetic costs; 14 tests passed. In a tiny 10-question retrieval fixture, adaptive BM25 + sparse TF-IDF + graph-linked search covered the same 13 labelled evidence references while selecting 508 rather than 897 context word-units (~43% less).

**These are not real LLM tokens, production savings, or measured answer-quality gains.**

Next: A/B benchmark real cloud agents on matched issues with independent reviews and quality gates. Measure cost, memory, latency, rework and escaped defects.

The paradigm I'm interested in isn't replacing engineering judgment. It's **smaller human-led teams supervising transparent systems that deliver more verified work with less wasted computation**.

PoC + architecture: https://github.com/gandhiashutosh14/governed-agent-orchestrator/tree/main/examples/autonomous-sdlc-vcg-poc
Research inspiration: https://github.com/anthropics/formal-math/tree/main/eth

#AgenticAI #SoftwareEngineering #RAG #AIAgents #SystemDesign
