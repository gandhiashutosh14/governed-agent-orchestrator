# Autonomous SDLC — Budget-Optimized Agent Army

Executable local **research proof of concept** of an optimized SDLC harness with a Versioned Context Graph, task-dependent agent spawning, work deduplication, independent-review gates, a human approval hold, sanitized observability intake, and a dashboard.

## Run

Python 3.11+; no third-party dependencies.

```bash
python server.py
# Visit http://127.0.0.1:8765
```

Tests and CLI:

```bash
python -m unittest discover -s tests -v
python harness.py
```

The demo uses *simulated* agent workers and *synthetic* token counters; these **are not real Claude usage/costs**. Code indexing and the fixture's Python `unittest` gate are genuinely executed. No Claude/LLM is called, no code is generated, no PR is created, and nothing is deployed. The localhost-only server is unauthenticated and must **never** be exposed publicly.

## Architecture

```text
Linear issues / GitHub PRs / OpenTelemetry / customer incidents
                             |
                 Input normalize and dedup
                             |
                   VERSIONED CONTEXT GRAPH
            Code files -- symbols -- imports -- issues
                   |               |           |
                tests            CI evidence   alerts
                             |
                 Budget-aware scheduler
             (DAG, file conflicts, task scope)
                             |
      Isolated worker jobs with narrow context manifests
           /              |                 \
    Planner/Builder      QA/tests      Reviewer A + B
           \              |                 /
       Commit/revision-bound verification receipts
                             |
              Approval + protected branch gate
                             |
          Runtime feedback loops into new issues
                             |
                   Human dashboard
```

### Core controls

- **Context fingerprint**: issue revision, related file hashes, acceptance checklist and dependencies. Change invalidation requires replan and rework, not just refreshed reviewer context.
- **Adaptive parallelism**: run disjoint file footprints together; serialize conflicting changes and respect dependency graph.
- **Dedup**: suppress duplicate *intents* (not every task touching the same file). Share already-built context packages.
- **Token budget preflight**: don't admit work that cannot complete the modeled minimum set of stages.
- **Verification**: two independent mock reviewer IDs, a *real* fixture unit-test run, a stale-context gate, a simulated high-risk human approval block.
- **Safety**: append only structured, sanitized observability aggregates; no raw logs or user interactions.
- **Observability**: show context hash, agent role, stage, revision, modeled tokens, modeled token cache hits, and synthetic PR/DoD outcomes.

### Explore the dashboard

1. Compare full-context fixed crew versus context-scoped adaptive mode.
2. Enable **Inject revised acceptance criteria midway** and rerun. The naive team reviews stale context and fails. Adaptive mode explicitly replans and models implementation rework before reviewing.
3. Reduce the token budget to make the scheduler reject work instead of burning unbounded tokens.
4. Inject a sanitized API error-rate signal, rerun, and see the resulting `OBS-...` triage issue linked to a source file.
5. Examine the graph, execution wave list, event feed, and receipts in `output/`.

### API examples

```bash
curl -X POST http://127.0.0.1:8765/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"service":"auth","signal_type":"failed_login_spike","count":42,"severity":"warning"}'

curl -X POST http://127.0.0.1:8765/api/run \
  -H 'Content-Type: application/json' \
  -d '{"budget":120000,"parallel":3,"inject_change":true}'
```

## Production architecture (NOT implemented here)

| Capability | Prototype | To make production grade |
|---|---|---|
| Knowledge graph | File hashes, Python AST symbols/imports, simple JS/TS/Go/Java heuristics | Tree-sitter or language-server, branch-aware incremental indexing and accurate cross-language dependencies |
| Tracking | In-memory example issues + local JSON event log | Linear GraphQL API, signed issue webhooks, durable event log |
| Executors | Deterministic mock agents | Real Claude Agent SDK / cloud agent jobs in isolated sandboxes |
| Concurrency | Disjoint file-path scheduling waves | Git worktrees/isolated cloud workspaces, conflict handling and durable leases |
| Reviews | Independent mock reviewer identities | Truly separate review contexts, PR diff-based review, GitHub branch protection |
| Tests | Actual local sample `unittest` execution | Sandboxed CI with tests tied to exact PR head SHA |
| Policy | Demo freshness, mocked review, human approval gates | SCM-enforced approvals, code owners, audited human decision and release policy |
| Monitoring | Local sanitized signal endpoint and triage issue | OTel/CI ingestion, signed webhooks, anomaly windows, PII scrubbing and prompt-injection defense |
| Memory | Context bytes + local Python RSS high-water | Actual cloud-worker container RSS/CPU/cgroups; model-serving GPU memory generally opaque |
| Cost | Synthetic tokens only | Provider-reported billable input/output/cache-read/cache-write tokens, USD, duration and agent retries |
| Durability | JSON snapshots | Transactional database and durable job queue; LangGraph checkpointers are optional |
| Dashboard | Local HTML/JS | Authentication, multi-tenant RBAC, streaming trace IDs, audit and retention |

## Correctness/safety invariants

Each production review/check receipt should bind to `issue_id`, issue specification revision, **commit SHA**, context manifest hash, actor identity, test/check identifier, timestamp and result. If code or DoD changes, verification expires; the agent replans before claiming completion. Independent reviewers cannot approve their own build changes. Tests must not be silently skipped to reduce token usage. External logs are untrusted, potentially sensitive input: aggregate and sanitize before indexing. No agent should have access to production secrets by default. High-risk merges require a human authorization enforced by GitHub, not the dashboard.

## Evaluation plan

