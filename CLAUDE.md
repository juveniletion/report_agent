# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**PharmaCost-AI / 基于 llm-wiki 的智能分析面板** — a Chinese-language competition system for pharmaceutical cost analysis. FastAPI backend plus a React 18 frontend vendored locally (no npm, no build step).

The governing design principle is **「大模型不该算数，它该写文章」**: every number, delta, threshold, contribution ratio, and compliance check is computed by deterministic Python; LLM calls are confined to narrative analysis and causal explanation. Preserve this boundary when changing code — do not move arithmetic into prompts.

This repository is **code-only**. `raw/`, `wiki/`, `state/`, `graph/`, `reports/`, `exports/`, `data/`, and `*.db` are gitignored because they contain challenge data (`.gitignore`). A fresh clone has no knowledge base; the current workspace has a built `wiki/` (12 articles) plus `raw/`, `state/`, `graph/`, `.index/`, and `reports/`. Commands that read `wiki/` fail on a fresh clone until it is built. Never invent missing values — the rules below require explicit `【数据缺失…】` markers instead.

## Commands

All commands run from the repository root.

```bash
# Setup
pip install -r requirements.txt          # Python 3.10+; Docker image uses 3.13; includes fastembed
cp .env.example .env                     # DEEPSEEK_API_KEY for LLM paths; local ONNX embeddings need no key

# Run the dashboard
python server/app.py                     # http://127.0.0.1:8765 (no reload)
python server/app.py --reload            # dev only; reloads server/ only
docker compose up -d --build             # container path; first build installs CJK fonts/pandoc/LaTeX
docker compose logs -f
docker compose down                      # keeps the memory volume
docker compose down -v                   # ⚠️ deletes memory (irreversible)

# Build/refresh the knowledge base
python scripts/ingest_raw.py inbox/              # deterministic acquire: convert, hash-dedupe, briefing
python scripts/ingest_agent.py --inbox           # LLM compile raw/ → wiki/ (5–15 min)
python scripts/ingest_agent.py --brief <file>    # compile from an existing briefing JSON
python scripts/finalize.py                       # state → DB mirror → graph → evidence check → math check
python scripts/finalize.py --only state|db|graph|check|math

# Facts, reports, analysis
python scripts/report_facts.py --product 银黄口服液 --month 2026-05 --json
python scripts/report_build.py --product 银黄口服液 --month 2026-05 [--dry-run] [--theme monthly|quarterly|topic]
python scripts/export_doc.py --md reports/银黄口服液_2026-05.md --to both [--charts]
python scripts/benchmark_engine.py --product 银黄口服液 --month 2026-05 [--no-llm]
python scripts/forecast.py --product 银黄口服液 --horizon 1
python scripts/forecast_scenarios.py --product 银黄口服液   # historical-scenario forecast (no LLM)
python scripts/period_agg.py --product 银黄口服液 --quarter 2026-Q2

# Retrieval and agents
python scripts/retrieval.py "查询" [--top 6] [--no-online] [--rebuild]
python scripts/router.py --selftest            # routing decisions only, no model call
python scripts/router.py --ask "二厂单位成本"
python scripts/agent_runtime.py -q "银黄口服液 5 月为什么涨？" --user demo
python scripts/report_agent.py -q "..."
python scripts/db_build.py [--stats|--verify-mirror]
python scripts/graph_build.py --stats | --trace <节点>

# Validation (run the relevant ones after changing data or report logic)
python scripts/check_evidence.py .
python scripts/check_math.py [-v]
python scripts/check_report.py reports/银黄口服液_2026-05.md
python scripts/smoke_docker.py                  # 36 HTTP end-to-end checks; server must be running
python scripts/eval_report.py --no-llm          # delivery evaluation without LLM cost

# CLI auth (server auth is cookie-based; Bearer tokens are for scripts)
python scripts/agent_auth.py --users
python scripts/agent_auth.py --issue <user>
python scripts/agent_auth.py --set-pw <user>
```

There is **no pytest suite**. `scripts/smoke_docker.py` is the end-to-end verification entry. `scripts/test_agent_retrieval.py` is a development-only probe that needs a sibling `../agents/wiki_calling_agent.py` and a built `wiki/` — do not treat it as the project's test command.

## Architecture

### Three data layers with strict ownership

