# Source Layout Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `research_agent` application package, move delegated researcher code to `research_agent.research_subagent`, and remove application runtime modules from repository root without changing behavior.

**Architecture:** Perform two Git-preserving moves: first nest current researcher package, then move loose application modules into its new parent package. Use canonical absolute imports only, keep `webapp` and `thread_wiki` top-level, preserve repository-relative resources, and make old import paths fail rather than forwarding them.

**Tech Stack:** Python 3.12–3.13, setuptools, uv, pytest, LangGraph, Ruff, mypy

---

## Target File Structure

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

Only `../../../increment_version.py` and `../../../migrate_sqlite_to_cosmos.py` remain as root
Python scripts. `../../../webapp` and `../../../thread_wiki` remain independent top-level
packages.

## Canonical Import Mapping

| Old import root | New import root |
| --- | --- |
| `agent` | `research_agent.agent` |
| `auth` | `research_agent.auth` |
| `azure_storage` | `research_agent.azure_storage` |
| `db` | `research_agent.db` |
| `db_sql` | `research_agent.db_sql` |
| `langgraph_snapshot` | `research_agent.langgraph_snapshot` |
| `logger_utils` | `research_agent.logger_utils` |
| `model_factory` | `research_agent.model_factory` |
| `research_agent_cli` | `research_agent.cli` |
| `retry_utils` | `research_agent.retry_utils` |
| `s3_storage` | `research_agent.s3_storage` |
| `server` | `research_agent.server` |
| root `utils` | `research_agent.cli_utils` |
| old `research_agent.*` researcher modules | `research_agent.research_subagent.*` |

Historical documents under `..` are records and are not
rewritten. Generated/cache/worktree copies are not migration targets.

### Task 1: Nest Researcher Implementation

**Files:**
- Create: `../../../tests/test_source_layout.py`
- Create: `research_agent/__init__.py`
- Move: `research_agent/prompts.py` → `../../../research_agent/research_subagent/prompts.py`
- Move: `research_agent/tools.py` → `../../../research_agent/research_subagent/tools.py`
- Move: `research_agent/clarification/` → `../../../research_agent/research_subagent/clarification`
- Move: `research_agent/resume/` → `../../../research_agent/research_subagent/resume`
- Move: `research_agent/utils/` → `../../../research_agent/research_subagent/utils`
- Move: `research_agent/__init__.py` → `research_agent/research_subagent/__init__.py`
- Modify: `agent.py:41-70`
- Modify: `research_agent_cli.py:22-24`
- Modify: `server.py:55-60`
- Modify: `webapp/routes.py:26-27`
- Modify: `thread_wiki/routes.py:25`
- Modify: `thread_wiki/service.py:38-41`
- Modify: `research_agent.ipynb:66,114,321`
- Modify: `../../../tests/test_citations.py`
- Modify: `../../../tests/test_clarification.py`
- Modify: `../../../tests/test_eval_tracking.py`
- Modify: `../../../tests/test_learning.py`
- Modify: `../../../tests/test_prompts_validation.py`
- Modify: `../../../tests/test_research_agent_cli.py`
- Modify: `../../../tests/test_resume.py`
- Modify: `../../../tests/test_retrieval.py`
- Modify: `../../../tests/test_skill_contracts.py`
- Modify: `../../../tests/test_skill_registry.py`
- Modify: `../../../tests/test_tools.py`
- Modify: `../../../tests/test_validation.py`
- Modify: `../../../tests/test_verification.py`
- Modify: `../../../tests/test_web_search.py`
- Modify: `../../../tests/test_write_file.py`

- [ ] **Step 1: Write failing researcher-boundary tests**

Create `../../../tests/test_source_layout.py`:

```python
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RESEARCHER_ENTRIES = (
    "__init__.py",
    "prompts.py",
    "tools.py",
    "clarification",
    "resume",
    "utils",
)


def test_researcher_implementation_is_nested() -> None:
    nested = ROOT / "research_agent" / "research_subagent"

    assert all((nested / entry).exists() for entry in RESEARCHER_ENTRIES)


def test_legacy_researcher_locations_are_absent() -> None:
    package = ROOT / "research_agent"

    assert all(not (package / entry).exists() for entry in RESEARCHER_ENTRIES[1:])


def test_legacy_researcher_imports_do_not_resolve() -> None:
    legacy_modules = (
        "research_agent.prompts",
        "research_agent.tools",
        "research_agent.clarification",
        "research_agent.resume",
        "research_agent.utils",
    )

    assert all(importlib.util.find_spec(module) is None for module in legacy_modules)


def test_research_subagent_resources_resolve_from_repository_root() -> None:
    from research_agent.research_subagent.utils.knowledge_filesystem import (
        _PROJECT_ROOT,
    )
    from research_agent.research_subagent.utils.skill_registry import SkillRegistry

    registry = SkillRegistry()

    assert _PROJECT_ROOT == ROOT
    assert registry.skills_dirs[:2] == [
        ROOT / ".deepagents" / "skills",
        ROOT / "docs" / ".deepagents" / "skills",
    ]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_source_layout.py -v
```

