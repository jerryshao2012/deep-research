# Extend the research agent

Use this guide to add prompts, tools, model providers, output skills, delegation behavior, or document formats without bypassing current registration paths. It is for contributors who need the implementation files and focused tests for each extension point.

## Choose an extension point

| Change | Active implementation | Required verification |
| --- | --- | --- |
| Orchestration or researcher behavior | `research_agent/research_subagent/prompts.py`, assembled in `research_agent/agent.py` | `tests/test_prompts_validation.py`, relevant agent contract tests |
| Agent tool | `research_agent/research_subagent/tools.py`, registered in `research_agent/agent.py` | focused tool tests plus agent contracts when registration changes |
| Chat or embedding provider | `research_agent/model_factory.py` | provider branch tests and `tests/test_retry_utils.py` when retry wrapping changes |
| Output skill | `.deepagents/skills/<skill>/SKILL.md` | `tests/test_skill_registry.py`, `tests/test_skill_contracts.py` |
| Delegation topology or limits | `research_agent/agent.py`, `research_agent/research_subagent/prompts.py` | prompt validation and `tests/test_agent_contracts.py` |
| Document format or context limit | `research_agent/research_subagent/utils/content_extractors.py`, `research_agent/research_subagent/utils/knowledge_filesystem.py` | `tests/test_tools.py` plus upload or ingestion tests when those surfaces change |

Read [Architecture overview](../architecture/overview.md) before changing boundaries shared by the CLI, LangGraph server, and web application.

## Change orchestration prompts

Edit the instruction constant that owns the behavior:

| Constant | Current consumer and responsibility |
| --- | --- |
| `RESEARCH_WORKFLOW_INSTRUCTIONS` | Orchestrator workflow, planning, synthesis, citations, and report-writing rules; included in `research_agent/agent.py`'s `INSTRUCTIONS`. |
| `SUBAGENT_DELEGATION_INSTRUCTIONS` | Orchestrator delegation strategy and formatted concurrency/iteration limits; appended to `INSTRUCTIONS` in `research_agent/agent.py`. |
| `RESEARCHER_INSTRUCTIONS` | Web-research sub-agent tool use, reflection, budgets, and stopping criteria; used as `research_sub_agent["system_prompt"]`. |
| `RESEARCHER_DESCRIPTION` | Description used to route work to the `research-agent` sub-agent. |

Keep these constants in `research_agent/research_subagent/prompts.py`. `research_agent/research_subagent/__init__.py` owns the delegated researcher's public prompt and tool exports; update that nested public API only when adding or renaming an exported constant. Outer `research_agent/__init__.py` intentionally remains a lightweight application-package marker.

For each prompt change:

1. add or update an assertion in `tests/test_prompts_validation.py` before changing the instruction;
2. update `research_agent/agent.py` only when assembly, formatting inputs, or consumers change;
3. run the focused prompt suite and any affected `tests/test_agent_contracts.py` nodes;
4. update [Validate prompt changes](prompt-validation.md) when the validation contract changes.

When changing reflection guidance, keep `think_tool` strategic rather than ceremonial. Instructions should require researcher to state what was learned, whether sources agree, what evidence is still missing, whether another search can close gap, and whether budget warrants stopping. Add concrete prompt-validation assertions for any new required reflection behavior.

## Add a custom tool

Define the LangChain `@tool` wrapper in `research_agent/research_subagent/tools.py`. Put substantial I/O or reusable logic in a focused module under `research_agent/research_subagent/utils/`, keep state-only parameters injected with `InjectedState` or `InjectedToolArg`, and document purpose, when to call the tool, arguments, return value, and failure behavior in its docstring.

Register the tool with the agent that needs it:

- add orchestrator tools to `_agent_kwargs["tools"]` in `research_agent/agent.py` when they need document state, local files, wiki access, or report writes;
- add narrowly scoped web-research tools to `research_sub_agent["tools"]` when isolated sub-agents should call them;
- export a stable delegated-research tool from `research_agent/research_subagent/__init__.py` only when callers outside `research_agent/agent.py` need it; keep outer `research_agent/__init__.py` lightweight;
- update prompt guidance only when the model needs explicit selection or sequencing rules.

Keep one owner by default. The current orchestrator owns `clarify_requirements`, `read_file`, `write_file`, `ls`, `glob`, `read_docs_folder`, and `llm_wiki_query`; `think_tool` is shared with the intentionally web-only `research-agent` sub-agent, whose other tools are `tavily_search` and `fetch_webpage_content`.

Add isolated behavior tests in the matching `tests/test_*.py` file. Update `tests/test_agent_contracts.py` when tool availability or ownership changes, and use external-service mocks only at the API boundary.

## Add a model provider

Chat-provider selection lives in `research_agent.model_factory._build_configured_model()`. Add the provider's complete environment predicate and construction branch in deliberate precedence order, pass the repository SSL configuration where the client supports it, and return `wrap_model_with_rate_limiting(model)` so shared rate shaping and retry behavior remain active.

Current chat precedence is the AWS Bedrock-compatible endpoint, versioned Azure OpenAI, Azure's OpenAI-compatible endpoint without an explicit API version, Google, Anthropic, then Ollama. Insert a new branch intentionally because the first complete environment predicate wins.

Also check these surfaces:

- add provider packages to `pyproject.toml` only when the existing LangChain integrations do not supply them;
- extend authentication helpers in `research_agent/model_factory.py` when the provider supports multiple credential modes;
- document supported environment variables in [Configuration](../guides/configuration.md);
- add focused branch-selection and missing-configuration tests under `tests/`;
- run `tests/test_retry_utils.py` when wrapper behavior changes and `tests/test_google_connectivity.py` when changing the Google path.

