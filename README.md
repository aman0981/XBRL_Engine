# XBRL Intelligence Engine

Consumption-side XBRL analytics engine for SEC EDGAR (USA) and ESMA ESEF (EU) filings. Ingests, parses, normalizes, and reconciles iXBRL disclosures; projects canonical financials (Revenue, Net Income, Total Assets, Operating Cash Flow, EPS); reconstructs primary statements in the filer's presentation order; detects retroactive restatements via a dimension-, unit-, and decimals-aware bitemporal model.

**Status:** Phase 7.5 (Mode C user-upload) complete. Active development — see [XBRL_PLAN.md](XBRL_PLAN.md) for the full 15.5-weekend roadmap.

---

## What works today

### Phase 0 — Repo scaffold

- FastAPI app boots, `/healthz` returns `{"status":"ok"}`, `/readyz` checks Postgres connectivity.
- `/metrics` exposes Prometheus counters and histograms:
  - `xie_filings_ingested_total{regulator,mode,status}`
  - `xie_facts_extracted_total{regulator,source}`
  - `xie_filing_parse_seconds{regulator,mode}`
- Structured logging via `structlog` (console renderer locally, JSON in container).
- Async SQLAlchemy 2.0 + asyncpg engine, Alembic migrations wired to declarative `Base`.
- Docker Compose stack: app + Postgres 16 with healthcheck and persistent volume.

### Phase 1 — SEC EDGAR client + raw filing store

- Async **SEC EDGAR HTTP client** with mandatory User-Agent, shared 10 req/s rate limit (`aiolimiter`), exponential retries on 429/503 via `tenacity`, and explicit 403 → `SECForbiddenError` (no retry).
- **Ticker → CIK10** resolver using SEC's public `company_tickers.json` (cached once per process).
- **Submissions discovery** — paginated walk of `data.sec.gov/submissions/CIK{cik10}.json` including `files.recent` overflow pages; filterable by form-type and limit.
- **Filing index + blob store** — walks `Archives/edgar/data/.../index.json`, downloads every document into `data/raw/sec/{cik10}/{accession_nodash}/`, writes a `manifest.json` with sha256 of every file.
- **`filings` table** (Alembic migration `20260523_0001`): regulator + entity_id + accession_no, period_of_report, form_type, ixbrl_url, raw_sha256, raw_path, ingestion_mode, amendment chain FK.
- **Ingestion pipeline** upserts `filings` rows with `regulator='SEC'`, `ingestion_mode='source_ixbrl'`, `ingestion_status='raw_downloaded'`.
- **CLI**: `python -m xie.ingest.cli fetch --ticker AAPL --form 10-K --limit 1 [--persist]`.

**Live demo verified:** Apple FY2025 10-K (`0000320193-25-000079`) fetched end-to-end — 93 documents (primary iXBRL `aapl-20250927.htm`, presentation/calculation/definition/label linkbases, xsd, `MetaLinks.json`, rendered exhibits) persisted to disk with sha256 manifest.

### Phase 1.5 — SEC companyfacts bootstrap (Mode A)

- **`SubmissionsClient.companyfacts`** — fetches `data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json` (every XBRL-tagged numeric fact ever filed by the CIK, pre-parsed by SEC).
- **Mode A parser** — flattens companyfacts JSON into typed `CompanyFact` records (qname, unit, value, period_start/end/instant, accession, form, fiscal_year/period, frame).
- **Schema expansion** (Alembic migration `20260523_0002`): `contexts` + `units` + `facts` + `canonical_line_items` tables.
- **Canonical map** — regulator-aware ordered fallback chain for US-GAAP, covering concept migration (e.g. `SalesRevenueNet` → `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` across ASC 606 transition). Five canonical names: Revenue, NetIncome, TotalAssets, OperatingCashFlow, EPS.
- **Mode A pipeline** — companyfacts → upsert stub `filings` rows (`ingestion_mode='companyfacts'`) → synthesise `contexts` (no dimensions) + `units` → bulk-upsert `facts` (`source='companyfacts'`) → project `canonical_line_items` via fallback chain.
- **Web UI** — `/` index, `/companies/{ticker_or_cik}` HTMX dark-themed view with canonical time-series + per-canonical mapping (source qname, method, confidence). Ticker→CIK10 resolution baked in.
- **CLI**: `python -m xie.ingest.cli bootstrap-companyfacts --ticker AAPL`.

**Live demo verified:** Apple Inc. (`CIK 0000320193`) Mode A ingest — 24,852 facts persisted, 71 stub filings, 552 canonical line-items spanning 2009–2026. Page `/companies/AAPL` shows Revenue $394.328 B (FY2024), Net Income $99.803 B, Total Assets $352.583 B, OCF $122.151 B, Diluted EPS $6.11 — matches Apple's 10-K. Fallback chain correctly resolves `SalesRevenueNet` (pre-ASC-606) → `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` across years.

### Phase 2 — lxml fact extractor (Mode B fidelity)

- **`xbrl/transforms.py`** — iXBRL Transform Registry numeric/date table covering TR3 / TR4 / TR5 (current REC) plus TR6 PWD prefixes. Supports `numdotdecimal`, `numcommadecimal`, `num-dot-decimal-apos`, `num-unit-decimal`, `numdash`, `zerodash`, `nocontent`, `fixed-zero`, `fixed-true`, `fixed-false`, `numwordsen`. Applies `scale` and `sign` per spec.
- **`xbrl/registry_dispatcher.py`** — maps the `format` attribute's namespace URI (`http://www.xbrl.org/inlineXBRL/transformation/{YYYY-MM-DD}`) to a `TRVersion` enum so each fact gets stamped with its transform-registry generation.
- **`xbrl/ixbrl_parser.py`** — hardened lxml extractor (`resolve_entities=False`, `no_network=True`, `huge_tree=False`). Extracts:
  - `xbrli:context` rows with period (duration / instant) + `xbrldi:explicitMember` / `typedMember` dimensions (full JSONB + deterministic sha256 hash)
  - `xbrli:unit` rows including `xbrli:divide` numerator/denominator pairs (for USD-per-shares etc.)
  - `ix:nonFraction` and `ix:nonNumeric` facts including `ix:hidden` (flagged `is_hidden=true`) and `ix:continuation`-joined text blocks
  - `link:schemaRef` + `link:linkbaseRef` DTS references (handed to Arelle in Phase 3)
