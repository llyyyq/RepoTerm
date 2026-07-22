# RepoTerm App Projection

Status: active engineering inventory
Audited at: 2026-06-29
Repository baseline: current RepoTerm worktree after the product-name migration.

This document maps the current RepoTerm workspace into the engineering object
model from `AGENTS.md`. It records observable app facts before any large
directory migration. It is intentionally conservative: directories listed as
materials are not deletion candidates until their entry coverage, replacement
target, and retirement condition are satisfied.

The machine-readable counterpart of this inventory lives at
`Docs/Documentation/engineering/material-inventory.json`. When a historical alias still
appears in older docs, the inventory records both the current on-disk root and
the alias rather than pretending the old path still exists.

## Current Product App

Logical product app: `product/app/repoterm_frontline`

Canonical AGENTS projection: `Main/RepoTermFrontline/`

Current implementation root: `repoterm/`

Reasoning:

- The user has confirmed that `repoterm/` is the current app implementation.
- `Main/RepoTermFrontline/Src/Application/Entry/RepoTermFrontline.py` now
  records the product app's observable entry contract without importing the
  legacy implementation root.
- `Main/RepoTermFrontline/Src/Application/Entry/LocalCommandSurface.py` now
  owns the local slash-command contract. `repoterm/cli_commands.py` imports
  this contract and still owns the temporary command handling implementation.
- `Main/RepoTermFrontline/Src/Application/Entry/RuntimeLifecycleSurface.py`
  now owns the runtime lifecycle entry contract. `pyproject.toml` console
  scripts are tested against this contract so package entry points cannot drift
  away from the Main module projection.
- `Main/RepoTermFrontline/Src/Application/Query/CurrentRuntimeProjection.py`
  checks that the current `repoterm/` implementation still provides the entry
  evidence named by that contract.
- `Main/RepoTermFrontline/Src/Application/Query/RuntimeCapabilityInventory.py`
  classifies the current `repoterm/` implementation into capability slices
  before any file migration. The first tracked slices cover lifecycle entries,
  command surface, session/rewind state, provider configuration, observability,
  tool orchestration, and research-tool residue.
- `pyproject.toml` exposes stable product entry surfaces:
  `repoterm = "repoterm.main:main"`,
  `repoterm-headless = "repoterm.headless:main"`, and
  `repoterm-readiness = "repoterm.readiness:main"`.
- `repoterm/main.py` owns the interactive CLI/TUI lifecycle and wires model,
  tools, permissions, session inspection, replay, checkpoints, and rewind.
- `repoterm/headless.py` owns a non-interactive one-shot lifecycle for CI,
  automation, and scripted evaluation.
- `repoterm/product_surfaces.py` exposes product state surfaces for
  instructions, hooks, delegation, extensions, provider readiness, and runtime
  snapshot reporting.

The AGENTS `Main/RepoTermFrontline` module now exists as the product app
projection boundary. Runtime code still executes from `repoterm/`; this keeps
the migration incremental while giving the product object an auditable module
identity and mirrored test evidence.

## Entry Surfaces

| entry surface | current point | observable result | app role |
| --- | --- | --- | --- |
| Interactive CLI/TUI | `repoterm`, `python -m repoterm.main` | terminal coding session with tools, permissions, model runtime, transcript, session commands | product app lifecycle entry |
| Headless runner | `repoterm-headless`, `python -m repoterm.headless` | single prompt execution with optional `REPOTERM_HEADLESS_MESSAGES_OUT` trace | product app automation entry |
| Local command surface | `repoterm/cli_commands.py` | `/session`, `/session-replay`, `/sessions`, `/checkpoints`, `/rewind`, `/readiness`, `/extensions` | product app operation surface |
| Product snapshot | `repoterm/product_surfaces.py` | instruction, hook, delegation, extension, readiness, and prompt bundle summaries | product app observability surface |
| Standalone readiness gate | `repoterm-readiness`, `python -m repoterm.readiness --json --fail-on blocked` | machine-readable runtime/provider readiness with explicit blocked/warning threshold behavior | product app quality gate entry |

## Lifecycle Projection

Configuration:

- Runtime configuration is loaded through `repoterm.config.load_runtime_config`.
- Provider readiness is observed through `collect_readiness_report` and exposed
  through `/readiness`.
- Extension, user profile, and managed policy paths are surfaced through
  `product_surfaces.py`.

Startup:

- Interactive startup enters through `repoterm.main:main`.
- Headless startup enters through `repoterm.headless:main`.

State, data, and logs:

- Memory/state material currently appears in `.repoterm-memory/`,
  `.repoterm-memory-local/`, `.repoterm-session-memory/`, and
  `.workbuddy/memory/`.
- Session and rewind behavior is represented in `repoterm/session.py`,
  `repoterm/cli_commands.py`, and related tests.
