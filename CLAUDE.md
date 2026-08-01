# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable + dev deps)
pip install -e ".[dev]"

# Full test suite
python -m pytest -q --tb=short

# Single test file or case
python -m pytest tests/test_agent_loop.py -q
python -m pytest tests/test_agent_loop.py::test_function_name -q

# AGENTS mirror tests (must use importlib mode)
python -m pytest -q --import-mode=importlib Main/RepoTermFrontline/Test/ Package/EngineeringStructure/Test/

# Lint (ruff: pycodestyle E + pyflakes F only, E501 ignored)
python -m ruff check repoterm/ --select=E,F --ignore=E501

# Type check (mypy, baseline ≤225 errors)
python -m mypy repoterm/

# Structure compliance gate
python -m repoterm.structure_check --root . --hotspots 5 --max-dependency-upstream 4 --check-material-inventory --report .temp/structure-compliance.json

# Runtime readiness gate
python -m repoterm.readiness --json

# Compile check (catches syntax errors across all source trees)
python -m compileall -q repoterm tests benchmarks Main Package

# Run in mock mode (no provider needed)
REPOTERM_MODEL_MODE=mock repoterm

# Benchmark evaluations
python benchmarks/runtime_regression_eval.py --rounds 3
python benchmarks/llm_e2e_eval.py --all --runs 3 --confirm-live
```

## Architecture

Repoterm is a terminal AI coding agent with **zero runtime dependencies** — all provider HTTP calls use stdlib `urllib`.

**Core loop:** `repoterm/agent_loop.py` — `run_agent_turn()` is the central orchestration point, importing and coordinating every subsystem (turn kernel, model, tools, context, memory, cybernetics, hooks, cost).

**Turn state machine:** `repoterm/turn_kernel.py` — routes work through `explore → execute → verify` phases. Tracks task-critical state in `StableTaskPack`. Stop reasons: `done`, `max_steps`, `await_user`, `blocked`, `verification_failed`, `widen_needed`.

**Tool subsystem:** `repoterm/tooling.py` — `ToolRegistry` with a validate→dispatch→normalize→truncate pipeline. Failures are normalized into `ToolResult`. 26 core tools in `repoterm/tools/`, profile-gated via `REPOTERM_TOOL_PROFILE`. Safe writes go through: diff preview → permission decision → checkpoint → write.

**Context governance:** `repoterm/context_manager.py` handles provider usage + local token estimation. `repoterm/micro_compact.py` (MicroCompactor) compacts old tool output; `repoterm/context_compactor.py` summarizes history under pressure. `repoterm/circuit_breaker.py` provides a compaction circuit breaker.

**Memory subsystem:** `repoterm/memory.py` (MemoryManager with scopes/categories/entries), `repoterm/timeline_memory.py`, `repoterm/vector_memory.py`, plus curator/injector/reranker pipeline modules. Theory doc: `Docs/Documentation/memory_theory.md`.

**Cybernetics layer:** Control-theory modules layered on top of the agent loop — `repoterm/cybernetic_orchestrator.py`, feedback/predictive/adaptive controllers, self-healing engine, stability monitor, state observer, task graph. These provide agent oversight and adaptive control.

**Session persistence:** `repoterm/session.py` — snapshot + delta model with autosave, checkpoints, rewind, replay, and resume.

**Model adapters:** `repoterm/anthropic_adapter.py`, `repoterm/openai_adapter.py`, `repoterm/mock_model.py` — raw HTTP adapters over urllib (no SDK deps). `repoterm/model_registry.py` selects adapters and supports risk/cost-adaptive model switching.

**TUI:** `repoterm/tui/` — 19 modules for the terminal UI (screen, renderer, input, transcript, markdown, theme, etc.).

## AGENTS Engineering Conventions

This repo follows an engineering structure spec defined in `AGENTS.md` (Chinese, 106KB). Key rules:

- **Repo uses "product project root profile":** `Main/` and `Package/` are contract/projection modules that describe and validate the runtime in `repoterm/`. They are **not** the runtime itself.
- `Main/RepoTermFrontline/` — entry-surface contracts; `Package/EngineeringStructure/` — structure compliance scanner.
- **Main modules cannot directly depend on each other.** Shared capability must live in Package modules.
- **Never introduce stubs, mocks, skips, or workarounds to pass checks.**
- **Never use destructive git commands** (`reset --hard`, `checkout --`, etc.) unless explicitly asked.
- Mirror tests under `Main/*/Test/` and `Package/*/Test/` use `.Test.py` naming and run with `--import-mode=importlib`.
- New source files in `Main/` or `Package/` must follow the AGENTS directory conventions (`Src/Application/...` with `Test/` mirror).

## Entry Points

| Command | Module | Purpose |
|---|---|---|
| `repoterm` | `repoterm.main:main` | Interactive TUI/CLI |
| `repoterm-headless` | `repoterm.headless:main` | One-shot non-interactive run |
| `repoterm-readiness` | `repoterm.readiness:main` | Provider/runtime health gate |
| `repoterm-structure-check` | `repoterm.structure_check:main` | AGENTS structure compliance gate |

## CI Pipeline Order

Pre-push validation runs in this sequence (`.github/workflows/ci.yml`):
1. `compileall` — syntax check across all source trees
2. Structure compliance — `repoterm-structure-check`
3. Readiness gate — `repoterm.readiness --json`
4. Ruff lint — `E` + `F` rules, `E501` ignored
5. Mypy — fails if errors exceed the 225 baseline
6. AGENTS mirror tests — `--import-mode=importlib`
7. Full pytest suite — `python -m pytest -q --tb=short`
