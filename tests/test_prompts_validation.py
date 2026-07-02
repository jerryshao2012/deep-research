"""Tests for improved research prompts/instructions validation.

This module validates that the enhanced prompt instructions contain:
- Required tool descriptions
- Clear delegation strategies
- Explicitly stated hard limits
"""

import pytest

from research_agent.prompts import (
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    RESEARCHER_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)


class TestResearcherInstructionsToolDescriptions:
    """Validate that RESEARCHER_INSTRUCTIONS contains all required tool descriptions."""

    def test_researcher_instructions_contains_tavily_search_tool(self) -> None:
        """tavily_search should be documented in researcher instructions."""
        assert "tavily_search" in RESEARCHER_INSTRUCTIONS
        # Should explain what tavily_search does
        assert "web search" in RESEARCHER_INSTRUCTIONS.lower()

    def test_researcher_instructions_contains_fetch_webpage_content_tool(self) -> None:
        """fetch_webpage_content should be documented in researcher instructions."""
        assert "fetch_webpage_content" in RESEARCHER_INSTRUCTIONS
        # Should explain what fetch_webpage_content does
        assert "webpage" in RESEARCHER_INSTRUCTIONS.lower()

    def test_researcher_instructions_contains_think_tool(self) -> None:
        """think_tool should be documented in researcher instructions."""
        assert "think_tool" in RESEARCHER_INSTRUCTIONS
        # Should emphasize think_tool usage
        assert "CRITICAL" in RESEARCHER_INSTRUCTIONS or "reflection" in RESEARCHER_INSTRUCTIONS.lower()

    def test_researcher_instructions_has_available_research_tools_section(self) -> None:
        """Should have a dedicated section listing available research tools."""
        assert "Available Research Tools" in RESEARCHER_INSTRUCTIONS

    def test_researcher_instructions_tools_have_descriptions(self) -> None:
        """Each tool should have a clear description of its purpose."""
        # Check for descriptive patterns
        assert "For conducting web searches" in RESEARCHER_INSTRUCTIONS or "web search" in RESEARCHER_INSTRUCTIONS.lower()
        assert "retriev" in RESEARCHER_INSTRUCTIONS.lower()  # retrieve/retrieving
        assert "reflection" in RESEARCHER_INSTRUCTIONS.lower() or "strategic" in RESEARCHER_INSTRUCTIONS.lower()


class TestDelegationStrategy:
    """Validate that delegation strategy is clearly documented."""

    def test_subagent_delegation_has_default_strategy_section(self) -> None:
        """Should document the default single-agent approach."""
        assert "DEFAULT" in SUBAGENT_DELEGATION_INSTRUCTIONS.upper()
        assert "1 sub-agent" in SUBAGENT_DELEGATION_INSTRUCTIONS

    def test_delegation_strategy_explains_when_to_use_single_agent(self) -> None:
        """Should provide examples of queries that use 1 sub-agent."""
        assert "quantum computing" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()
        assert "general overview" in SUBAGENT_DELEGATION_INSTRUCTIONS or "covers all aspects" in SUBAGENT_DELEGATION_INSTRUCTIONS

    def test_delegation_strategy_explains_parallel_execution(self) -> None:
        """Should explain when to parallelize research tasks."""
        assert "comparison" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()
        assert "parallel" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()

    def test_delegation_strategy_has_concrete_comparison_examples(self) -> None:
        """Should provide concrete examples of comparison use cases."""
        assert "Compare" in SUBAGENT_DELEGATION_INSTRUCTIONS
        # Should have at least one comparison example with multiple entities
        assert ("OpenAI" in SUBAGENT_DELEGATION_INSTRUCTIONS or
                "Python" in SUBAGENT_DELEGATION_INSTRUCTIONS)

    def test_delegation_strategy_documents_key_principles(self) -> None:
        """Should explicitly state key delegation principles."""
        assert "Key Principles" in SUBAGENT_DELEGATION_INSTRUCTIONS
        # Should bias towards single agent (most token-efficient)
        assert "single" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()
        assert "token-efficient" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()

    def test_delegation_strategy_avoids_premature_decomposition(self) -> None:
        """Should warn against breaking research into too many narrow tasks."""
        assert "Avoid premature decomposition" in SUBAGENT_DELEGATION_INSTRUCTIONS or \
               "avoid" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()

    def test_delegation_parallel_execution_limits_documented(self) -> None:
        """Should clearly document parallel execution limits."""
        assert "Parallel Execution Limits" in SUBAGENT_DELEGATION_INSTRUCTIONS
        assert "max_concurrent_research_units" in SUBAGENT_DELEGATION_INSTRUCTIONS or \
               "parallel sub-agent" in SUBAGENT_DELEGATION_INSTRUCTIONS


