# Experiment analysis tool

A self-contained (standard-library only) analysis workbench for online
experiments. It handles fixed and stratified randomisation, cross-device users,
mid-experiment switching, and computes **ITT** next to **as-treated** (and
per-protocol / raw-assigned) estimates on the same stream of facts. Every
pause, rollback or metric-definition change opens a **version** with a frozen
**sample snapshot**, contamination is excluded with a written reason, and a
verification pass detects repeated exposure and late-arriving results.

```
python3 cli.py serve            # http://127.0.0.1:8787
python3 -m unittest discover -s tests
node tests/render_smoke.js      # optional UI smoke test (needs jsdom, see below)
```

No third-party Python dependencies. Data lives in SQLite (`EXP_DB`, default
`./experiment_analysis.sqlite3`).

## Concepts

| Concept | Meaning |
|---|---|
| **Experiment** | Immutable identity + metrics + allocation method. |
| **Version** | A config snapshot plus a *sample window*. `kind` is `initial`, `config_change`, `pause`, `resume`, `rollback` or `metric_change`. |
| **Sample window** (`window_start`/`window_end`) | The assignments/events a version is evaluated on. Separate from the lifecycle timestamps (`effective_from`/`effective_to`). |
| **Snapshot** | The exact assignment and event ids analysed, with SHA-256 hashes, the exclusion ledger and the metric definitions. |
| **Exclusion ledger** | Every dropped unit/event with a `rule` and a human-written `reason`. |

### Populations

| Population | Arm | Sample |
|---|---|---|
| `assigned` | initial assignment | everyone in the window, **no** exclusions — the audit baseline |
| `intention_to_treat` | initial assignment | eligible units; all their outcomes stay in the initial arm |
| `as_treated` | arm of the assignment that emitted the event | units with an exposure event; crossovers contribute to both arms |
| `per_protocol` | initial assignment | exposed units that never crossed over |

The populations differ only in how switching and missing exposure are
treated — that is where ITT and as-treated diverge.

### Version semantics

| Kind | Sample window | Status |
|---|---|---|
| initial | opens at `effective_from` | active |
| config_change | inherits the running window, stays open | active |
| metric_change | inherits the start, stays **open** (replays the whole experiment under the new definition) | active |
| pause | closes at the transition instant; the paused version inherits that frozen window | paused |
| resume | opens fresh from the resume instant | active |
| rollback | replays the **target version's frozen window** and config | rolled_back |

A pause/rollback *closes the sample* of the version it supersedes. Config and
metric changes do not — they re-evaluate the same running sample under new
rules.

## Exclusion rules (all recorded with a reason)

- `external_contamination` — listed in `config.rules.contaminated_external_ids` (user, assignment or device id) with a custom reason from `contaminated_reasons`.
- `forced_assignment` — deterministic override, not randomised.
- `variant_conflict` — multiple arms while `allow_switch` is false.
- `pre_exposure_outcome` — outcome before the unit's first assignment.
- `event_before_assignment` — event timestamp precedes its assignment.
- `assignment_outside_window` — event belongs to a different version.
- `duplicate_event` — identical payload replayed under a new event id.
- `metric_definition_changed` — recorded under an outdated metric version.
- `outside_version_window` — occurred or was received outside the window.

## Verification

`GET /api/experiments/:id/verify?version=:vid` recomputes the version **live**
(including late results) and diffs it against the frozen snapshot:
new/removed event and assignment ids, hash agreement, per-metric estimate
shift, and ingestion lag (median/p95/max from `occurred_at` to `received_at`).

A version that is still `active` gets a snapshot refreshed on each recompute —
the report says so explicitly, because "clean" there only means the stored ids
agree with current data, not that an independent record was preserved.

## HTTP API

```
GET  /api/health
GET  /api/experiments
POST /api/experiments                  {id, name, metrics[], allocation_method, ...}
POST /api/demo                         one-click stratified demo with 3 versions
GET  /api/experiments/:id              detail + versions + snapshots + audit log
GET  /api/experiments/:id/analysis?version=&live=1
GET  /api/experiments/:id/compare?left=&right=&live=
GET  /api/experiments/:id/verify?version=
GET  /api/experiments/:id/snapshots
GET  /api/experiments/:id/exclusions
GET  /api/experiments/:id/audit
GET  /api/experiments/:id/export
POST /api/experiments/:id/versions     {kind, reason, config, rollback_to, effective_from}
POST /api/experiments/:id/recompute
POST /api/experiments/:id/simulate     {n_units, seed}
POST /api/experiments/:id/late-results {count}  backfill inside-window late results
POST /api/experiments/:id/assignments  {assignments: [...]}
POST /api/experiments/:id/events       {events: [...]}
POST /import                           bundle from /export
```

Ingested events accept an optional top-level `received_at`; when absent it
defaults to `occurred_at` so frozen views remain reproducible. Ingestion is
idempotent by `assignment_id` / `event_id` (duplicates are counted, not
inserted).

`/import` restores a bundle **verbatim** — stored version configs, frozen
snapshots and the exclusion ledger — instead of replaying the lifecycle, which
would re-derive older versions from the experiment's *final* metric
definitions. The response carries `import_check`, comparing every restored
snapshot hash with a fresh recompute (`match` / `mismatch`). Because assignment
and event ids are global primary keys, importing a bundle whose facts already
exist in the target store is rejected before anything is written; import into
an empty store.

## UI

A single-page, no-build UI (`static/`) behind the same server:

- **Overview** — sample funnel, ITT forest plot, verification summary.
- **Allocation balance** — SRM χ², per-stratum arm split, covariate SMDs, deterministic re-allocation check.
- **Sample loss** — exposure rates per arm, differential attrition callout, the list of lost units, population comparison.
- **Metric differences** — population switcher, forest plot, estimate table, ITT vs as-treated gap.
- **Versions & snapshots** — lifecycle actions (pause/resume/metric change/rollback/config), version comparison with warnings, snapshot ledger with hashes, verify button.
- **Verification** — every check card, the exclusion ledger (rule → reason → examples), late/out-of-order results, ingestion diagnostics.

Charts are hand-rolled inline SVG: 2px lines, ≥8px markers with a surface ring,
`tabular-nums` only in aligned columns, one categorical pair
(blue = control, orange = treatment) validated for light and dark surfaces.
Dark mode follows the OS and has a manual toggle.

## Tests

`tests/test_engine.py` covers the statistics (z/t quantiles against known
values), ITT vs as-treated attribution, per-protocol crossover removal,
switching-disallowed exclusion, contamination reasons, stratified balance,
pause-freeze + late-result drift, clean verification of a frozen snapshot,
rollback window replay, metric-version exclusion, order-independent
export/import round-trip (frozen hashes preserved, duplicate fact ids
rejected), and version comparison warnings.

`tests/render_smoke.js` loads the real static bundle in jsdom against a running
server and clicks through every tab (including the version comparison and the
snapshot verify button on every version row), failing on any page error:

```
npm_config_cache=/tmp/npmcache npm install jsdom --no-save --prefix /tmp/jsdomtest
python3 cli.py serve --port 8787 &
node tests/render_smoke.js
```