- AgentOps benchmark and trace artifacts currently appear under `benchmarks/` and
  `outputs/`.

Observation and health:

- `/readiness` reports provider and fallback readiness.
- `repoterm-readiness --json --fail-on blocked` exposes the same readiness
  facts as a standalone gate suitable for local checks and CI automation.
- `repoterm-readiness --examples-out <path>` exports read-only fallback
  configuration examples as an artifact without mutating user settings.
- `repoterm-readiness --doctor-out <path>` exports a read-only Markdown
  readiness repair report for local diagnostics.
- `/session`, `/session-replay`, `/sessions`, and `/checkpoints` expose durable
  session state.
- `tests/test_cli_commands.py` verifies the product command surfaces.

## Material Inventory

| material | current identity | observed entry/value | coverage status | retirement condition |
| --- | --- | --- | --- | --- |
| `ts-src/py-src/` (`py-src/` historical alias) | archived Python source material | removed legacy package mirror | deleted after module-level burn-down confirmed no current caller | restore from Git history only if a current product caller and focused tests are added |
| `ts-src/` | archived TypeScript reference | removed TypeScript source, docs and launchers | deletion completed; no current product app ownership | restore from Git history only for a documented comparison need |
| `RepoTerm-fork/` | comparison material | forked source/docs tree plus nested external RepoTerm copy | archive approved; no current product or test caller | physical deletion can proceed after inventory gates pass and dirty-worktree handling is explicit |
| `RepoTerm-main-work/` | comparison/material workspace | current-looking docs/site, node workspace, TS tests, and nested external copy | archive approved; parity provenance migrated; no product runtime caller | physical deletion can proceed after inventory gates pass and dirty-worktree handling is explicit |
| `claude-code-src/` | fuel/reference vendor | Claude Code comparison/reference source | no product ownership | only documented comparison value remains, or reference is replaced by narrower docs |
| `superpowers-zh/` | support/fuel vendor | local Superpowers Chinese materials and skills | support material | stable support entry is documented, or copied ability is no longer needed |
| `.dead-modules-backup/` | retired-code evidence | backup for removed modules such as gateway, cron runner, protocol, safe execution | deletion blocked by audit value | removed modules stay skipped/covered, and owner approves final archival/deletion |
| `experiments/` (`paper_experiments/` historical alias) | archived research material | obsolete paper transcripts removed from the product repository | current AgentOps evidence replaces the old paper probe | restore externally only if the research line resumes |
| `outputs/` | generated evidence | local runtime artifacts | ignored and regenerated by current gates | never treat generated output as product source |

## Migration Already Done

- Root package entry points now resolve to `repoterm.main` and
  `repoterm.headless`.
- `Main/RepoTermFrontline` now exists as the canonical AGENTS Main module for
  the product app entry contract, with a mirrored `.Test.py` file.
- Local slash-command metadata has moved from `repoterm/cli_commands.py` into
  the Main module's Entry contract; the handler remains in `repoterm/` until a
  later Usecase/Boot migration closes the executable path.
- Runtime lifecycle entry metadata for `repoterm` and `repoterm-headless`
  now lives under the Main module's Entry contract; the executable targets still
  point to `repoterm.main:main` and `repoterm.headless:main`.
- `Main/RepoTermFrontline` now also exposes a pure query projection of the
  current runtime root, so the Main module can verify its legacy implementation
  evidence before any implementation files are moved.
- `Main/RepoTermFrontline` now carries a runtime capability inventory. Its next
  migration candidates are `repoterm/main.py`, `repoterm/headless.py`, and
  `repoterm/cli_commands.py`, because they are the product lifecycle and
  operation entry surfaces.
- Product surfaces for memory/session/rewind/readiness are present in current
  Python code and local command tests.
- Standalone readiness gate support is present through `repoterm/readiness.py`
  and the `repoterm-readiness` console script. CI treats blocked readiness as a
  local gate failure while preserving provider warning evidence.
- Gateway and cron runner tests are explicitly skipped as removed dead code,
  with backups retained under `.dead-modules-backup/`.
- README/product homepage assets exist under `Docs/Documentation/assets/readme/`.

## Migration Still Open

- Runtime implementation files have not moved from `repoterm/` into the
  canonical Main module; the Main module currently carries the entry contract
  plus local command contract, runtime-evidence, and capability-inventory
  queries, not the executable implementation.
- Runtime support objects are still implicit in config/provider/tool setup
  rather than modeled under `runtime/`.
- AGENTS product-root profile scanning now has a canonical pure query module
  at `Package/EngineeringStructure/Src/Application/Query/ProductRootProjection.py`.
  `repoterm.engineering_structure` remains a compatibility surface for the
  current product package. The mirrored structure test is
  `Package/EngineeringStructure/Test/Application/Query/ProductRootProjection.Test.py`.
  The scanner now recognizes role spaces, Package/Main module candidates,
  module direct reserved items, `Src` source files, and exact `Test` mirrors.
  The root documentation workspace has been renamed to canonical `Docs/Documentation/`, so it
  is recognized as a legal project-level embedded workspace. The newly added
  `Package/EngineeringStructure` module closes its own source/test mirror.