Expected: failures because `../../../research_agent/research_subagent` does not exist and
legacy researcher paths still resolve.

- [ ] **Step 3: Move researcher files with Git history**

Run:

```bash
mkdir -p research_agent/research_subagent
git mv research_agent/__init__.py research_agent/research_subagent/__init__.py
git mv research_agent/prompts.py research_agent/research_subagent/prompts.py
git mv research_agent/tools.py research_agent/research_subagent/tools.py
git mv research_agent/clarification research_agent/research_subagent/clarification
git mv research_agent/resume research_agent/research_subagent/resume
git mv research_agent/utils research_agent/research_subagent/utils
```

- [ ] **Step 4: Create lightweight application initializer**

Create `research_agent/__init__.py`:

```python
"""Deep research application package."""
```

Do not re-export researcher symbols or import environment-dependent modules.

- [ ] **Step 5: Rewrite researcher imports to nested namespace**

Apply this mapping in moved researcher files, root entry modules, independent
packages, notebook cells, test imports, and `unittest.mock.patch` strings:

```text
research_agent.prompts        -> research_agent.research_subagent.prompts
research_agent.tools          -> research_agent.research_subagent.tools
research_agent.clarification  -> research_agent.research_subagent.clarification
research_agent.resume         -> research_agent.research_subagent.resume
research_agent.utils          -> research_agent.research_subagent.utils
```

Update `agent.py` to import public researcher tools directly from
`research_agent.research_subagent`, not from new lightweight parent initializer.

- [ ] **Step 6: Preserve repository resource resolution after added depth**

Update only parent-depth calculations affected by researcher move:

```python
# research_agent/research_subagent/tools.py
base_dir = _resolve_wiki_base_dir(Path(__file__).resolve().parent.parent.parent)

# research_agent/research_subagent/utils/skill_registry.py
base_dir = Path(__file__).resolve().parent.parent.parent.parent

# research_agent/research_subagent/utils/cli.py
base_dir = Path(__file__).resolve().parent.parent.parent.parent

# research_agent/research_subagent/utils/knowledge_filesystem.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# research_agent/research_subagent/utils/text_search.py (temporary until Task 2)
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent.parent))
```

For `text_search.py`, remove temporary `sys.path` mutation and import canonical
factory directly once Task 2 creates it:

```python
from research_agent.model_factory import create_embedding_model
```

Until Task 2, keep existing model import block working with corrected temporary
root depth; complete its canonical rewrite in Task 2.

- [ ] **Step 7: Run focused nested-package tests**

Run:

```bash
uv run pytest tests/test_source_layout.py tests/test_prompts_validation.py tests/test_clarification.py tests/test_resume.py tests/test_validation.py tests/test_verification.py tests/test_skill_registry.py tests/test_research_agent_cli.py tests/test_skill_contracts.py -v
```

Expected: PASS. If collection imports `agent.py`, configure existing model test
prerequisites rather than weakening assertions.

- [ ] **Step 8: Check no old researcher imports remain outside history/spec artifacts**

Run:

```bash
rg -n --glob '*.py' --glob '*.ipynb' --glob '!tests/test_source_layout.py' --glob '!documents/history/**' --glob '!docs/superpowers/**' --glob '!.worktrees/**' 'research_agent\.(prompts|tools|clarification|resume|utils)'
```

Expected: no old-path matches.

- [ ] **Step 9: Commit nested researcher package**

```bash
git add tests/test_source_layout.py research_agent agent.py research_agent_cli.py server.py webapp/routes.py thread_wiki/routes.py thread_wiki/service.py research_agent.ipynb tests
git commit -m "refactor: nest research subagent package"
```

### Task 2: Move Application Modules into `research_agent`

