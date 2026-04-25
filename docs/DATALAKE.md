# Datalake

The datalake is the product. Reports are views over it. Clones are
durable assets in it. Runs compound through it.

## 1. Why this layer is first-class

A codebase under active development accumulates the same questions
over and over:

- "What module was this behavior in before the refactor?"
- "Why was this dependency pinned here?"
- "Has this path caused regressions before?"
- "What does this type get used for across the repo?"

Rookery answers these from accumulated context. Without a datalake,
every session starts cold and every clone dies when its process does.
With one, the tenth session on a repository is meaningfully smarter
than the first — and that intelligence is portable (another operator,
another machine, another team member).

## 2. Three stores

| Store | Content | Characteristics | Backend |
|-------|---------|-----------------|---------|
| Blob | Raw corpus, sources, clone prefix files, reports | Immutable, content-addressed, potentially large | Local FS (default) or B2 |
| SQL index | Structured facts — symbols, findings, verdicts, score dims | Queryable, small rows, heavy WHERE clauses | DuckDB |
| Graph | Relationships — clone lineage, call graph, coverage, verdict chains | Traversal queries | Neo4j (optional, Stage 4+) |

Early stages run blob + SQL index only; the graph can be emulated via
join tables in DuckDB until traversal queries become painful.

## 3. Layout

```
.rookery/                          # per-repo datalake root (gitignored)
├── blobs/
│   ├── sha256/
│   │   └── {2-char-prefix}/{full-sha256}.blob
│   └── index.jsonl                # sha256 → {kind, size, created_at, source_refs}
├── clones/
│   ├── {clone_id}/
│   │   ├── v1/
│   │   │   ├── prefix.txt         # the append-only prefix
│   │   │   ├── prefix.sha256
│   │   │   ├── scorecard.json
│   │   │   ├── exemplars.jsonl
│   │   │   └── meta.json          # size_tokens, created_at, parent_version
│   │   ├── v2/...
│   │   └── current -> v2          # symlink to active version
│   └── ...
├── index.duckdb                   # SQL index
├── runs/
│   └── {run_id}/
│       ├── manifest.json
│       ├── verdicts.jsonl
│       ├── reports/
│       └── transcripts/
└── config.yaml
```

## 4. The SQL index schema (starting point)

Consciously minimal. Inspired by MiroThinker's `conditions` table but
narrowed to what code actually needs.

```sql
-- Every row in the datalake, typed.
CREATE TABLE rows (
    id           BIGINT PRIMARY KEY,
    repo_id      TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    row_type     TEXT NOT NULL,      -- 'symbol' | 'finding' | 'verdict' |
                                     -- 'exemplar' | 'raw' | 'mcp_result' |
                                     -- 'report' | 'tool_output'
    parent_id    BIGINT,
    related_id   BIGINT,

    -- Content refs
    content_sha  TEXT,                -- blob reference
    inline_text  TEXT,                -- for small content (<= 2 KB)

    -- Structural refs
    file_path    TEXT,
    line_start   INTEGER,
    line_end     INTEGER,
    symbol       TEXT,
    git_sha      TEXT,

    -- Scoring / flags (gradient dimensions)
    confidence   FLOAT DEFAULT 0.5,
    novelty      FLOAT DEFAULT 0.5,
    specificity  FLOAT DEFAULT 0.5,
    relevance    FLOAT DEFAULT 0.5,
    scored_at    TIMESTAMP,

    -- Verdict-specific
    verdict_kind TEXT,                -- 'APPROVE' | 'WARN' | 'BLOCK' | NULL
    clone_id     TEXT,
    intent       TEXT,
    target_refs  TEXT[],

    -- Bookkeeping
    consider_for_use BOOLEAN DEFAULT TRUE,
    obsolete_reason  TEXT,
    created_at   TIMESTAMP NOT NULL,
    metadata     JSON
);

CREATE INDEX idx_rows_repo_run   ON rows(repo_id, run_id);
CREATE INDEX idx_rows_type       ON rows(row_type);
CREATE INDEX idx_rows_clone      ON rows(clone_id);
CREATE INDEX idx_rows_path       ON rows(file_path);

-- Clone registry
CREATE TABLE clones (
    id              TEXT NOT NULL,      -- logical id, e.g. "invariant_keeper"
    repo_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    prefix_sha256   TEXT NOT NULL,
    size_tokens     BIGINT,
    scorecard       JSON,
    created_at      TIMESTAMP NOT NULL,
    retired_at      TIMESTAMP,
    predecessor     INTEGER,              -- version of parent clone
    PRIMARY KEY (id, repo_id, version)
);

-- Clone activation log — which clones were consulted, when
CREATE TABLE consultations (
    id              BIGINT PRIMARY KEY,
    repo_id         TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    intent          TEXT NOT NULL,
    clone_ids       TEXT[],
    duration_ms     INTEGER,
    budget_exhausted BOOLEAN,
    created_at      TIMESTAMP NOT NULL
);
```

This is the starting schema. It will grow. Migrations are versioned
and the schema version is a row in a dedicated `schema_version` table.

## 5. Cross-run federation

Every row carries `repo_id` and `run_id`. Queries default to all runs
for a repo (with `consider_for_use = TRUE`), so a new run's router
sees the combined history of verdicts, findings, and clone versions.

Practical consequences:

- The same clone ID survives across runs; only its version bumps.
- Verdict calibration draws on the full history.
- A new run can ask "has the Invariant Keeper ever blocked a change
  to this file?" and get a meaningful answer.

## 6. Clone persistence

The single most important datalake operation: a clone produced by a
tear-down phase survives the end of that run. Specifically:

1. The clone's prefix bytes are serialized to `blobs/` and
   content-addressed.
2. The clone's row in `clones` points at that SHA.
3. On a subsequent run, the orchestrator can pull the prefix, submit
   it to a running vLLM (or swap into DeepSeek's cache via API if the
   provider supports cross-session caching), and have the clone
   available after one prefill cost.

This is the piece that makes runs compound. Without it, every run
re-tears-down from cold. With it, tear-down is a weekly or
per-major-merge event, not a per-session one.

## 7. Garbage collection

The blob store grows without bound unless pruned. Policy:

- Clone prefix files: kept forever for currently-active clone
  versions; older versions pruned N days after supersession.
- Raw corpus shards: kept while any `rows` row references them.
- Tool-output blobs: pruned after 90 days if no verdict references
  them.
- Reports: kept forever.

GC is a background job; it never runs on a hot path.

## 8. Publishing (Stage 4+)

A repo's datalake should be shippable: tar the `.rookery/` directory
(or just the `clones/` and `index.duckdb` parts) and another operator
or machine can pick up where the first left off. This is the mechanism
by which rookery knowledge travels with the code, not with the
operator.

Exclusions for publishing:
- `runs/*/transcripts/` (may contain sensitive operator state)
- Any clone whose scorecard flags it as "not-yet-validated"
- Verdicts older than a configurable TTL