Compare identical issues and base commits on two arms: (A) fixed specialized workers, full-context prompts, static scheduling; (B) task-aware workers, bounded graph-based retrieval, shared context, duplicate-work suppression and revision invalidation. Keep models, runtime resources, tests, acceptance criteria and risk gates identical. Collect actual provider billing, container RSS and runtime, PR review minutes, CI/E2E test success, escaped defects and rework. Stratify by task complexity; measure confidence intervals. Require quality **non-inferiority** before claiming efficiency gains. Token minimization that damages DoD is not optimization.

The often-discussed 3-SAT paper is an inspiration about **reformulating a search problem**, not a directly portable subexponential algorithm. Nothing in this PoC constitutes an adaptation or proof of that algorithm.

### Research references

- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Google Research: Scaling agent systems, January 2026](https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/)
- [Stanford: Token consumption in coding agents, April 2026](https://digitaleconomy.stanford.edu/publication/how-do-ai-agents-spend-your-money-analyzing-and-predicting-token-consumption-in-agentic-coding-tasks/)
- [Linear: API and webhooks](https://linear.app/docs/api-and-webhooks)
- [OpenTelemetry: GenAI token accounting conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-token-metrics.md)
- [GitHub: Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [Anthropic: 3-SAT formal proof and paper](https://github.com/anthropics/formal-math/tree/main/eth)

## V2: Versioned Context Graph + budget-adaptive retrieval

**Naming:** This publicly shareable prototype uses **Versioned Context Graph (VCG)** throughout. It does not depend on proprietary terminology, schemas, or customer data.

The VCG is a revision-aware evidence graph connecting files, imported modules, code symbols, issues, and operational signals. The new RAG demonstration uses the **same context-routing ideas** for both software-engineering evidence and a separate, synthetic support-document corpus.

```text
             Versioned Context Graph (VCG)
             file <-> symbol <-> issue <-> signal
                          |
Query + tenant -> Route -> BM25 / sparse TF-IDF cosine
                          |             |
                          +-- Fuse ----+
                               |
                      Optional 1-hop graph links
                               |
                    Relevance / evidence-type priority
                               |
                     Freshness & tenant checks
                               |
                   Bounded evidence context pack
                               |
                     Human/agent consumes evidence
```

This is a **functional retrieval PoC, not a production RAG stack**. It includes genuine lexical indexing and scoring (BM25), TF-IDF cosine similarity over sparse vectors, reciprocal-rank fusion, type-sensitive routing, one-hop graph traversal, per-tenant source filtering, latest-revision selection, and context-size budgeting. It does **not** use model embeddings, approximate nearest-neighbor search, neural reranking, model generation, a persistent vector database, or real hosted-provider token counting.

### Try the RAG experiment

```bash
python retrieval.py > output/retrieval.json
python -m unittest discover -s tests -v
python server.py
# Open http://127.0.0.1:8765 and try the Budget-Adaptive RAG panel
```

Read-only query endpoint, limited to bundled fabricated corpora:

```bash
curl -X POST http://127.0.0.1:8765/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"What issue is linked to the observed 401 failure and which component is affected?", "tenant":"demo", "budget_words":130}'
```

**Reproducible small-corpus observation:** Across 10 hand-authored fixture questions with 13 gold document references, fixed TF-IDF top-K retrieved all 13 with 897 whitespace-separated context units; adaptive retrieval retrieved all 13 with 508 units (about **43.4% fewer**). These figures are *not model tokens* and say nothing conclusive about real-world retrieval performance. The tiny evaluation set is close to the data and routing heuristics, is too small for generalization, and does not assess generated answer faithfulness.

The first, naive optimization settings retrieved only 10/13 gold references while saving context. This failure is why the final example includes type-sensitive evidence routing and graph relationships and why production gates must protect recall and source freshness before reducing context.

### Production-grade retrieval extensions (not yet built)

1. Use actual model embeddings and HNSW/ScaNN/DiskANN indexing; tune `hnsw_ef`, index size, quantization and filtered-ANN performance with labelled recall@K and p95 latency.
2. Choose exact lexical, vector, hybrid, or graph-enhanced retrieval per query complexity; run expensive reranking only when the cheaper evidence is insufficient.
3. Cache *by corpus revision, tenant, access policy and query intent*; invalidate impacted chunks and graph neighborhoods on source changes or permissions updates.
4. Return auditable evidence IDs, source revision, timestamps and citations with every result; require read-after-write consistency for security and deployment questions.
5. Enforce a retrieval objective such as minimize query cost and delivered context size **subject to** recall, grounding, authorization and freshness thresholds.
6. Evaluate separately on software code, user-facing support docs, multi-hop queries and multilingual queries. Include uncertain and adversarial cases. Compare exact-vs-ANN recall and generated-answer quality, not only context length.

The 3-SAT work motivates thinking about **better problem representations and search orders**. There is no proven algorithmic reduction from the 3-SAT method to RAG retrieval or agent orchestration.

### Retrieval research references

- [Anthropic: Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval) — contextual embeddings, BM25 and reranking
- [Qdrant: Search parameters](https://qdrant.tech/documentation/search/) — HNSW search breadth and filtering
- [Qdrant: Measuring approximate-nearest-neighbor recall](https://qdrant.tech/documentation/tutorials-search-engineering/ann-recall/) — recall vs latency
- [Microsoft GraphRAG: Query modes](https://microsoft.github.io/graphrag/query/overview/) — local, global and other graph-based retrieval


## Repository scope and publishing notes

This code is distributed as a self-contained research sample. See [SECURITY.md](SECURITY.md) and [PUBLICATION_NOTES.md](PUBLICATION_NOTES.md) before use. The local server intentionally binds to loopback only. No open-source license is granted unless one is subsequently added.