**Files:**
- Modify: `../../../tests/test_source_layout.py`
- Move: `agent.py` → `../../../research_agent/agent.py`
- Move: `auth.py` → `../../../research_agent/auth.py`
- Move: `azure_storage.py` → `../../../research_agent/azure_storage.py`
- Move: `db.py` → `../../../research_agent/db.py`
- Move: `db_sql.py` → `../../../research_agent/db_sql.py`
- Move: `langgraph_snapshot.py` → `../../../research_agent/langgraph_snapshot.py`
- Move: `logger_utils.py` → `../../../research_agent/logger_utils.py`
- Move: `model_factory.py` → `../../../research_agent/model_factory.py`
- Move: `research_agent_cli.py` → `../../../research_agent/cli.py`
- Move: `retry_utils.py` → `../../../research_agent/retry_utils.py`
- Move: `run.py` → `../../../research_agent/run.py`
- Move: `s3_storage.py` → `../../../research_agent/s3_storage.py`
- Move: `server.py` → `../../../research_agent/server.py`
- Move: `utils.py` → `../../../research_agent/cli_utils.py`
- Modify: `../../../research_agent/research_subagent/tools.py`
- Modify: `../../../research_agent/research_subagent/utils/citation_validator.py`
- Modify: `../../../research_agent/research_subagent/utils/content_extractors.py`
- Modify: `../../../research_agent/research_subagent/utils/eval_tracking.py`
- Modify: `../../../research_agent/research_subagent/utils/knowledge_filesystem.py`
- Modify: `../../../research_agent/research_subagent/utils/skill_registry.py`
- Modify: `../../../research_agent/research_subagent/utils/text_search.py`
- Modify: `../../../research_agent/research_subagent/utils/verification.py`
- Modify: `../../../research_agent/research_subagent/utils/web_search.py`
- Modify: `../../../.deepagents/skills/golden-dataset/scripts/skill_model_factory.py`
- Modify: `../../../thread_wiki/routes.py`
- Modify: `../../../thread_wiki/service.py`
- Modify: `../../../tests/logger_example.py`
- Modify: `../../../tests/test_agent_contracts.py`
- Modify: `../../../tests/test_aws_persistence_scripts.py`
- Modify: `../../../tests/test_azure_persistence_scripts.py`
- Modify: `../../../tests/test_clarification.py`
- Modify: `../../../tests/test_frontend_api_contract.py`
- Modify: `../../../tests/test_langgraph_snapshot.py`
- Modify: `../../../tests/test_research_agent_cli_e2e.py`
- Modify: `../../../tests/test_research_agent_cli_helpers.py`
- Modify: `../../../tests/test_retry_utils.py`
- Modify: `../../../tests/test_server.py`
- Modify: `../../../tests/test_tools.py`

- [ ] **Step 1: Extend layout tests for application ownership and paths**

Add to `../../../tests/test_source_layout.py`:

```python
APPLICATION_FILES = {
    "agent.py": "agent.py",
    "auth.py": "auth.py",
    "azure_storage.py": "azure_storage.py",
    "db.py": "db.py",
    "db_sql.py": "db_sql.py",
    "langgraph_snapshot.py": "langgraph_snapshot.py",
    "logger_utils.py": "logger_utils.py",
    "model_factory.py": "model_factory.py",
    "research_agent_cli.py": "cli.py",
    "retry_utils.py": "retry_utils.py",
    "run.py": "run.py",
    "s3_storage.py": "s3_storage.py",
    "server.py": "server.py",
    "utils.py": "cli_utils.py",
}


def test_application_modules_are_packaged() -> None:
    package = ROOT / "research_agent"

    assert all((package / target).is_file() for target in APPLICATION_FILES.values())


def test_only_maintenance_scripts_remain_at_root() -> None:
    root_python_files = {path.name for path in ROOT.glob("*.py")}

    assert root_python_files == {
        "increment_version.py",
        "migrate_sqlite_to_cosmos.py",
    }


def test_packaged_storage_still_resolves_repository_directories(monkeypatch) -> None:
    from research_agent import azure_storage, s3_storage

    monkeypatch.setenv("REPORTS_OUTPUT_FOLDER", "./output")
    monkeypatch.setenv("INPUT_FOLDER", "./input")

    azure_paths = dict(azure_storage._resolve_tracked_folders())
    s3_paths = dict(s3_storage._resolve_tracked_folders())

    assert azure_paths["docs"] == ROOT / "docs"
    assert azure_paths[".langgraph_api"] == ROOT / ".langgraph_api"
    assert s3_paths["docs"] == ROOT / "docs"


def test_golden_dataset_factory_uses_packaged_runtime_imports() -> None:
    source = (
        ROOT / ".deepagents/skills/golden-dataset/scripts/skill_model_factory.py"
    ).read_text(encoding="utf-8")

    assert "from research_agent.retry_utils import wrap_model_with_rate_limiting" in source
    assert "from research_agent.cli_utils import get_ssl_verify_config" in source
    assert "from retry_utils import" not in source
    assert "from utils import" not in source
```

