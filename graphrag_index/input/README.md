# GraphRAG Input Documents

Documents placed here are indexed by `graphrag index --root ..` and become
traversable nodes and relationships in the knowledge graph.

## Document Types

| Prefix | Description |
|---|---|
| `failure_retrospective_*.txt` | Written by Watchdog on every rollback/failure |
| `pr_description_*.txt` | PR descriptions indexed before merge |
| `code_change_summary_*.txt` | Summaries of significant code diffs |
| `approval_history_*.txt` | Vault approval/rejection reasoning |

## Adding Documents

Watchdog calls `GraphRAGClient.index_document()` automatically after each failure.
For manual indexing, drop a `.txt` file here and re-run:

```bash
graphrag index --root .. --update
```

## Entities GraphRAG Will Extract

- `file` — source files modified in changes
- `function` — specific functions mentioned
- `pull_request` — PR numbers and their outcomes  
- `failure` — specific failure events with root causes
- `deployment` — deployment events and health state
- `developer` — (anonymised) approvers and reviewers

## Relationships

GraphRAG learns edges like:
- PR #42 **modified** `auth/jwt.py`
- `auth/jwt.py` **caused** failure #7
- failure #7 **triggered** rollback on 2024-03-15
- PR #51 **fixed** failure #7