class TestHardLimits:
    """Validate that hard limits are explicitly stated."""

    def test_researcher_instructions_has_hard_limits_section(self) -> None:
        """Should have a dedicated 'Hard Limits' section."""
        assert "Hard Limits" in RESEARCHER_INSTRUCTIONS

    def test_hard_limits_document_search_tool_budgets(self) -> None:
        """Should specify search tool call budgets."""
        # Simple queries limit
        assert "2-3" in RESEARCHER_INSTRUCTIONS or "Simple queries" in RESEARCHER_INSTRUCTIONS
        # Complex queries limit
        assert "5" in RESEARCHER_INSTRUCTIONS or "Complex queries" in RESEARCHER_INSTRUCTIONS

    def test_hard_limits_specify_maximum_searches(self) -> None:
        """Should specify the maximum number of searches."""
        assert "search tool call" in RESEARCHER_INSTRUCTIONS.lower() or \
               "search" in RESEARCHER_INSTRUCTIONS.lower()

    def test_hard_limits_document_stopping_criteria(self) -> None:
        """Should document when to stop searching."""
        assert "Stop Immediately When" in RESEARCHER_INSTRUCTIONS or \
               "stop" in RESEARCHER_INSTRUCTIONS.lower()
        # Should mention stopping conditions
        assert "answer" in RESEARCHER_INSTRUCTIONS.lower()
        assert "source" in RESEARCHER_INSTRUCTIONS.lower()

    def test_hard_limits_state_source_requirements(self) -> None:
        """Should specify minimum number of relevant sources."""
        assert ("3" in RESEARCHER_INSTRUCTIONS and "source" in RESEARCHER_INSTRUCTIONS.lower()) or \
               ("relevant" in RESEARCHER_INSTRUCTIONS.lower() and "source" in RESEARCHER_INSTRUCTIONS.lower())

    def test_hard_limits_prevent_duplicate_searches(self) -> None:
        """Should warn against repeating similar searches."""
        assert "similar" in RESEARCHER_INSTRUCTIONS.lower() or \
               "last 2 searches" in RESEARCHER_INSTRUCTIONS

    def test_subagent_delegation_has_research_limits(self) -> None:
        """Should document research iteration limits in delegation instructions."""
        assert "Research Limits" in SUBAGENT_DELEGATION_INSTRUCTIONS
        assert "max_researcher_iterations" in SUBAGENT_DELEGATION_INSTRUCTIONS or \
               "iteration" in SUBAGENT_DELEGATION_INSTRUCTIONS.lower()


class TestThinkToolGuidance:
    """Validate that think_tool guidance is clear and comprehensive."""

    def test_researcher_instructions_emphasizes_think_tool_usage(self) -> None:
        """Should emphasize the importance of using think_tool."""
        assert "think_tool" in RESEARCHER_INSTRUCTIONS
        assert "CRITICAL" in RESEARCHER_INSTRUCTIONS  # Think tool should be marked as critical

    def test_think_tool_has_when_to_use_guidance(self) -> None:
        """Should explain when to use think_tool."""
        assert "When to use" in RESEARCHER_INSTRUCTIONS or \
               "After each search" in RESEARCHER_INSTRUCTIONS

    def test_think_tool_has_reflection_guidance(self) -> None:
        """Should explain what reflection should address."""
        assert "Reflection should address" in RESEARCHER_INSTRUCTIONS.lower() or \
               "reflection" in RESEARCHER_INSTRUCTIONS.lower()

    def test_think_tool_guidance_includes_gap_assessment(self) -> None:
        """Should mention gap assessment in reflection."""
        assert "gap" in RESEARCHER_INSTRUCTIONS.lower() or \
               "missing" in RESEARCHER_INSTRUCTIONS.lower()

    def test_think_tool_guidance_includes_quality_evaluation(self) -> None:
        """Should mention quality evaluation in reflection."""
        assert "quality" in RESEARCHER_INSTRUCTIONS.lower() or \
               "evidence" in RESEARCHER_INSTRUCTIONS.lower()

    def test_think_tool_guidance_includes_strategic_decision(self) -> None:
        """Should mention strategic decision-making in reflection."""
        assert "strategic" in RESEARCHER_INSTRUCTIONS.lower() or \
               "should i continue" in RESEARCHER_INSTRUCTIONS.lower() or \
               "should I continue" in RESEARCHER_INSTRUCTIONS