- [ ] **Step 2: Run application-layout tests and verify RED**

Run:

```bash
uv run pytest tests/test_source_layout.py -v
```

Expected: application files missing under package, root contains runtime modules,
and packaged storage imports fail.

- [ ] **Step 3: Move every application module with Git history**

Run:

```bash
git mv agent.py research_agent/agent.py
git mv auth.py research_agent/auth.py
git mv azure_storage.py research_agent/azure_storage.py
git mv db.py research_agent/db.py
git mv db_sql.py research_agent/db_sql.py
git mv langgraph_snapshot.py research_agent/langgraph_snapshot.py
git mv logger_utils.py research_agent/logger_utils.py
git mv model_factory.py research_agent/model_factory.py
git mv research_agent_cli.py research_agent/cli.py
git mv retry_utils.py research_agent/retry_utils.py
git mv run.py research_agent/run.py
git mv s3_storage.py research_agent/s3_storage.py
git mv server.py research_agent/server.py
git mv utils.py research_agent/cli_utils.py
```

- [ ] **Step 4: Rewrite production imports to canonical application paths**

Apply Canonical Import Mapping table to moved modules, researcher modules,
`webapp`, and `thread_wiki`. Required special cases:

```python
# research_agent/db.py
from research_agent import db_sql

# thread_wiki/routes.py, inside _wiki_get_current_user
from research_agent import server as _server

# research_agent/research_subagent/utils/text_search.py
from research_agent.model_factory import create_embedding_model

# .deepagents/skills/golden-dataset/scripts/skill_model_factory.py
from research_agent.cli_utils import get_ssl_verify_config
from research_agent.retry_utils import wrap_model_with_rate_limiting
```

Delete `text_search.py`'s temporary `sys.path` insertion/finally block; installed
package imports now provide model factory directly. Delete the golden-dataset
factory's `sys.path` insertion plus now-unused `sys` and `Path` imports.

- [ ] **Step 5: Rewrite tests and patch targets**

Use canonical imports in listed tests. Preserve local aliases where this avoids
unrelated assertion churn:

```python
from research_agent import db
from research_agent import server
from research_agent import azure_storage
from research_agent import s3_storage
from research_agent.agent import ResearchStateMiddleware
from research_agent.cli import main
from research_agent.langgraph_snapshot import ...
from research_agent.retry_utils import ...
```

Update patch strings from `research_agent.utils.*` to
`research_agent.research_subagent.utils.*`. Update source-contract reads to:

```python
Path("research_agent/agent.py").read_text(encoding="utf-8")
```

- [ ] **Step 6: Preserve root-relative storage behavior**

Update moved storage modules:

```python
# research_agent/azure_storage.py and research_agent/s3_storage.py
project_root = Path(__file__).resolve().parent.parent
docs_root = project_root / "docs"
```

In Azure storage also resolve `.langgraph_api` from `project_root`. Leave
environment-controlled output/input paths unchanged.

- [ ] **Step 7: Run focused application migration tests**

Run:

```bash
uv run pytest tests/test_source_layout.py tests/test_agent_contracts.py tests/test_retry_utils.py tests/test_langgraph_snapshot.py tests/test_azure_persistence_scripts.py tests/test_aws_persistence_scripts.py tests/test_research_agent_cli_helpers.py tests/test_server.py tests/test_frontend_api_contract.py tests/test_eval_tracking.py -v
```

Expected: PASS.

- [ ] **Step 8: Run import and architecture checks**

Run:

```bash
uv run python -c "import research_agent"
uv run python -c "import research_agent.research_subagent.tools"
uv run python scripts/check_architecture.py
```

Expected: both commands exit 0. Plain `import research_agent` must not initialize
models or require provider credentials. Agent graph import keeps its existing
configured-model prerequisite and is covered by agent-focused tests.

