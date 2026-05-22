# 5-Lane Retrieval

The "5 lanes" are retrieval strategies. The "4 namespaces" live inside Lane 1 (semantic). Orthogonal axes.

| Lane | Strategy | Backed by |
|---|---|---|
| 1 | Semantic | Pinecone vector similarity across 4 namespaces |
| 2 | Recency | Time-windowed query on recent decisions / active threads |
| 3 | Entity | Direct lookup by entity slug |
| 4 | Pressure | Owner-state-modulated salience boost |
| 5 | Foundation | Hard-rule lookup for active scope |

## Lane 1 namespaces and weights

```yaml
namespace_weights:
  solomon-corpus-wiki:      0.40    # synthesized, highest signal
  solomon-captured-items:   0.30    # owner's stated rules
  solomon-corpus-raw:       0.20    # grounding citations
  solomon-decision-log:     0.10    # historical context
```

Sum = 1.0. Per-query override allowed (e.g., a hard-rule lookup might force `captured-items: 1.0`). Default lives in `memory/pinecone-index.md`.

## Helper

`solomon-corpus-query` exposes a tool that hits all four namespaces with the configured weights, deduplicates, and returns ranked results with citation paths.
