"""Deep Research Agent Example.

This module demonstrates building a research agent using the deepagents package
with custom tools for web search and strategic thinking.
"""

from research_agent.research_subagent.prompts import (
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    RESEARCHER_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)
from research_agent.research_subagent.tools import (
    fetch_webpage_content,
    glob,
    ls,
    read_docs_folder,
    read_file,
    tavily_search,
    think_tool,
)

__all__ = [
    "think_tool",
    "ls",
    "glob",
    "read_file",
    "read_docs_folder",
    "tavily_search",
    "fetch_webpage_content",
    "RESEARCHER_INSTRUCTIONS",
    "RESEARCH_WORKFLOW_INSTRUCTIONS",
    "SUBAGENT_DELEGATION_INSTRUCTIONS",
]
