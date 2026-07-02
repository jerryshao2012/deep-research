# Test Suite: Prompts Validation

**File**: `tests/test_prompts_validation.py`

This comprehensive test suite validates the enhanced research prompts and instructions to ensure they contain clear guidance on:
- Tool descriptions
- Delegation strategy
- Hard limits
- Think_tool usage
- Report writing guidelines
- Execution rules

---

## Test Classes & Coverage

### 1. **TestResearcherInstructionsToolDescriptions** (6 tests)
Validates that RESEARCHER_INSTRUCTIONS documents all required tools:
- ✅ `tavily_search` tool is documented
- ✅ `fetch_webpage_content` tool is documented
- ✅ `think_tool` is documented with CRITICAL emphasis
- ✅ Dedicated "Available Research Tools" section exists
- ✅ Each tool has clear purpose description
- ✅ Tool descriptions are descriptive and helpful

**Coverage**: Tool documentation completeness

---

### 2. **TestDelegationStrategy** (7 tests)
Validates delegation strategy documentation:
- ✅ DEFAULT strategy section exists and explains single-agent approach
- ✅ Examples provided for single-agent queries (quantum computing, etc.)
- ✅ Parallel execution guidance documented
- ✅ Concrete comparison examples provided (OpenAI vs Anthropic, Python vs JavaScript)
- ✅ Key Principles section exists
- ✅ Warnings against premature decomposition
- ✅ Parallel execution limits are documented

**Coverage**: Delegation strategy clarity and examples

---

### 3. **TestHardLimits** (7 tests)
Validates explicit hard limits documentation:
- ✅ Hard Limits section exists in RESEARCHER_INSTRUCTIONS
- ✅ Search tool budgets documented (2-3 for simple, 5 for complex)
- ✅ Maximum search specifications
- ✅ Stopping criteria clearly documented
- ✅ Minimum relevant sources specified (3+)
- ✅ Warning against duplicate searches
- ✅ Research iteration limits in delegation instructions

**Coverage**: Hard limits completeness and clarity

---

### 4. **TestThinkToolGuidance** (6 tests)
Validates think_tool usage guidance:
- ✅ Think_tool marked as CRITICAL
- ✅ "When to use" guidance provided
- ✅ Reflection guidance documented
- ✅ Gap assessment mentioned in reflection
- ✅ Quality evaluation mentioned
- ✅ Strategic decision-making guidance

**Coverage**: Think_tool documentation quality

---

### 5. **TestInstructionsCohesion** (5 tests)
Validates overall consistency across all instructions:
- ✅ Tool mentions consistent across sections
- ✅ All instruction sections are substantial (>100 chars)
- ✅ Consistent markdown formatting
- ✅ Limit placeholders referenced correctly
- ✅ No incomplete placeholders

**Coverage**: Cross-document consistency

---

### 6. **TestReportWritingGuidelines** (4 tests)
Validates report writing guidelines:
- ✅ Report Writing Guidelines section exists
- ✅ Citation format specified ([1], [2], etc.)
- ✅ Structure patterns documented (comparison, list, summary)
- ✅ Self-referential language warnings

**Coverage**: Output format guidance

---

### 7. **TestExecutionRules** (3 tests)
Validates critical execution rules:
- ✅ Rule: Never ask user for results
- ✅ Rule: Immediate action (don't pause for narrative)
- ✅ Rule: Always complete tasks

**Coverage**: Agent behavior constraints

---

## Running the Tests

### Run all prompt validation tests:
```bash
cd deep_research
uv run pytest tests/test_prompts_validation.py -v
```

### Run specific test class:
```bash
uv run pytest tests/test_prompts_validation.py::TestDelegationStrategy -v
```

### Run specific test:
```bash
uv run pytest tests/test_prompts_validation.py::TestHardLimits::test_hard_limits_document_search_tool_budgets -v
```

### Run with coverage:
```bash
uv run pytest tests/test_prompts_validation.py --cov=research_agent.prompts --cov-report=term-missing
```

---

## Test Statistics

- **Total Tests**: 38
- **Test Classes**: 7
- **Coverage Areas**: 
  - Tool descriptions
  - Delegation strategy
  - Hard limits
  - Think_tool guidance
  - Instructions cohesion
  - Report writing guidelines
  - Execution rules

---

## Key Assertions Validated

| Category | Key Validations |
|----------|-----------------|
| **Tools** | tavily_search, fetch_webpage_content, think_tool documented |
| **Delegation** | Single-agent default, parallel for comparisons, key principles |
| **Hard Limits** | Search budgets, stop criteria, source requirements |
| **Think_tool** | When to use, reflection checklist, gap/quality/decision guidance |
| **Consistency** | Cross-document alignment, proper formatting, no incomplete placeholders |
| **Guidelines** | Report structure, citation format, no self-referential language |
| **Execution** | Never ask user, immediate action, complete tasks |

---

## Integration with CI/CD

This test suite can be integrated into your CI/CD pipeline:

```bash
# In your test workflow
pytest tests/test_prompts_validation.py -v --junit-xml=results.xml
```

The tests will automatically validate that prompt improvements are maintained as the codebase evolves.
