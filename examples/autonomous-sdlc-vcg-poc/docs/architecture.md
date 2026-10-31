# Autonomous SDLC: proposed production architecture

The project is a deterministic **research simulation**. The following flow is a *target architecture*, not a claim that all connectors or cloud coding agents have been integrated.

```mermaid
flowchart TD
    H[Human supervision / budgets / approvals] --> O[Adaptive orchestrator]
    L[Linear issues and DoD] --> O
    G[GitHub repository / PRs / CI] --> O
    X[Sanitized OpenTelemetry signals] --> O
    O --> D[Deduplicate intents / dependency DAG / budget allocation]
    D --> A[Isolated builder agents]
    D --> B[QA and test agents]
    D --> C[Independent review agents]
    A --> Q[Revision-bound checks]
    B --> Q
    C --> Q
    Q --> P{Policy + human approval gate}
    P -->|authorized| R[PR merge / deploy]
    P -->|not authorized| O
    R --> X
    V[(Versioned Context Graph)] <--> O
    V <--> A
    V <--> B
    V <--> C
```

## Adaptive retrieval layer

```mermaid
flowchart LR
    Q[Task + tenant + revision] --> RT[Query router]
    RT --> BM[Exact / BM25]
    RT --> SE[Semantic retrieval]
    RT --> GR[Graph traversal]
    BM --> FU[Fuse, deduplicate, rerank]
    SE --> FU
    GR --> FU
    V[(Versioned Context Graph)] --> BM
    V --> SE
    V --> GR
    FU --> F{Fresh, authorized, enough evidence?}
    F -->|yes| PK[Minimal evidence package]
    F -->|no| RT
    PK --> AG[Agent]
```

## Optimization objective

Minimize total model, retrieval, retry, tool and coordination cost **subject to** non-inferior completion quality, evidence recall, test pass, review independence, freshness, access control and human approvals.

Production measurement must separate hosted-model token use from cloud-agent container RAM/CPU and model-serving hardware memory (typically not exposed).

## Why the 3-SAT connection is only an analogy

Recent AI-assisted theoretical work on 3-SAT motivated the question of whether changing a computational representation or search order can reduce computation. It does **not** provide a proven complexity improvement for these agent or RAG workflows. Any claimed savings need controlled benchmarks with real agents, fixed task sets and independent quality gates.
