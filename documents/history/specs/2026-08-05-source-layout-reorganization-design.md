# Source Layout Reorganization Design

## Purpose

Reorganize Python source so package names describe ownership. `research_agent`
becomes application package, existing researcher implementation becomes
`research_agent.research_subagent`, and application-owned loose modules leave
repository root. This is intentionally a clean breaking rename: old import paths
will not remain available through compatibility shims.

## Goals

- Make `research_agent` clear application boundary and composition root.
- Make delegated researcher implementation explicit as `research_subagent`.
- Remove application-owned Python modules from repository root.
- Preserve runtime behavior, public symbols, persisted data formats, environment
  variables, and external API contracts.
- Keep independently owned `webapp` and `thread_wiki` packages top-level.
- Update every executable, configuration, deployment, test, and maintained
  documentation reference in same change.

## Non-goals

- Redesign agent orchestration or split large modules.
- Change model, storage, authentication, retry, or database behavior.
- Move `webapp` or `thread_wiki` under `research_agent`.
- Provide deprecated import aliases such as `research_agent.tools`.
- Remove deprecated launchers; they move with application package and remain
  excluded from production entry points.
- Reorganize standalone maintenance scripts that do not participate in runtime.

## Ownership Boundary

`../../../research_agent` owns application composition and shared runtime services:

```text
research_agent/
├── __init__.py
├── agent.py
├── auth.py
├── azure_storage.py
├── cli.py
├── cli_utils.py
├── db.py
├── db_sql.py
├── langgraph_snapshot.py
├── logger_utils.py
├── model_factory.py
├── retry_utils.py
├── run.py
├── s3_storage.py
├── server.py
└── research_subagent/
    ├── __init__.py
    ├── prompts.py
    ├── tools.py
    ├── clarification/
    ├── resume/
    └── utils/
```

Top-level `../../../webapp` remains HTTP/upload application. Top-level `../../../thread_wiki`
remains independent document and code knowledge subsystem. Root maintenance
scripts `../../../increment_version.py` and `../../../migrate_sqlite_to_cosmos.py` remain
standalone because they are not imported by application runtime.

New `research_agent/__init__.py` stays lightweight. It must not eagerly construct
graph, models, database clients, or other environment-dependent objects.

## File Mapping

| Current path | New path |
| --- | --- |
| `agent.py` | `../../../research_agent/agent.py` |
| `auth.py` | `../../../research_agent/auth.py` |
| `azure_storage.py` | `../../../research_agent/azure_storage.py` |
| `db.py` | `../../../research_agent/db.py` |
| `db_sql.py` | `../../../research_agent/db_sql.py` |
| `langgraph_snapshot.py` | `../../../research_agent/langgraph_snapshot.py` |
| `logger_utils.py` | `../../../research_agent/logger_utils.py` |
| `model_factory.py` | `../../../research_agent/model_factory.py` |
| `research_agent_cli.py` | `../../../research_agent/cli.py` |
| `retry_utils.py` | `../../../research_agent/retry_utils.py` |
| `run.py` | `../../../research_agent/run.py` |
| `s3_storage.py` | `../../../research_agent/s3_storage.py` |
| `server.py` | `../../../research_agent/server.py` |
| `utils.py` | `../../../research_agent/cli_utils.py` |
| `research_agent/**` | `research_agent/research_subagent/**` |

Moves should retain Git rename history. Content changes during relocation are
limited to imports, package-relative resource resolution, and executable module
paths required by new layout.

## Import Contract

- Application modules use canonical `research_agent.<module>` imports.
- Researcher internals use canonical
  `research_agent.research_subagent.<module>` imports.
- Imports within `research_subagent` may use explicit relative imports when they
  cannot be confused with application modules.
- `webapp` and `thread_wiki` import application services through
  `research_agent.*` and researcher services through
  `research_agent.research_subagent.*`.
- No module registration in `sys.modules`, forwarding modules, aliases, or
  deprecation wrappers preserve old paths.
- Imports such as `research_agent.tools` and root imports such as
  `from model_factory import ...` are invalid after migration.
- Root `utils.py` becomes `research_agent.cli_utils`, rather than
  `research_agent.utils`, so old researcher path `research_agent.utils` cannot
  silently resolve to unrelated application helpers.