```
raw/       first truth. Humans/ingest_raw.py may only ADD. Never edit, rename, delete.
wiki/      second truth. Owned by the LLM via ingest_agent; every load-bearing value
           carries an inline evidence coordinate such as
           [成本汇总_2026.csv:单位成本(元/盒):银黄&2026-05] or [file.pdf:P3:章节].
state/     derived snapshot of key metrics (metrics.json).
graph/     derived knowledge graph (current build ~79 nodes / 58 edges; every node carries a coord).
.index/    derived BM25 + local ONNX vector index (vectors.json).
data/shared/knowledge.db   derived SQLite mirror (mirror_* tables only).
data/auth.db, data/users/<id>.db   authoritative, NOT rebuildable (accounts, conversations).
```

The judgment rule: **when a derived view disagrees with `wiki/`, the derived view is wrong.** Rebuild it; never hand-edit `state/`, `graph/`, `.index/`, or `mirror_*`. `db_build.py` deliberately contains no DELETE against authoritative tables.

`SKILL.md` is the rule layer (iron rules, evidence invariant, domain constraints, ingest/lint/query procedures). Changes there need human approval. `INGEST_AGENT.md` and `AGENT_RUNTIME.md` are the normative prompts/design docs for their respective agents.

### Determinism boundary

- `scripts/report_facts.py` is the single source of computed product/cost numbers. It reads the fixed set of CSVs under `raw/` and attaches coordinates. It also supports personal cost summaries via `report_context(personal_root=..., use_personal=True)`: personal rows override shared rows by `(product, month)` after column and identity checks.
- `scripts/report_build.py` fills the report template with those script-computed facts and calls the LLM only for the six narrative analysis sections. `--dry-run` leaves out LLM prose. `use_personal=True` writes the report into the user's workspace; the Word-template path also inserts the three charts.
- `scripts/forecast.py` and `scripts/forecast_scenarios.py` are explicit **extrapolations** (no raw evidence coordinates). Keep their output out of the six-chapter report's factual body and label ranges as scenario ranges, not confidence intervals.
- `server/app.py` imports `report_facts` and never recomputes numbers; it serves files and orchestrates.
- `check_math.py` independently recalculates derived values with `Decimal`; `check_evidence.py` checks literals/links/coverage; `check_report.py` checks chapter/table/placeholder/RPA contract. They intentionally do not overlap — don't merge them.

### Server and agents

`server/app.py` (FastAPI) is the deterministic orchestrator. Endpoint groups: read-only dashboard (`/api/health`, `/products`, `/overview`, `/trend`, `/heatmap`, `/conflicts`, `/timeline`, `/graph`, `/benchmark`, `/forecast`, `/forecast/scenarios`, `/decide`), search/Q&A (`/api/search`, `/api/chat` SSE, `/api/route`), personal control panel (`/api/control/*` upload/ingest/generate/export/download/my-reports/my-download/my-export), and RPA tracking (`/api/rpa/*`). Expected failures must raise `DataMissing` (→503) or `ConfigError` (→501) so the JSON body reaches the client; do not use `SystemExit` in request paths.

There are **three independent ReAct agents** (`langgraph.prebuilt.create_react_agent`), not a multi-agent system and not orchestrated by another model:

- `scripts/agent_runtime.py` — main Q&A agent used by `/api/chat`; reads `wiki/`, reads `raw/`, writes only its own memory. Contains memory, context compaction (CJK-aware), and tool guards (`LoopGuard`, truncation, result dedupe).
- `scripts/report_agent.py` — read-only wiki/raw evidence agent; no memory, no writes.
- `scripts/ingest_agent.py` — reads `raw/`, writes `wiki/` through `ingest_tools.py`; re-reads its spec from disk on every run.

They share the filesystem (the same `wiki/`), not messages. `server/app.py` decides what to call; there is no orchestrator agent.

### Retrieval, routing, frontend

- `scripts/retrieval.py`: BM25 over character bigrams (CJK) + vector side via **local ONNX fastembed (`BAAI/bge-small-zh-v1.5`, 512-dim) by default** + RRF (`k=60`). Set `EMBED_PROVIDER=siliconflow` to use bge-m3 over HTTP. Without a usable model it degrades to BM25. The index lives in `.index/vectors.json` and is rebuilt when the embedding spec changes.
- `scripts/router.py`: deterministic three-tier ladder — library direct answer (0 tokens) → Qwen2.5-7B with output validation → deepseek-chat. Routing is a script, never an LLM judgment; small-model answers are checked for hallucinated numbers and false negatives, and escalate on failure.
- `server/static/app.js` is the entire frontend: React 18 UMD + htm + ECharts, all vendored in `server/static/vendor/`. There is no npm/build step; edit the JS directly. Comments and user-facing strings are Chinese.