- **`ingest/modeb_pipeline.py`** — parses the previously-downloaded primary iXBRL → upserts `contexts` + `units` + `facts (source='source_ixbrl')` + `fact_dimensions` (delete-then-insert keeps it idempotent).
- **`ingest/reconcile.py`** — fact-by-fact diff between Mode A (companyfacts) and Mode B (source iXBRL) for the same filing, persisted into `fact_reconciliation` with categories `only_in_a` / `only_in_b` / `value_diff` / `precision_diff`. This is the named differentiator.
- **`fact_dimensions` + `fact_reconciliation`** tables (Alembic migration `20260523_0003`).
- **CLI**: `parse-filing --accession {accession} [--primary path]` and `reconcile --accession {accession}`.
- **Web UI**: `/filings/{accession}/facts` (Mode A / Mode B / hidden-only filter, with per-fact qname, period, unit, value, decimals, format, TR version, flags) and `/filings/{accession}/reconciliation` (mismatch summary + top 500 findings).

**Live demo verified:** Apple FY2025 10-K (`0000320193-25-000079`) Mode B parse:
- **1,042 facts** (deduped from 1,131 raw `ix:nonFraction` + `ix:nonNumeric`), **182 contexts**, **7 units**, **712 fact_dimensions**.
- Transform-registry distribution: 648 TR4, 0 TR5 (Apple still emits TR4-format-prefixed transforms in their FY2025 filing, even though TR5 is the current REC).
- Sample fact resolves: `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` = `$416,161,000,000` (FY2025 actuals) and `$391,035,000,000` (FY2024 comparative) — matches Apple's reported figures.

**Reconciliation (Mode A vs Mode B, same filing):**
- 1,101 unique `(concept, period, unit)` keys examined.
- **only_in_b: 674** — facts our lxml parser captured that SEC's `companyfacts` API strips (mostly dimensional segment breakdowns, hidden facts).
- **only_in_a: 410** — facts companyfacts retains across this CIK that our single-filing parse didn't surface (typically comparative-period restatements pulled from companion filings).
- **precision_diff: 16** — same numeric value but different `decimals` attribute (e.g. SEC rounds to `-3` while filer wrote `-6`).
- **value_diff: 1** — `us-gaap:StockRepurchasedAndRetiredDuringPeriodShares` differs because SEC's companyfacts rounded to 3-decimal precision (402 M shares) while the source iXBRL carries the exact 401,672,000 — a real data-quality artefact of SEC's pre-processing.

### Phase 3 — Arelle sidecar + linkbases + DQC

- **`xbrl/arelle_worker.py`** — in-process Arelle controller (`arelle.Cntlr`). On load, Arelle resolves the filing's **DTS** (Discoverable Taxonomy Set) — pulling US-GAAP, DEI, SRT, country/exchange/currency taxonomies and the filer's extension XSD — caches everything under `data/arelle_cache/`, then exposes typed concept and relationship objects.
- **Concept enrichment** — for every QName Arelle resolves, persist a row in `concepts` with `standard_label`, `documentation`, `period_type` (`duration` / `instant`), `balance_type` (`debit` / `credit`), `data_type`, and `is_extension` (derived from the QName's namespace — filer's own namespace = extension; FASB / IFRS / DEI / SRT / xbrl.org = standard).
- **Linkbase walk** — `model_xbrl.relationshipSet(arcrole, linkrole)` yields parent-child arcs for:
  - **presentation** (`parent-child` arcrole) — filer's rendering order
  - **calculation** (`summation-item` arcrole) — signed weights, basis for math validation
  - **definition** (XDT `dimension-domain` / `domain-member` / `hypercube-dimension` / `all` / `notAll` / `dimension-default`, plus ESEF `wider-narrower` for Phase 6).
- **Built-in Arelle validations** captured into `dqc_findings` (full XULE/XBRL-US DQC plugin invocation deferred — see "Not done" below).
- **`concepts` + `concept_arcs` + `dqc_findings` tables** (Alembic migration `20260524_0004`), plus a `qname` regex CHECK constraint that catches malformed concept names at insert.
- **Off-event-loop execution** — Arelle's load is blocking + holds ~1 GB RAM during DTS resolution; `EnrichPipeline.run` invokes it via `asyncio.to_thread` so the connection pool stays free.
- **Chunked upserts** — Postgres caps bind params at 32,767; concepts insert is chunked at 2,000 rows × 10 cols, arcs chunked at 1,000 rows × 8 cols.
- **CLI**: `enrich-filing --accession {accession} [--primary path]`.
- **Web UI** — `/filings/{accession}/facts` now shows the standard label, period type, and balance type next to each fact qname, plus a "DQC / Arelle findings" summary table and an Arelle validation badge on the filing header.

**Live demo verified:** Apple FY2025 10-K enrichment:
- **18,784 concepts** persisted (US-GAAP + DEI + SRT + linkbase-XLink schemas + AAPL extensions).
- **2,280 linkbase arcs** persisted (767 presentation, 213 calculation, 1,300 definition).
- Calc-linkbase example: `us-gaap:Assets` = `AssetsCurrent` (weight 1.0) + `AssetsNoncurrent` (weight 1.0), per the FASB calc linkbase.
- Concept enrichment spot-check: `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` resolves to `"Revenue from Contract with Customer, Excluding Assessed Tax"`, `duration`, `credit`. `aapl:NumberOfSignificantVendors` correctly flagged `is_extension=true`.
- **2 DQC findings** captured: `ix11.10.1.2:invalidTransformation` + `ix11.11.1.2:invalidTransformation` — Arelle flags SEC's legacy date-transformation namespace `http://www.sec.gov/inlineXBRL/transformation/2015-08-31` as unrecognised (real, known gap between SEC's internal transformation set and the xbrl.org REC set).
- Total wall-clock for second-run enrichment (warm cache): ~13 s.

### Phase 4 — Canonical projection + statement reconstruction + on-demand fallback