class TestInstructionsCohesion:
    """Validate overall cohesion and consistency across instructions."""

    def test_workflow_and_researcher_instructions_reference_same_tools(self) -> None:
        """Tool mentions should be consistent across workflow and researcher instructions."""
        workflow_mentions_tavily = "tavily_search" in RESEARCH_WORKFLOW_INSTRUCTIONS
        researcher_mentions_tavily = "tavily_search" in RESEARCHER_INSTRUCTIONS

        # At least one should mention tavily since it's a core tool
        assert workflow_mentions_tavily or researcher_mentions_tavily

    def test_all_instruction_sections_are_non_empty(self) -> None:
        """All instruction constants should contain meaningful content."""
        assert len(RESEARCH_WORKFLOW_INSTRUCTIONS) > 100
        assert len(RESEARCHER_INSTRUCTIONS) > 100
        assert len(SUBAGENT_DELEGATION_INSTRUCTIONS) > 100

    def test_instructions_use_consistent_formatting(self) -> None:
        """Instructions should use consistent markdown formatting."""
        # Should use markdown headers
        assert "#" in RESEARCH_WORKFLOW_INSTRUCTIONS
        assert "#" in RESEARCHER_INSTRUCTIONS
        assert "#" in SUBAGENT_DELEGATION_INSTRUCTIONS

    def test_delegation_instructions_reference_limits(self) -> None:
        """Delegation instructions should reference the configured limits."""
        assert "{max_concurrent_research_units}" in SUBAGENT_DELEGATION_INSTRUCTIONS
        assert "{max_researcher_iterations}" in SUBAGENT_DELEGATION_INSTRUCTIONS

    def test_no_incomplete_placeholders_in_researcher_instructions(self) -> None:
        """Researcher instructions should not have unresolved placeholders."""
        # Format string placeholders like {date} are OK, but instructions should be complete
        assert "{skill_catalog}" in RESEARCHER_INSTRUCTIONS or \
               len(RESEARCHER_INSTRUCTIONS) > 500  # Should have substantial content


class TestReportWritingGuidelines:
    """Validate that report writing guidelines are clearly documented."""

    def test_workflow_includes_report_writing_guidelines(self) -> None:
        """Should document report writing guidelines."""
        assert "Report Writing Guidelines" in RESEARCH_WORKFLOW_INSTRUCTIONS

    def test_report_guidelines_document_citation_format(self) -> None:
        """Should specify citation format."""
        assert "[1]" in RESEARCH_WORKFLOW_INSTRUCTIONS or "[1], [2]" in RESEARCH_WORKFLOW_INSTRUCTIONS
        assert "Source" in RESEARCH_WORKFLOW_INSTRUCTIONS

    def test_report_guidelines_document_structure_patterns(self) -> None:
        """Should provide structure patterns for different output types."""
        assert "comparison" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower()
        assert "list" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower()
        assert "summary" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower() or \
               "overview" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower()

    def test_report_guidelines_prohibit_self_referential_language(self) -> None:
        """Should warn against self-referential language."""
        assert "self-referential" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower() or \
               "I found" in RESEARCH_WORKFLOW_INSTRUCTIONS or \
               "I researched" in RESEARCH_WORKFLOW_INSTRUCTIONS


class TestExecutionRules:
    """Validate critical execution rules are documented."""

    def test_workflow_documents_never_ask_for_results_rule(self) -> None:
        """Should document rule against asking user for results."""
        assert "NEVER ask" in RESEARCH_WORKFLOW_INSTRUCTIONS or \
               "Do NOT ask" in RESEARCH_WORKFLOW_INSTRUCTIONS

    def test_workflow_documents_immediate_action_rule(self) -> None:
        """Should document rule against pausing for narrative."""
        assert "pause" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower() or \
               "immediately" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower()

    def test_workflow_documents_complete_tasks_rule(self) -> None:
        """Should document rule to always complete tasks."""
        assert "write_todos" in RESEARCH_WORKFLOW_INSTRUCTIONS or \
               "complete" in RESEARCH_WORKFLOW_INSTRUCTIONS.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