- [ ] **Step 9: Commit packaged application modules**

```bash
git add research_agent thread_wiki webapp tests .deepagents/skills/golden-dataset/scripts/skill_model_factory.py
git commit -m "refactor: package research application modules"
```

### Task 3: Update Runtime and Packaging Entry Points

**Files:**
- Modify: `../../../tests/test_source_layout.py`
- Modify: `../../../tests/test_packaging.py`
- Modify: `../../../langgraph.json`
- Modify: `../../../.github/workflows/eval-regression.yml`
- Possibly regenerate: `../../../deep_research_example.egg-info/SOURCES.txt`

- [ ] **Step 1: Add failing entry-point contracts**

Add to `../../../tests/test_source_layout.py`:

```python
import json


def test_langgraph_uses_packaged_entrypoints() -> None:
    config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8"))

    assert config["graphs"]["research"] == "./research_agent/agent.py:agent"
    assert config["auth"]["path"] == "./research_agent/auth.py:auth"
    assert config["http"]["app"] == "./webapp/__init__.py:app"


def test_eval_workflow_watches_packaged_agent() -> None:
    workflow = (ROOT / ".github/workflows/eval-regression.yml").read_text(
        encoding="utf-8"
    )

    assert '"research_agent/agent.py"' in workflow
    assert '"agent.py"' not in workflow
    assert '"research_agent/model_factory.py"' in workflow
    assert '"model_factory.py"' not in workflow
    assert "from research_agent.research_subagent.utils.eval_tracking import" in workflow
    assert "from research_agent.research_subagent.utils.learning import" in workflow
    assert "from research_agent.utils" not in workflow
```

Extend `../../../tests/test_packaging.py`:

```python
def test_nested_research_subagent_is_discoverable() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    include = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]

    assert "research_agent.*" in include
```

- [ ] **Step 2: Run entry-point tests and verify RED**

Run:

```bash
uv run pytest tests/test_source_layout.py tests/test_packaging.py -v
```

Expected: LangGraph and workflow path assertions fail.

- [ ] **Step 3: Update exact LangGraph paths**

Change only graph and auth values:

```json
"graphs": {
  "research": "./research_agent/agent.py:agent"
},
"auth": {
  "path": "./research_agent/auth.py:auth"
}
```

Keep `./webapp/__init__.py:app` unchanged.

- [ ] **Step 4: Update CI path filter**

Update every executable reference in `../../../.github/workflows/eval-regression.yml`:

```text
"agent.py"                                      -> "research_agent/agent.py"
"model_factory.py"                              -> "research_agent/model_factory.py"
research_agent.utils.eval_tracking              -> research_agent.research_subagent.utils.eval_tracking
research_agent.utils.learning                   -> research_agent.research_subagent.utils.learning
```

- [ ] **Step 5: Rebuild editable package metadata**

Run:

```bash
uv sync --frozen
```

If tracked `../../../deep_research_example.egg-info/SOURCES.txt` changes, include generated
path updates; do not hand-edit metadata.

- [ ] **Step 6: Verify package and entry-point contracts**

Run:

```bash
uv run pytest tests/test_source_layout.py tests/test_packaging.py tests/test_architecture_boundaries.py -v
uv run python -m research_agent.cli --help
```

Expected: tests pass and CLI help exits 0. If CLI currently constructs model
before parsing `--help`, preserve existing provider prerequisite and verify with
configured test model rather than adding compatibility imports.

- [ ] **Step 7: Commit entry-point changes**

```bash
git add langgraph.json .github/workflows/eval-regression.yml tests/test_source_layout.py tests/test_packaging.py deep_research_example.egg-info/SOURCES.txt
git commit -m "build: point runtime at packaged application"
```

Omit generated metadata path from `git add` when it did not change.

### Task 4: Remove Remaining Stale Code and Notebook References

**Files:**
- Modify: `../../../tests/test_source_layout.py`
- Modify: every remaining `.py` file reported by failing contract
- Modify: `../../research_agent.ipynb`

- [ ] **Step 1: Add failing AST import contract**

Add to `../../../tests/test_source_layout.py`:

```python
import ast

LEGACY_ROOT_MODULES = {
    "agent",
    "auth",
    "azure_storage",
    "db",
    "db_sql",
    "langgraph_snapshot",
    "logger_utils",
    "model_factory",
    "research_agent_cli",
    "retry_utils",
    "s3_storage",
    "server",
    "utils",
}
LEGACY_RESEARCHER_PREFIXES = (
    "research_agent.prompts",
    "research_agent.tools",
    "research_agent.clarification",
    "research_agent.resume",
    "research_agent.utils",
)


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def test_python_sources_do_not_use_legacy_imports() -> None:
    source_roots = (
        ROOT / "research_agent",
        ROOT / "webapp",
        ROOT / "thread_wiki",
        ROOT / "tests",
        ROOT / ".deepagents" / "skills",
    )
    violations: list[str] = []
    for source_root in source_roots:
        for path in source_root.rglob("*.py"):
            imports = _absolute_imports(path)
            stale = {
                module
                for module in imports
                if module.split(".", maxsplit=1)[0] in LEGACY_ROOT_MODULES
                or module.startswith(LEGACY_RESEARCHER_PREFIXES)
            }
            violations.extend(f"{path.relative_to(ROOT)}: {module}" for module in sorted(stale))

    assert not violations, "\n".join(violations)
```

- [ ] **Step 2: Run stale-import contract and verify RED if references remain**

Run:

```bash
uv run pytest tests/test_source_layout.py::test_python_sources_do_not_use_legacy_imports -v
```

Expected: FAIL listing any missed imports, or PASS if Task 1–3 already replaced
all imports. If it passes immediately, keep it as durable architecture contract;
do not manufacture a change.

- [ ] **Step 3: Replace every reported Python import**

Use Canonical Import Mapping table. Do not add `sys.modules` aliases, forwarding
modules, or root wrappers.

- [ ] **Step 4: Update notebook code cells**

In `../../research_agent.ipynb`, change only code-cell source strings:

```python
from research_agent.research_subagent.tools import tavily_search, think_tool
from research_agent.research_subagent.prompts import ...
from research_agent.research_subagent.utils.skill_registry import get_skill_registry
```

Keep notebook outputs and metadata unchanged.

- [ ] **Step 5: Verify notebook JSON and stale-import contract**

Run:

```bash
uv run python -m json.tool research_agent.ipynb
uv run pytest tests/test_source_layout.py -v
```

Expected: valid JSON and all layout/import contracts pass.

- [ ] **Step 6: Commit stale-reference cleanup**

```bash
git add tests/test_source_layout.py research_agent webapp thread_wiki tests research_agent.ipynb
git commit -m "test: enforce canonical source imports"
```

### Task 5: Update Maintained Documentation and Repository Guidance

**Files:**
- Modify: `../../../tests/test_documentation.py`
- Modify: `../../../README.md`
- Modify: `../../../AGENTS.md`
- Modify: `../../api/upload.md`
- Modify: `../../architecture/clean-architecture.md`
- Modify: `../../architecture/overview.md`
- Modify: `../../development/extending-the-agent.md`
- Modify: `../../development/testing.md`
- Modify: `../../getting-started/installation.md`
- Modify: `../../getting-started/local-development.md`
- Modify: `../../getting-started/usage.md`
- Modify: `../../guides/configuration.md`
- Modify: `../../guides/evaluation.md`
- Modify: `../../guides/reliability.md`

- [ ] **Step 1: Add failing maintained-documentation contract**

Add to `../../../tests/test_documentation.py`:

```python
MAINTAINED_SOURCE_DOCS = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    *sorted((ROOT / "documents" / "api").glob("*.md")),
    *sorted((ROOT / "documents" / "architecture").glob("*.md")),
    *sorted((ROOT / "documents" / "development").glob("*.md")),
    *sorted((ROOT / "documents" / "getting-started").glob("*.md")),
    *sorted((ROOT / "documents" / "guides").glob("*.md")),
)
LEGACY_SOURCE_DOC_TOKENS = (
    "uv run python research_agent_cli.py",
    "uv run python model_factory.py",
    "`research_agent_cli.py`",
    "`research_agent/tools.py`",
    "`research_agent/prompts.py`",
    "`research_agent/utils/",
    "[agent.py](agent.py)",
    "[auth.py](auth.py)",
    "[model_factory.py](model_factory.py)",
    "[server.py](server.py)",
    "[run.py](run.py)",
)