Preserving public symbols means preserving their behavior under canonical new
module paths. It does not retain exports from old `research_agent` namespace.

## Entry Points and Execution Flow

`../../../langgraph.json` points graph and auth entries into packaged files under
`../../../research_agent`; web application entry remains under `../../../webapp`.

CLI invocation becomes:

```bash
uv run python -m research_agent.cli "What is quantum computing?"
```

Deprecated launcher invocation, where retained for reference, uses package
module paths such as `python -m research_agent.run` and
`python -m research_agent.server`. Production Docker and deployment entry points
continue using LangGraph configuration rather than deprecated launchers.

Runtime flow remains:

```text
CLI / LangGraph / webapp
        |
        v
research_agent.agent
        |
        v
research_agent.research_subagent
        |
        v
tools, documents, skills, models, persistence
```

No request, state, checkpoint, database, output, or skill data shape changes.

## Filesystem and Resource Resolution

Any path derived from `__file__` must be recalculated for extra package depth.
Repository resources such as `../../../.deepagents/skills`, `documents`, output folders,
configuration files, and local databases retain current repository-relative or
environment-configured locations. Tests must cover path-sensitive behavior so
package relocation does not silently point inside `../../../research_agent`.

## Migration Sequence

1. Add structure and configuration contract tests that fail against current
   layout.
2. Move existing `research_agent` contents into `research_subagent` and create
   lightweight application package initializer.
3. Move application-owned root modules into application package, renaming CLI
   module to `cli.py`.
4. Update researcher-internal imports.
5. Update application, `webapp`, `thread_wiki`, and test imports.
6. Update LangGraph, packaging, Docker, deployment, architecture-check, and
   script references.
7. Update maintained documentation and repository guidance.
8. Verify no stale paths remain and run focused then full checks.

Each group must leave repository importable before next group begins where
practical. Behavior changes discovered during move are fixed only when needed to
preserve pre-migration behavior.

## Test Strategy

TDD starts with layout contracts that demonstrate requested breaking change:

- application modules exist under `research_agent`;
- researcher modules exist under `research_agent.research_subagent`;
- application-owned root modules are absent;
- old `research_agent.tools`-style paths are absent;
- `../../../langgraph.json` points to packaged graph and auth modules;
- package discovery includes nested researcher packages;
- new CLI module invocation is importable without path hacks.

After moves, run focused suites for packaging, architecture boundaries, agent
contracts, prompts, tools, CLI, authentication, databases, snapshot handling,
storage, web application, and thread wiki. Run full pytest suite, Ruff, mypy,
and stale-reference scan before completion. Environment-dependent suites retain
their existing prerequisites and skip behavior.

## Risks and Controls

| Risk | Control |
| --- | --- |
| Import cycle introduced by new parent package | Keep parent initializer lightweight; import concrete modules directly. |
| Eager model or database setup during package import | Test plain package and CLI imports with external providers unavailable. |
| Resource paths shift with added directory depth | Audit `__file__` usage and add path-resolution tests. |
| Configuration still names root files | Contract-test LangGraph, Docker, deployment, and architecture references. |
| Stale paths survive in docs or tests | Scan tracked source, config, scripts, and maintained docs for old paths. |
| Git loses file history | Use file moves and inspect rename detection before commit. |
| Independent packages become coupled to internals | Keep their imports limited to canonical application/subagent modules. |

## Acceptance Criteria

- No application runtime `.py` module remains at repository root.
- `../../../increment_version.py` and `../../../migrate_sqlite_to_cosmos.py` are only allowed
  root Python scripts.
- `research_agent.agent:agent` is canonical graph composition object.
- `research_agent.research_subagent.tools` is canonical researcher tools module.
- Old root module imports and old `research_agent.<researcher-module>` imports
  fail rather than resolve through compatibility code.
- CLI, LangGraph graph, authentication, webapp, wiki, deployment, and packaging
  entry points use new paths.
- Existing behavior-focused tests pass without weakening assertions.
- New layout, import, entry-point, and stale-reference tests pass.
- Ruff and mypy report no new violations attributable to migration.
- Maintained documentation describes only canonical layout and commands.