- **`xbrl/canonical_project.py`** — single projector that builds `canonical_line_items` from `facts` for either Mode A (`source='companyfacts'`), Mode B (`source='source_ixbrl'`), or both. Mode B rows beat Mode A on ties (more accurate). Calc-linkbase awareness: when the picked concept appears as a `from_concept_id` of any `concept_arcs` row with `linkbase_type='calculation'`, the row is stamped `mapping_method='calc_linkbase_root'` with a confidence boost — i.e. the engine *knows* a concept like `us-gaap:Assets` is a calc-tree total, not just a fallback hit.
- **`xbrl/linkbase.py`** — async helpers that walk `concept_arcs` for a given extended-link-role (ELR) to materialise a `StatementNode` tree (parent → children, ordered by `arc_order`) and join with `facts` for that filing on primary (non-dimensional) contexts to render filer-order primary statements.
- **Role definitions captured during enrichment** — `ArelleEnricher` now reads `model_xbrl.roleTypes` and yields `EnrichedRole(uri, definition, used_on)`. `EnrichPipeline._upsert_roles` persists into the new `role_definitions` table; UI uses `definition` as the human label (e.g. `"9952151 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS"`).
- **`role_definitions` table** (Alembic migration `20260524_0005`), unique on `uri`, with `used_on` recording which linkbase types reference the role.
- **CLI**: `canonicalize --ticker {ticker} | --cik {cik10}` re-runs the projector for any entity once new facts (from either mode) land.
- **Web UI** — `/filings/{accession}/statements` lists every filer-defined statement role (filtered by definition heuristic: "statement / balance / cash flow / operations / equity") and links to per-statement render at `/filings/{accession}/statements/{role_b64}`. The render shows the presentation tree with indentation, with one column per primary (non-dimensional) context — Apple's 10-K thus shows Period-over-Period comparatives in the filer's column order.
- **On-demand ingest (Locked Decision #18)** — `POST /companies/{ticker_or_cik}/ingest` triggers a `BackgroundTasks` Mode A bootstrap for any ticker that isn't in the DB yet; the company page shows an "Ingest now" button when canonical rows are empty.

**Live demo verified:**
- Apple FY2025 10-K re-enriched → **767 role definitions** captured. Apple-specific statement ELRs visible:
  - `http://www.apple.com/role/CONSOLIDATEDSTATEMENTSOFOPERATIONS`
  - `http://www.apple.com/role/CONSOLIDATEDBALANCESHEETS`
  - `http://www.apple.com/role/CONSOLIDATEDSTATEMENTSOFCASHFLOWS`
  - `http://www.apple.com/role/CONSOLIDATEDSTATEMENTSOFSHAREHOLDERSEQUITY`
- `canonicalize --ticker AAPL` re-projected with calc-linkbase awareness → **556 canonical line-items** (compared to 552 from Mode A only — the extra rows come from Mode B's FY2025 facts that companyfacts hadn't surfaced yet for the freshest filing).
- `/filings/0000320193-25-000079/statements` lists 30+ Apple-defined statements in filer order.
- `/filings/0000320193-25-000079/statements/{Income-Statement-role-b64}` renders the Consolidated Statement of Operations with `Net income`, `Revenue`, `Earnings per share` labels resolved from the US-GAAP taxonomy and three period columns from the filing's contexts.

### Phase 5 — Restatement detection (bitemporal, SCD-Type-2)

- **`xbrl/restatement.py`** — single Postgres CTE that partitions every entity's facts by the natural restatement key — `(concept_qname, period_start, period_end, period_instant, dimensions_hash, unit_signature)` — then `ROW_NUMBER() OVER (... ORDER BY filings.filing_date)` to find consecutive (prev, next) pairs where the value differs. Each differing pair becomes a candidate.
- **`compare_at_precision(a_value, a_dec, b_value, b_dec)`** — when the two facts have different `decimals` exponents, the values are rounded to the *coarser* of the two (smaller / more negative = coarser; SEC's `-6` rounds to nearest 1,000,000 vs `-3` rounds to nearest 1,000). If the rounded values match, the difference is `precision_change=True` and is skipped from the material-restatement list. `decimals='INF'` short-circuits — exact match required.
- **`restatements` table** (Alembic migration `20260524_0006`) — stores both sides of the pair (original_filing_id, original_value, original_decimals_text, original_reported_at) + (restated_filing_id, restated_value, restated_decimals_text, restated_reported_at), plus delta + delta_pct, and three boolean flags: `is_amendment` (restated filing is form `*/A` or has `amends_accession_no`), `is_concept_migration` (ASC 606-style rename — Phase 7 polish), `is_precision_change`.
- **Mode B fact period denormalisation** — Phase 2 stored period info on `contexts` only; the restatement partition key needs it on `facts`. Phase 5 added `period_start` / `period_end` / `period_instant` to the Mode B persist + included them in the `ON CONFLICT DO UPDATE` set list so existing rows update on re-parse.
- **CLI**: `detect-restatements --ticker {ticker} | --cik {cik10}` — runs the CTE, idempotently replaces the entity's prior rows.
- **Web UI** — `/companies/{ticker_or_cik}/restatements` lists the top 500 material restatements (precision-only filtered out), with concept, period, original/restated value, delta, delta %, both reported-at dates, and an `amendment` badge.

**Live demo verified:** Apple Inc. detection over 24,852 Mode A facts + 1,042 Mode B facts:
- **449 material restatements** persisted (down from 698 before period denorm, all duplicates that collapsed across periods with NULL period_end).
- 77 amendment-flagged restatements (form ends in `/A` or carries `amends_accession_no`).
- **Top finding: Apple's August 2020 4-for-1 stock split.** Across `CommonStockSharesAuthorized`, `CommonStockSharesIssued`, `CommonStockSharesOutstanding`, `WeightedAverageNumberOfSharesOutstandingBasic`, `WeightedAverageNumberOfDilutedSharesOutstanding`, EVERY share count from FY2018, FY2019, Q1 FY2020 was restated **+300%** (i.e. 4× the original) between Jul 2020 and Oct 2020 — exactly matching the split's effective date.
- Other real findings: `StockRepurchasedAndRetiredDuringPeriodValue` FY2013 went $9.0B → $22.95B (+155%) between Oct 2013 and Oct 2015 — a real reclassification.
- NetIncomeLoss restatements for 2007-2009 reflect stock-split-driven per-share denominator changes propagating up to per-share earnings.
- Zero precision-only rows (companyfacts and source iXBRL agree on `decimals` precision for AAPL — clean filer).

### Phase 6 — ESEF ingestion (EU)

- **`regulators/esef/client.py`** — thin async wrapper around the [`xbrl-filings-api`](https://pypi.org/project/xbrl-filings-api/) library that hits `filings.xbrl.org/api/filings` (JSON:API). Search by `entity.identifier` (LEI) or `country` (ISO 3166-1 alpha-2). Library calls are blocking; we run them via `asyncio.to_thread`.
- **`regulators/esef/taxonomy_package.py`** — unzips an ESEF Taxonomy Package (REC 2016-04-19) into `data/raw/esef/{LEI}/{api_id}/unpacked/`, reads `META-INF/taxonomyPackage.xml` for the entry-point URI, locates the primary iXBRL xHTML report and the filer's extension XSD.
- **IFRS canonical fallback chain in `canonical_map.py`** — `IFRS_FALLBACKS` covers `ifrs-full:Revenue` → `RevenueFromContractsWithCustomers`, `ifrs-full:ProfitLoss` → `ProfitLossAttributableToOwnersOfParent`, `ifrs-full:Assets`, `ifrs-full:CashFlowsFromUsedInOperatingActivities`, `ifrs-full:DilutedEarningsLossPerShare` → `BasicEarningsLossPerShare`. `find_canonical(qname, regulator="ESEF")` dispatches to it.
- **Regulator-aware canonical projection** — `project_canonical_for_entity(session, entity_id, sources, regulator)` now takes a `regulator` argument; SEC defaults remain. Mode B pipeline reads the filing's `regulator` and switches sources to `('source_ixbrl',)` for ESEF (no companyfacts equivalent for EU filings).
- **ESEF Mode B pipeline (`ingest/esef_pipeline.py`)** — discover → download package → unzip → reuse `ModeBPipeline.parse_and_persist` on the primary iXBRL → project canonicals with `regulator='ESEF'`. The accession_no is synthesised as `ESEF-{api_id}` since ESEF has no SEC-style identifier.
- **LEI routing** — `/companies/{token}` distinguishes 10-digit CIK, 20-char LEI ([A-Z0-9]), and ticker by shape. Same template renders both. `_annual_canonical` and `_entity_name` are regulator-agnostic.
- **Bug fix: NULL period_end / period_instant duplicates** — Postgres treats NULL as ≠ in unique constraints, so re-running `canonicalize` was silently duplicating rows for canonicals with one of the period columns NULL (e.g. Revenue with `period_instant` NULL). `project_canonical_for_entity` now snapshot-rebuilds (DELETE-then-INSERT) per (regulator, entity_id) so re-runs are truly idempotent.
- **CLI**: `fetch-esef --lei {LEI} | --country {ISO2} [--limit N]`. `canonicalize --cik {LEI}` auto-detects LEI shape and re-projects under `regulator='ESEF'`.

**Live demo verified:** Hermès International ESEF FY2025 filing:
- LEI `969500Y4IJGHJE2MTJ13` discovered via filings.xbrl.org → package ZIP 12 MB downloaded → unpacked → primary iXBRL `reports/hermes-2025-12-31-1-fr.html` identified
- Parsed: **530 facts** (deduped from 543), **56 contexts** including segment-axis dimensional contexts, **3 units** (`EUR`, `EURPerShare`, `shares`), **190 fact_dimensions**.
- French-locale numerics handled: format `ixt:num-comma-decimal` (TR5) correctly parses `"16 002"` with `scale=6` into `Decimal("16002000000")`.
- **Canonical FY2025 (€):** Revenue **16.002B**, Net Income **4.560B**, Operating Cash Flow **5.374B**, Total Assets **24.322B** (instant @ 2025-12-31). FY2024 comparatives: Revenue 15.170B, NI 4.631B, OCF 5.139B, Assets 23.084B. All values match Hermès's published FY2025 results, resolved through the IFRS fallback chain.
- `/companies/969500Y4IJGHJE2MTJ13` renders the same template Apple uses, with the IFRS qnames in the mapping table.

### Phase 7 (partial) — Dockerfile + Compose polish

- **Multi-stage Dockerfile** ([Dockerfile](Dockerfile)). A `builder` stage installs build-essential + libxml2/libxslt headers and compiles the venv at `/opt/venv` (lxml, numpy, pillow for Arelle, asyncpg). A `runtime` stage starts from a fresh `python:3.12-slim`, installs only the runtime shared libs (`libxml2`, `libxslt1.1`, `curl`, `ca-certificates`) plus a non-root `xie` user, and copies the pre-built `/opt/venv` over. Final image: **566 MB** (vs ~720 MB single-stage).
- **Non-root by default** — `USER xie` after creating `/data`, `/data/raw`, `/data/arelle_cache` with `xie:xie` ownership. App can't write outside the declared mount points.
- **HEALTHCHECK** baked into the image (`curl --fail --silent http://127.0.0.1:8000/healthz`, 15 s interval, 3 s timeout, 3 retries, 10 s start grace).
- **Reverse-proxy ready** — uvicorn launches with `--proxy-headers --forwarded-allow-ips=*` so Caddy / Traefik / nginx in front of it (Hetzner deploy) gets correct `X-Forwarded-For` / `X-Forwarded-Proto`.
- **`.dockerignore`** keeps `data/raw/` (the 24 K AAPL facts on disk), `venv/`, `__pycache__`, `.pytest_cache`, IDE folders, and dev-only docs out of the build context.
- **docker-compose updated** ([docker-compose.yml](docker-compose.yml)):
  - Named volumes for `pgdata`, **`xie_raw`** (`/data/raw`), **`xie_arelle_cache`** (`/data/arelle_cache`) so taxonomy + filing cache persists across `docker compose down`.
  - App service has `healthcheck`, `restart: unless-stopped`, `init: true` (PID-1 reaper), 2 GB memory limit (Arelle DTS load peaks ~1 GB), and rotating JSON file logs (10 MB × 3 files).
  - `XIE_RAW_DATA_DIR=/data/raw` and `XIE_ARELLE_CACHE_DIR=/data/arelle_cache` exported so the app writes to the mounted volumes, not container-ephemeral paths.

**Verified live:**
- `docker build -t xbrl-intelligence-engine:local .` → 566 MB image, 21 s after cold pulls.
- `docker run -d -p 8765:8000 -e XIE_DATABASE_URL=... xbrl-intelligence-engine:local` → container boots, structlog JSON logs to stdout, `/healthz`, `/readyz` (host Postgres via `host.docker.internal`), and `/metrics` all 200 OK.
- `docker inspect --format "{{.State.Health.Status}}" xie_test` → `healthy` after 4 consecutive probes.
- `pytest -m docker` → 9 / 9 green: image builds, container boots, `/healthz` + `/metrics` respond, container reports `healthy`, runs as non-root, image ≤ 800 MB budget, cold-start with bad DB URL still serves `/healthz`, dev/prod compose overlays validated, HEALTHCHECK pinned to `/healthz` (not `/readyz`).

**Hardening applied after review:**
- `--forwarded-allow-ips` tightened from `*` to `172.16.0.0/12,127.0.0.1` (only trust X-Forwarded-* from RFC1918 / loopback peers).
- `useradd --no-create-home --shell /usr/sbin/nologin` (no unused homedir, no interactive shell).
- HEALTHCHECK `start_period` bumped to 30 s — Arelle import + DB pool init cold-start on a small VM.
- `mem_limit: 2g` set at the top level alongside `deploy.resources.limits.memory` so plain `docker compose up` actually enforces the cap (the `deploy:` block alone is Swarm-only).
- Compose split into three files: base `docker-compose.yml` (services + volumes, no host ports), `docker-compose.override.yml` (auto-loaded for dev — exposes 5432 + 8000 + console logs), `docker-compose.prod.yml` (opt-in — no Postgres host port, app bound to `127.0.0.1:8000` for a reverse proxy on the host).
- `.env` auto-loaded by compose (`.env.example` ships placeholder credentials). No password committed to source.

**Multi-platform note:** the local `docker build` produces only the host arch. For an arm64 target (Hetzner CAX line):
```powershell
docker buildx create --name xie-builder --use --bootstrap
docker buildx build --platform linux/amd64,linux/arm64 -t xbrl-intelligence-engine:multi --push .
```

### Phase 7.5 — User-upload (Mode C)

Drag-and-drop any iXBRL filing or ESEF Taxonomy Package and the same Mode B pipeline that handles SEC + ESEF runs against it. The third ingestion lane next to Mode A (`source='companyfacts'`) and Mode B (`source='source_ixbrl'`).

- **`POST /uploads`** ([uploads.py](src/xie/api/endpoints/uploads.py)) — multipart endpoint. Hard 50 MB cap (413 on overflow). Sniffs the first 64 bytes:
  - `PK\x03\x04` → ESEF Taxonomy Package (unzipped by the Phase 6 unpacker, which still does the zip-slip + symlink-member guard from the [code review](#code-review--phase-6-esef-ingestion)).
  - `<?xml` / `<html` / `<!doctype` → raw iXBRL xHTML — parsed in place.
  - Anything else → **415 Unsupported Media Type**.
- **Per-IP sliding-window rate limit** ([rate_limit.py](src/xie/api/rate_limit.py)) — 10 uploads / hour / IP, with a `Retry-After` header on the 429. Pure in-process (defaultdict + deque); swap for slowapi+Redis when scaling out.
- **Synthetic filing identity** — `accession_no = 'upload-<sha256[:12]>'`, `regulator='UPLOAD'`, `entity_id='upload-<sha256[:12]>'`, `ingestion_mode='user_upload'`. CASCADE FKs already in place from Phase 1 means a single `DELETE FROM filings WHERE ...` cleans every dependent row.
- **Arelle SSRF guard** — `ArelleEnricher` now takes a `work_offline: bool` arg; `EnrichPipeline(work_offline=True)` flips `cntlr.webCache.workOffline=True` so a user-supplied `link:schemaRef` cannot trigger outbound HTTP from the server. Standard SEC/ESEF flow keeps `work_offline=False` for first-load taxonomy fetches.
- **TTL cleanup script** ([scripts/cleanup_uploads.py](scripts/cleanup_uploads.py)) — `python -m scripts.cleanup_uploads` deletes `UPLOAD` rows + `data/raw/uploads/{sha256}/` directories older than `XIE_UPLOAD_TTL_HOURS` (24 h default). Cron line in the script docstring.
- **UI** — [/uploads](http://127.0.0.1:8000/uploads) shows the drag-and-drop form, the configured cap / rate / TTL, and the 10 most-recent uploads. Successful POST → 303 to `/uploads/{sha256}` → 302 to `/filings/upload-{sha[:12]}/facts` (same template as SEC/ESEF filings).

**Deployment caveat — per-IP rate limit depends on `--forwarded-allow-ips`.** uvicorn replaces `request.client.host` with the `X-Forwarded-For` value only when the immediate peer IP is in the trusted CIDR (currently `172.16.0.0/12,127.0.0.1`). A reverse proxy on a different subnet would deliver every request as the proxy's IP, collapsing all clients into one bucket. Widen the CIDR — or set `--forwarded-allow-ips=<your-proxy-IP>` — when deploying behind a non-default network.

**Live demo verified:** uploaded the local AAPL FY2025 10-K primary iXBRL (`aapl-20250927.htm`, 1.52 MB) via curl:
- `curl -F file=@aapl-20250927.htm http://127.0.0.1:8000/uploads`
- → 303 redirect, `Location: /uploads/548ae59778cf08ee0f2ee088e7ece20d947076c3c01f74d2d65db4c2777e436a`
- → 302 redirect, `Location: /filings/upload-548ae59778cf/facts`
- → facts page renders with `regulator='UPLOAD'`, source_ixbrl facts from the same Mode B parse used for the database-sourced view
- `python -m scripts.cleanup_uploads` ran cleanly (0 rows ≥ 24 h old yet)

---

## Quickstart (local)

```powershell
# 1. Create venv
python -m venv venv

# 2. Install deps
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Run tests
$env:PYTHONPATH = "src"
# Tier 1 (unit, default) — 227 tests, < 15 s
.\venv\Scripts\python.exe -m pytest -v
# Tier 2 (integration_db) — 22 tests, requires host Postgres
$env:XIE_TEST_DB_URL = "postgresql+asyncpg://USER:PASS@127.0.0.1:5432/DBNAME"
.\venv\Scripts\python.exe -m pytest -m integration_db -v
# Tier 4 (docker) — 9 tests, requires Docker daemon
.\venv\Scripts\python.exe -m pytest -m docker -v
# All tiers (release gate)
.\venv\Scripts\python.exe -m pytest --override-ini="addopts=" -v

# 4. Run app (without Postgres — /healthz + /metrics work)
.\venv\Scripts\python.exe -m uvicorn xie.api.main:app --port 8000
# → http://127.0.0.1:8000/healthz
# → http://127.0.0.1:8000/metrics

# 5. Fetch a real SEC filing (no Postgres needed; blobs land in data/raw/)
.\venv\Scripts\python.exe -m xie.ingest.cli fetch --ticker AAPL --form 10-K --limit 1

# --- Phase 1.5 demo (Postgres required) ---

# 6. Bring up Postgres (Docker Compose OR your own host instance).
#    Set XIE_DATABASE_URL to point at it.
$env:XIE_DATABASE_URL = "postgresql+asyncpg://USER:PASS@127.0.0.1:5432/DBNAME"
# URL-encode special password chars (e.g. '@' -> '%40').

# 7. Apply migrations
$env:PYTHONPATH = "src"
.\venv\Scripts\python.exe -m alembic upgrade head

# 8. Mode A bootstrap: pulls all SEC-pre-parsed facts for a ticker.
.\venv\Scripts\python.exe -m xie.ingest.cli bootstrap-companyfacts --ticker AAPL

# 9. Serve the UI
.\venv\Scripts\python.exe -m uvicorn xie.api.main:app --port 8000
# → http://127.0.0.1:8000/companies/AAPL

# --- Phase 2 demo (Mode B + reconciliation) ---

# 10. Fetch the source iXBRL (writes blobs under data/raw/sec/...)
.\venv\Scripts\python.exe -m xie.ingest.cli fetch --ticker AAPL --form 10-K --limit 1

# 11. Mode B parse: extract every ix:nonFraction / ix:nonNumeric into the facts table
.\venv\Scripts\python.exe -m xie.ingest.cli parse-filing `
    --accession 0000320193-25-000079 `
    --primary "data\raw\sec\0000320193\000032019325000079\aapl-20250927.htm"

# 12. Diff Mode A (companyfacts) vs Mode B (source iXBRL) for that filing
.\venv\Scripts\python.exe -m xie.ingest.cli reconcile --accession 0000320193-25-000079

# 13. Browse the per-filing pages
# → http://127.0.0.1:8000/filings/0000320193-25-000079/facts
# → http://127.0.0.1:8000/filings/0000320193-25-000079/reconciliation

# --- Phase 3 demo (Arelle enrichment) ---

# 14. Load DTS via Arelle, persist concepts + linkbase arcs + DQC findings.
#    First run downloads ~1 GB of US-GAAP/DEI/SRT taxonomies into data/arelle_cache/
#    (~1-2 min). Subsequent runs reuse the cache (~10 s wall-clock).
.\venv\Scripts\python.exe -m xie.ingest.cli enrich-filing `
    --accession 0000320193-25-000079 `
    --primary "data\raw\sec\0000320193\000032019325000079\aapl-20250927.htm"

# 15. /filings/.../facts now shows standard labels + balance + period type next to qnames.

# --- Phase 4 demo (statement reconstruction + canonical projection) ---

# 16. Re-run canonical projection so Mode A + Mode B + calc-linkbase awareness merge.
.\venv\Scripts\python.exe -m xie.ingest.cli canonicalize --ticker AAPL

# 17. Browse filer-order primary statements (rendered from presentation linkbase).
# → http://127.0.0.1:8000/filings/0000320193-25-000079/statements
#    (lists every Apple-defined statement role; click into any to see the tree)

# 18. Type any unindexed ticker (e.g. NVDA) in the search bar -> /companies/NVDA
#    -> "Ingest now (Mode A on-demand)" button kicks off a background bootstrap.

# --- Phase 5 demo (restatement detection) ---

# 19. Detect cross-filing restatements (bitemporal diff with compare_at_precision).
.\venv\Scripts\python.exe -m xie.ingest.cli detect-restatements --ticker AAPL

# 20. Browse the restatements UI:
# → http://127.0.0.1:8000/companies/AAPL/restatements

# --- Phase 6 demo (ESEF / EU) ---

# 21. Ingest a French ESEF filing (Hermès International) end-to-end.
#     Pulls package ZIP (~12 MB) from filings.xbrl.org, unzips, parses iXBRL,
#     projects canonicals via the IFRS fallback chain.
.\venv\Scripts\python.exe -m xie.ingest.cli fetch-esef --lei 969500Y4IJGHJE2MTJ13

# 22. Browse the same UI by LEI:
# → http://127.0.0.1:8000/companies/969500Y4IJGHJE2MTJ13

# --- Phase 7 demo (container image) ---

# 23. (One-time) copy the example env file and edit secrets.
Copy-Item .env.example .env
# notepad .env  -> set POSTGRES_PASSWORD=...

# 24. Build the multi-stage image (566 MB final, non-root).
docker build -t xbrl-intelligence-engine:local .

# 25. DEV stack — Postgres + app exposed on host ports 5432 + 8000, console logs.
docker compose up -d
# (override.yml is auto-loaded by Compose)
# → http://127.0.0.1:8000/healthz

# 26. PROD stack — same images, but: Postgres has no host port, app bound to
#     127.0.0.1:8000 only (front with Caddy/Traefik on the host).
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 27. Confirm container health:
docker inspect --format "{{.State.Health.Status}}" (docker compose ps -q app)
# → "healthy" within ~30 s
# Logs are JSON-rendered structlog; volumes survive `docker compose down`.

# 28. Run the Docker smoke tests (build + run + probe + non-root check).
#     Skipped by default; opt in via marker.
.\venv\Scripts\python.exe -m pytest tests -m docker -v

# --- Phase 7.5 demo (user-upload Mode C) ---

# 29. Upload an iXBRL file via the drag-and-drop UI.
#     → http://127.0.0.1:8000/uploads

# 30. Or via curl:
curl -i -F "file=@data/raw/sec/0000320193/000032019325000079/aapl-20250927.htm" `
     http://127.0.0.1:8000/uploads
# → 303 See Other -> /uploads/{sha256} -> /filings/upload-{sha[:12]}/facts

# 31. Sweep expired uploads (cron line in scripts/cleanup_uploads.py docstring).
.\venv\Scripts\python.exe -m scripts.cleanup_uploads
```

## Quickstart (Docker — full stack with Postgres)

```powershell
docker compose up --build
# → http://localhost:8000/healthz
# → http://localhost:8000/readyz   (verifies DB connectivity)
# → http://localhost:8000/metrics
```

---

## Configuration

All settings env-prefixed `XIE_`. Override via shell or `.env` file.

| Variable | Default | Purpose |
|---|---|---|
| `XIE_APP_NAME` | `XBRL Intelligence Engine` | App title (OpenAPI) |
| `XIE_ENV` | `dev` | Environment label in logs |
| `XIE_LOG_LEVEL` | `INFO` | structlog filter level |
| `XIE_LOG_JSON` | `false` | `true` for JSON renderer (containers) |
| `XIE_DATABASE_URL` | `postgresql+asyncpg://xie:xie@localhost:5432/xie` | Async Postgres DSN |
| `XIE_SEC_USER_AGENT` | `XBRL-IE engineering.team@engineosol.com` | Required by SEC EDGAR fair-access policy |
| `XIE_ARELLE_CACHE_DIR` | `data/arelle_cache` | Where Arelle stores resolved taxonomy schemas (~1 GB after a fresh US-GAAP load) |
| `XIE_UPLOAD_MAX_BYTES` | `52428800` (50 MB) | Mode C user-upload size cap |
| `XIE_UPLOAD_MAX_UNZIPPED_BYTES` | `209715200` (200 MB) | Zip-bomb guard: cap on total decompressed ZIP payload |
| `XIE_UPLOAD_TTL_HOURS` | `24` | Mode C blob + DB row retention |
| `XIE_UPLOAD_RATE_LIMIT_PER_HOUR` | `10` | Per-IP upload throttle (sliding window) |

---

## Repository layout

```
src/xie/
  api/
    main.py              FastAPI app + lifespan
    endpoints/
      health.py          /healthz, /readyz
      metrics.py         /metrics + Prometheus collectors
      companies.py       /, /companies/{ticker_or_cik}, /companies/{ticker}/restatements, POST /companies/{ticker}/ingest
      filings.py         /filings/{accession}/facts + /reconciliation + /statements + /statements/{role_b64}
      uploads.py         GET /uploads, POST /uploads, GET /uploads/{sha256}
    rate_limit.py        SlidingWindowLimiter (per-IP, in-process)
    templates/
      base.html          dark-themed shell, htmx loaded
      index.html         landing page
      company.html       canonical 5-yr time-series + on-demand ingest button
      filing_facts.html  per-filing fact browser (source/hidden filters + DQC summary)
      filing_reconciliation.html  A-vs-B mismatch summary + top 500 findings
      filing_statements.html      lists filer-defined statement roles
      filing_statement.html       reconstructed primary statement (filer order)
      company_restatements.html   cross-filing restatement table (top 500)
      upload.html        drag-drop form + 10 most-recent uploads
  core/
    config.py            Settings (pydantic-settings, XIE_ prefix)
    logging.py           structlog config (console / JSON)
  db/
    base.py              DeclarativeBase
    models.py            ORM models (filings, contexts, units, facts, canonical_line_items)
    session.py           Async engine + get_session dependency
  ingest/
    cli.py               Typer CLI: fetch, bootstrap-companyfacts
    sec_pipeline.py      SECIngestPipeline (Mode B foundation: discover -> download -> upsert)
    companyfacts_pipeline.py  CompanyFactsPipeline (Mode A: filings + facts + canonical)
    modeb_pipeline.py    ModeBPipeline (parse downloaded primary iXBRL -> facts + dims)
    reconcile.py         Reconciler (Mode A vs Mode B fact_reconciliation diff)
    enrich_pipeline.py   EnrichPipeline (Arelle DTS load -> concepts + arcs + roles + dqc_findings)
    esef_pipeline.py     ESEFIngestPipeline (filings.xbrl.org discovery -> unzip -> Mode B + IFRS canonical)
    upload_pipeline.py   UploadPipeline (Mode C: sniff -> persist blob -> Mode B parse, synthetic UPLOAD filing)
scripts/
  cleanup_uploads.py     TTL sweep for Mode C blobs + filings rows (run hourly via cron)
  regulators/
    sec/
      client.py          SECClient: UA + 10 req/s + tenacity retries
      tickers.py         TickerResolver (ticker -> CIK10)
      discovery.py       SubmissionsDiscovery (paginated)
      filing_index.py    FilingIndexFetcher (index.json + sha256 manifest)
      companyfacts.py    CompanyFactsClient + parse_companyfacts(payload)
    esef/
      client.py          xbrl-filings-api wrapper (filings.xbrl.org JSON:API)
      taxonomy_package.py  unzip REC 2016-04-19 package -> primary iXBRL + extension XSD
  xbrl/
    canonical_map.py     SEC US-GAAP fallback chains (Canonical enum)
    transforms.py        iXBRL Transform Registry numeric/date functions (TR3/TR4/TR5)
    registry_dispatcher.py  namespace URI -> TRVersion
    ixbrl_parser.py      lxml-hardened extractor (contexts/units/facts/hidden/continuation/DTS)
    arelle_worker.py     ArelleEnricher (Cntlr wrapper: concept metadata + linkbase relationships + roles + findings)
    canonical_project.py project_canonical_for_entity (calc-linkbase-aware; Mode A + B merge)
    linkbase.py          presentation tree builder + statement reconstruction
    restatement.py       cross-filing SCD-Type-2 detector + compare_at_precision
alembic/
  env.py
  versions/
    20260523_0001_create_filings.py
    20260523_0002_create_facts_and_canonical.py
    20260523_0003_fact_dimensions_and_reconciliation.py
    20260524_0004_concepts_arcs_dqc.py
    20260524_0005_role_definitions.py
    20260524_0006_restatements.py
tests/                            # 258 tests total: 227 unit + 22 integration_db + 9 docker
  # Unit tier (default, no marker)
  test_arelle_worker.py            qname helpers, is_extension, to_thread cancellation, raise propagation
  test_arelle_worker_offline.py    work_offline default + override flips webCache.workOffline
  test_canonical_map.py            fallback chain + reverse lookup + Decimal(4,3) confidence round-trip
  test_canonical_project.py        projector composition unit slice
  test_companies_routing.py        LEI shape detection, 5-period cap, 500-row truncate
  test_companyfacts_parser.py      duration/instant/null-value parse
  test_concept_search.py           concept search router
  test_dimensions_units.py         fact_dimensions + units persistence
  test_esef_filings.py             ESEF filings.xbrl.org JSON:API client
  test_esef_smoke.py               ESEF discovery smoke
  test_filings_facts_router.py     /filings + /facts endpoints
  test_health.py                   /healthz live, /readyz DB-down 5xx, /metrics content-type
  test_html_response.py            template render smoke
  test_ingest_bg.py                background ingest swallow-and-log
  test_ingest_pipeline.py          ModeA + ModeB pipeline unit paths
  test_inline_xbrl.py              inline XBRL extraction
  test_ixbrl_parser.py             nil, INF, scale+sign, nested continuations, multi-ns, wider-narrower, hidden, dimensional
  test_metrics.py                  prometheus counter emit
  test_modeb_pipeline.py           source_ixbrl pipeline + idempotence
  test_observability.py            structlog binding
  test_property.py                 hypothesis: transforms round-trip + compare_at_precision invariants + ZIP fuzz
  test_rate_limit.py               sliding window: limit / blocks / per-key / window-slides + natural aging
  test_restatement.py              compare_at_precision (INF, ROUND_HALF_UP, neg symmetry, Unicode minus)
  test_restatement_router.py       /restatements endpoint
  test_sec_client.py               SECClient retry + UA gate
  test_taxonomy_package.py         ESEF package ZIP unzip + manifest parse
  test_transforms.py               TR3/TR4/TR5 numeric formats + scale/sign + registry coverage + locale pinning
  test_upload_pipeline_*.py        Mode C pipeline branches
  test_upload_router.py            /uploads 415 / 413 / 429 / bad-sha 400 / malformed-zip 4xx
  test_upload_sniff.py             ZIP magic vs xHTML vs unsupported bytes + nested ZIP reject

  # Integration-DB tier (-m integration_db; requires $env:XIE_TEST_DB_URL)
  test_integration_db_smoke.py     test_schema / db_session / async_client fixtures
  test_restatement_db.py           NULL-context COALESCE, filing.id tie-break
  test_canonical_project_db.py     source preference, calc-linkbase boost, idempotence
  test_models_fk.py                FK RESTRICT, NULL-in-UNIQUE quirk
  test_e2e_pipeline.py             two-filing synthetic ingest → projection → restatement → page render
  test_aapl_e2e.py                 AAPL canonical-count snapshot regression guard

  # Docker tier (-m docker)
  test_docker_smoke.py             build + run + healthz + non-root + image-size + cold-start + compose overlays + HEALTHCHECK pin

  fixtures/                        tiny_ixbrl.htm + 8 iXBRL specimen htms + e2e pair + aapl_companyfacts_small.json + snapshots/
data/raw/sec/...         Persisted iXBRL blobs (gitignored)
Dockerfile               python:3.12-slim image (runs uvicorn)
docker-compose.yml       app + postgres:16-alpine + pgdata volume
requirements.txt         Runtime + dev deps
pytest.ini               pythonpath=src, asyncio_mode=auto
alembic.ini              Alembic config
XBRL_PLAN.md             Full 15.5-weekend implementation plan
```

---

## Stack

| Layer | Choice |
|---|---|
| Web | FastAPI 0.115 + Uvicorn (standard) |
| ORM | SQLAlchemy 2.0 async + asyncpg |
| Migrations | Alembic (async env) |
| Config | pydantic-settings |
| Logging | structlog |
| Metrics | prometheus_client |
| HTTP client | httpx (async) + aiolimiter (10 req/s) + tenacity (exp backoff) |
| iXBRL parser | lxml 5.3 (hardened: no_network, no entities, no huge_tree) |
| Taxonomy / linkbase processor | Arelle 2.41 (in-process, cached DTS under data/arelle_cache) |
| ESEF discovery | xbrl-filings-api 1.0 (wraps filings.xbrl.org JSON:API) |
| CLI | Typer 0.12 (pinned click 8.1.7) + Rich |
| Templates | Jinja2 3.1 + htmx 2.0 (CDN) |
| DB | Postgres 16 |
| Tests | pytest + pytest-asyncio + httpx MockTransport + pytest-recording + hypothesis (property tests) |
| Container | python:3.12-slim (multi-stage, non-root, 566 MB) |
| Runtime (local) | Python 3.13 (venv) |

> Note: Plan targets Python 3.12; local venv is 3.13 because 3.12 wasn't installed. Container image still uses 3.12.

---

## Roadmap

See [XBRL_PLAN.md](XBRL_PLAN.md) for the full 8-phase plan. Phases:

| Phase | Scope | Status |
|---|---|---|
| **0** — Repo scaffold | FastAPI + Postgres + Alembic + observability | **Done** |
| **1** — SEC EDGAR client (Mode B foundation) + raw filing store | EdgarClient, filings table, blob store, CLI | **Done** |
| **1.5** — SEC companyfacts bootstrap (Mode A) | companyfacts.py, /companies/{ticker} backed by Mode A | **Done** |
| **2** — lxml fact extractor (Mode B fidelity) | TR3/TR4/TR5 transforms, facts table, Mode A vs B reconciliation | **Done** |
| **3** — Arelle sidecar + linkbases + DQC | Long-lived Arelle worker, concept arcs, DQC findings | **Done** |
| **4** — Canonical projection + statement reconstruction | canonical_map.py, on-demand ingestion fallback | **Done** |
| **5** — Restatement detection | Bitemporal SCD-Type-2 diff | **Done** |
| **6** — ESEF ingestion | xbrl-filings-api + wider-narrower anchor resolution | **Done** |
| **7** — Deploy + benchmark CI + polish | Multi-stage Dockerfile + compose volumes **(done)**; Hetzner deploy + accuracy gate (FSDS + Frames parity) | partial |
| **7.5** — User-upload (Mode C) | POST /uploads with SSRF guard + TTL | **Done** |

---

## License

TBD.