Embedding selection is separate in `create_embedding_model()`, whose current order is Azure OpenAI, OpenAI, Google, Ollama, then `SimpleLocalEmbeddings`. Extend that function and its fallback order when the new provider also supplies embeddings; do not assume a chat-provider branch automatically configures retrieval.

## Add an output skill

Built-in project skills live in `.deepagents/skills/<skill-id>/SKILL.md`. Supported custom extensions live in `docs/.deepagents/skills/<skill-id>/SKILL.md`; the skill-upload route creates and populates that root, so it can be absent before the first custom skill is installed.

Every `SKILL.md` needs YAML frontmatter containing at least `name` and `description`. Add only scripts or supporting assets referenced by the skill.

Runtime discovery order is explicit:

| Root | `SkillsMiddleware` order | `SkillRegistry` order | Role |
| --- | ---: | ---: | --- |
| `.deepagents/skills/` | 1 | 1 | Active built-in project skills; populated in the repository. |
| `doc/.deepagents/skills/` | 2 | Not scanned | Compatibility root for older layouts; normally absent. |
| `docs/.deepagents/skills/` | 3 | 2 | Supported custom extension and upload target; created on demand and scanned by the registry. |

`research_agent/agent.py` passes all three roots to Deep Agents in the order shown. `research_agent/research_subagent/utils/skill_registry.py` scans the built-in root before the custom root, while `webapp/routes.py` installs uploaded skills into `docs/.deepagents/skills/`.

`research_agent/skills/` is not an active source and does not exist in the current tree; references to it in older material describe a pre-migration layout. Structured skills may additionally define the schema, render spec, defaults, quality guidelines, and scripts consumed by `SkillRegistry`, but plain instructional skills do not need those sections.

At runtime, registry discovers skill metadata, selected instructions are injected into research prompt, and `render_skill_output` applies configured render/validation path before artifact is written under active output directory. Golden-dataset workflow may also run bundled metrics and scoring helpers; frontend-slides and other presentation skills own their assets/scripts inside skill directory. Do not register these legacy helper names as general agent tools unless current graph explicitly exposes them.

Run both discovery contracts after adding a skill:

```bash
uv run pytest tests/test_skill_registry.py tests/test_skill_contracts.py -v
```

Add a focused contract test for any skill-specific schema, renderer, or script behavior.

## Change delegation behavior

The current graph registers one `SubAgent` named `research-agent`. The orchestrator delegates through Deep Agents' built-in `task()` capability; sub-agent results return to the orchestrator for comparison and synthesis, while local-document snippets must be supplied in the delegated prompt because the sub-agent has no filesystem tools.

For strategy-only changes, edit `SUBAGENT_DELEGATION_INSTRUCTIONS` and its prompt assertions. For capability or topology changes, update `research_sub_agent`, `_agent_kwargs["subagents"]`, and the corresponding tool registrations in `research_agent/agent.py`; keep `MAX_CONCURRENT_RESEARCH_UNITS` and `MAX_RESEARCHER_ITERATIONS` aligned with the formatted prompt and configuration docs.

Verify delegation changes with:

```bash
uv run pytest tests/test_prompts_validation.py tests/test_agent_contracts.py -v
```

## Extend document and context handling

`ResearchStateMiddleware` in `research_agent/agent.py` seeds `/research_request.md`, injects current files into state context, configures the document folder, and leaves uploaded-file access with the orchestrator. `read_docs_folder` in `research_agent/research_subagent/tools.py` delegates extraction to `read_docs_folder_impl` in `research_agent/research_subagent/utils/knowledge_filesystem.py`, which constrains reads to the configured folder, prefers a ready thread wiki, caches extracts under the active output folder's `extracted/` directory, and avoids returning oversized bodies inline.

Current direct extraction supports `.pdf`, `.txt`, `.md`, `.docx`, `.pptx`, and `.xlsx`. To add a format:

1. implement its extractor and dispatch in `research_agent/research_subagent/utils/content_extractors.py`;
2. add the suffix to `SUPPORTED_DOC_SUFFIXES` in `research_agent/research_subagent/utils/knowledge_filesystem.py`;
3. update the `read_docs_folder` tool description and prompt guidance if selection behavior changes;
4. add extraction and safety-limit coverage to `tests/test_tools.py`;
5. run relevant upload and ingestion suites when the new format crosses those APIs.

Context controls include `MAX_GLOB_DEPTH`, `MAX_FILES_TO_READ`, `MAX_TOTAL_SIZE_MB`, `MAX_INLINE_FILE_CHARS`, preview limits, and section chunk limits. Change defaults and documentation together, preserve `specific_files` as the targeted large-folder path, and test configured-folder containment before widening filesystem behavior.

## Complete the extension

- Add a failing focused test for the new contract before implementation.
- Update every registration and public export required by the chosen extension point.
- Keep prompt guidance synchronized with actual tool ownership and configured limits.
- Run the focused tests above, then use [Test and check changes](testing.md) to select broader verification.
- Update configuration, usage, or evaluation guides only when user-facing behavior changes.

## Related documentation

- [Architecture overview](../architecture/overview.md)
- [Test and check changes](testing.md)
- [Validate prompt changes](prompt-validation.md)
- [Configuration](../guides/configuration.md)
- [Use skills in the UI](../guides/skills.md)
- [Use the research interfaces](../getting-started/usage.md)
- [Evaluate research quality and regressions](../guides/evaluation.md)
- [Code ingestion](../architecture/code-ingestion.md)