def test_maintained_docs_use_packaged_source_paths() -> None:
    violations: list[str] = []
    for path in MAINTAINED_SOURCE_DOCS:
        text = path.read_text(encoding="utf-8")
        violations.extend(
            f"{path.relative_to(ROOT)}: {token}"
            for token in LEGACY_SOURCE_DOC_TOKENS
            if token in text
        )

    assert not violations, "\n".join(violations)
```

- [ ] **Step 2: Run documentation contract and verify RED**

Run:

```bash
uv run pytest tests/test_documentation.py::test_maintained_docs_use_packaged_source_paths -v
```

Expected: FAIL with current CLI commands and root/module links.

- [ ] **Step 3: Update commands and source paths**

Use canonical forms consistently:

```text
uv run python -m research_agent.cli "Topic"
uv run python -m research_agent.model_factory
research_agent/agent.py
research_agent/research_subagent/tools.py
research_agent/research_subagent/prompts.py
research_agent/research_subagent/utils/...
```

Update relative Markdown links according to each document's directory depth.
Keep `..` unchanged.

- [ ] **Step 4: Update architecture descriptions**

Describe `research_agent` as application package and
`research_agent.research_subagent` as delegated researcher package. Keep
`webapp` and `thread_wiki` top-level. Update AGENTS.md entry-point, data-flow,
file-organization, testing, troubleshooting, and enhancement examples.

- [ ] **Step 5: Run documentation and focused structure tests**

Run:

```bash
uv run pytest tests/test_documentation.py tests/test_source_layout.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit documentation migration**

```bash
git add README.md AGENTS.md documents tests/test_documentation.py
git commit -m "docs: document packaged research architecture"
```

### Task 6: Full Verification and Migration Audit

**Files:**
- Modify only files required by verified failures

- [ ] **Step 1: Verify no stale Python imports remain**

Run:

```bash
rg --hidden -n --glob '*.py' --glob '!tests/test_source_layout.py' --glob '!.git/**' --glob '!.worktrees/**' '^(from|import) (agent|auth|azure_storage|db|db_sql|langgraph_snapshot|logger_utils|model_factory|research_agent_cli|retry_utils|s3_storage|server|utils)(\.| import|$)'
rg --hidden -n --glob '*.py' --glob '*.ipynb' --glob '!tests/test_source_layout.py' --glob '!documents/history/**' --glob '!docs/superpowers/**' --glob '!.git/**' --glob '!.worktrees/**' 'research_agent\.(prompts|tools|clarification|resume|utils)'
```

Expected: no legacy imports. Review matches manually because prose and test names
may legitimately contain filename substrings.

- [ ] **Step 2: Verify maintained commands and configuration**

Run:

```bash
rg -n --glob '!documents/history/**' --glob '!docs/superpowers/**' --glob '!.worktrees/**' 'uv run python (research_agent_cli|model_factory)\.py|\./(agent|auth)\.py:|research_agent\.(prompts|tools|clarification|resume|utils)' README.md AGENTS.md documents langgraph.json .github
```

Expected: no matches.

- [ ] **Step 3: Run architecture, packaging, and import checks**

Run:

```bash
uv run python scripts/check_architecture.py
uv run pytest tests/test_source_layout.py tests/test_packaging.py tests/test_architecture_boundaries.py -v
uv run python -c "import research_agent"
uv run python -c "import research_agent.research_subagent.tools"
```

Expected: all commands exit 0.

- [ ] **Step 4: Run complete pytest suite**

Run:

```bash
uv run pytest tests/ -v
```

Expected: all configured tests pass. Classify missing external model/network
prerequisites explicitly; do not weaken or skip tests to obtain green output.

- [ ] **Step 5: Run lint and type checking**

Run:

```bash
uv run ruff check .
uv run mypy research_agent/
```

Expected: no migration-introduced errors.

- [ ] **Step 6: Inspect Git history preservation and diff hygiene**

Run:

```bash
git diff --check main...HEAD
git diff --stat main...HEAD
git diff --summary main...HEAD
git status --short
```

Expected: clean whitespace, moves detected as renames where content similarity
permits, and no uncommitted files.

- [ ] **Step 7: Record code areas and final decision evidence**

Record package boundary, canonical imports, entry points, and resource-path
adjustments in project context memory. If Threadroot recorded a run, inspect:

```bash
threadroot score latest --json
```

- [ ] **Step 8: Final commit for verification-only fixes, if needed**

```bash
git add <exact-files-fixed-after-verification>
git commit -m "fix: complete source layout migration"
```

Skip this commit when verification required no changes.
