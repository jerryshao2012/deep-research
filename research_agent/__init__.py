"""Deep Research Agent Example.

This module demonstrates building a research agent using the deepagents package
with custom tools for web search and strategic thinking.
"""

from research_agent.prompts import (
    RESEARCHER_INSTRUCTIONS,
    RESEARCH_WORKFLOW_INSTRUCTIONS,
    SUBAGENT_DELEGATION_INSTRUCTIONS,
)
from research_agent.tools import (
    think_tool,
    ls,
    glob,
    read_file,
    read_docs_folder,
    tavily_search,
    fetch_webpage_content,
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