- AGENTS compliance checking now has a tool entry at
  `python -m repoterm.structure_check --root . --report .temp/structure-compliance.json`.
  It combines directory/file structure findings with the first Python
  dependency-boundary check for AGENTS modules, including Application
  child-section import rules and direct cross-module source-import violations.
  The dependency check resolves both absolute imports and relative imports,
  and the JSON report records original imports, resolved imports, import style,
  target area, and whether each edge is allowed.
  `Src/Import/` files are now recognized as Import file entities and checked
  for basic encoded stem shape and duplicate stem conflicts inside one module.
  The CLI can now print impact hotspots with `--hotspots N`, and can enforce
  gate thresholds with `--max-dependency-upstream N` and
  `--max-import-upstream N`. These thresholds turn dependency concentration and
  module import impact into explicit failure exits while preserving the full
  JSON evidence payload.
  The report path lives under `.temp/`, which is ignored and excluded from
  root structure scanning.
- Runtime/provider readiness now has a standalone gate entry at
  `python -m repoterm.readiness --json --fail-on blocked`. This keeps provider
  warnings visible without conflating external channel availability with local
  product and structure gate failures.
  The same tool can export fallback configuration examples with
  `--examples-out`, keeping the repair path visible while avoiding automatic
  credential writes. It can also export a Markdown doctor report with
  `--doctor-out`, which packages issues, next actions, and safe config examples
  into a local diagnostic artifact.
- Material inventory records the completed removal of `ts-src/py-src/`,
  `ts-src/`, obsolete paper experiments, and generated outputs.
- `ts-src/py-src/` module-level burn-down is closed: 11 legacy-only modules are
  retired and current-code name residues are cleared.
- `ts-src/` reference material has been removed after product-facing links
  moved to current docs; historical `CODE_WIKI.md` references remain explicitly
  comparison-only.
- `RepoTerm-fork/` now has a comparison-material burn-down manifest; deletion
  is now an owner/archive decision because `Docs/Documentation/CODE_WIKI.md` references are
  explicitly historical. Archive approval is recorded, but the directory
  remains in place.
- `RepoTerm-main-work/` now has a parity-source burn-down manifest; direct test
  provenance moved to `Docs/Documentation/engineering/ts-parity-provenance.json`, so deletion
  is now an owner/archive decision rather than a pytest path dependency.
  Archive approval is recorded, but the directory remains in place.
- Obsolete Paper A experiments, benchmark wrappers and generated outputs were
  removed. Current evaluation evidence is owned by the deterministic Runtime
  regression and live-model AgentOps reports under `benchmarks/`.

## Next Minimal Closed Loop

The next closed loop should be:

1. Keep `repoterm/` as the active product app source root.
2. Treat `Docs/Documentation/engineering/material-inventory.json` as the single source of
   truth for current material roots and historical aliases.
3. Continue by moving from inventory to action: migrate or explicitly retain
   the remaining Docs/Documentation/test links that block deletion of comparison materials.
4. Run focused gates after every inventory update:
   `python -m compileall -q repoterm tests benchmarks Main Package`,
   `python -m repoterm.structure_check --root . --hotspots 5 --max-dependency-upstream 4 --report .temp/structure-compliance.json`,
   `python -m repoterm.readiness --json --fail-on blocked`,
   `python -m repoterm.readiness --examples-out .temp/readiness-fallback-examples.json --fail-on blocked`,
   `python -m repoterm.readiness --doctor-out .temp/readiness-doctor.md --fail-on blocked`,
   `python -m pytest -q --import-mode=importlib Main/RepoTermFrontline/Test/Application/Dto/AppProjection.Test.py Main/RepoTermFrontline/Test/Application/Entry/RepoTermFrontline.Test.py Main/RepoTermFrontline/Test/Application/Entry/LocalCommandSurface.Test.py Main/RepoTermFrontline/Test/Application/Entry/RuntimeLifecycleSurface.Test.py Main/RepoTermFrontline/Test/Application/Query/CurrentRuntimeProjection.Test.py Main/RepoTermFrontline/Test/Application/Query/RuntimeCapabilityInventory.Test.py Package/EngineeringStructure/Test/Application/Query/ProductRootProjection.Test.py Package/EngineeringStructure/Test/Application/Query/StructureCompliance.Test.py tests/test_cli_commands.py tests/test_engineering_inventory.py tests/test_engineering_structure.py`.

This closes the current handoff without pretending that old materials are
already migrated. It also gives the next migration round a safe burn-down map.