### Data roots and isolation

- `scripts/paths.py` resolves the data root in this order: `LLM_WIKI_DATA` → `/data` → repo `data/`. `LLM_WIKI_DB` is legacy and **does not affect resolution**.
- `LLM_WIKI_WS` injects a workspace root (used by `ingest_tools`, `state`, `graph_build`, and personal uploads). The spec root (`SKILL.md`, `references/`) is always the code repo and must never come from user input.
- Docker mounts `raw/ wiki/ state/ graph/` read-only, `reports/ exports/` writable, and `.index/` writable (embedding index cache), one directory at a time, because most scripts hardcode `WIKI_ROOT = HERE.parent`.
- Auth: server-side session + HttpOnly cookie, PBKDF2-SHA256 passwords, CSRF double-submit, one SQLite file per user (`data/users/<id>.db`) for physical isolation. Shared `wiki/`/`raw/` are read-only; writes go only to agent memory or the uploader's workspace. Personal reports live under `data/users/<id>/ws/reports/` and are served by `/api/control/my-*`, never mixed into shared `reports/`.
- RPA: `server/mock_rpa.py` listens on internal port 8090 (not published). Tasks are parsed from section 6.4 of a report rather than regenerated; task IDs include the product code. If the mock is down, return `degraded: true` and persist locally — never report a fake success.

## Non-negotiable domain rules

These are encoded in `SKILL.md` §2, §9, §10 and enforced in prompts/validators. Violating them produces wrong-magnitude reports or fabricated root causes.

- **Units:** cost summary is always `元/盒`; industry benchmark is `元/支|袋|粒`. Never convert on your own. If cross-table comparison is required, show both units and a separate derived value with the formula. `1 盒 = 10 支 / 20 袋 / 60 粒`.
- **Precision:** copy literals exactly (`11.21`, not `11.2` or `约 11`). Zero-padding (`11.6` → `11.60`) is allowed; truncation is not. Show the components and formula for every derived value.
- **Evidence:** every key number needs an inline coordinate. Missing data gets `【数据缺失: 该文件中未包含此项】`. Conflicting sources keep both values in a `> **Status: Disputed**` block with an engineering judgment.
- **Equipment IDs:** `EQ-<类别>-<三位流水>`, e.g. `EQ-TQ-001`. `TQ-01` does not exist and is treated as a hallucination.
- **Root cause:** the official cause for 银黄口服液's 4–6 月 cost rise is **提取工艺收率波动**, not equipment failure. The only 2026 maintenance event is the March capsule filling machine; `EQ-TQ-001` has no 2026 failure.
- **Validators report, they do not fix.** Suspect values are candidates for human judgment (`SKILL.md` §7.2 lists known false positives).

## Conventions

- Documentation, code comments, UI text, and error messages are Chinese. Preserve existing Chinese terminology and the "why this exists" incident comments when editing; they encode hard-won constraints.
- New report facts belong in `report_facts.py`; new deterministic decisions belong in scripts (`decide.py`, `router.py`, `benchmark_engine.py`), not in prompts.
- The report template source is `raw/templates/月度成本分析报告模板.md`; the Web UI's 「使用 Word 模板」 checkbox passes `template_docx` and fills the provided `.docx` (the Word path also inserts charts). Keep one template as the single source of truth rather than letting `.md` and `.docx` drift.
- `.gitignore` patterns must be on standalone lines — Git does not support inline comments after patterns; an inline comment makes the ignore rule silently ineffective. Sensitive directories include `raw/`, `wiki/`, `state/`, `graph/`, `reports/`, `exports/`, `inbox/`, `.index/`, and the challenge/delivery archives.
- `report_facts.py` reads a fixed list of CSV filenames, so newly uploaded CSVs flow into retrieval/Q&A but not into the monthly report unless that list is extended. Personal cost CSVs are an explicit exception, handled through `report_context`.
