"""Dynamic Skill Registry for loading and managing agent skills.

This module provides a dynamic, directory-based skill registry that enables
a plug-and-play ecosystem where adding a new capability is as simple as
dropping a new folder into the skills/ directory.

Architecture:
- File System: Standardized folder structure with SKILL.md files containing YAML frontmatter
- Skill Registry: Core utility that scans, parses, and holds skills in memory
- Agent Orchestrator: Uses the registry for routing and prompt injection
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from logger_utils import setup_logger
from research_agent.utils.json_utils import robust_json_loads

logger = setup_logger(__name__)


class SkillInfo:
    """Information about a single skill.

    Attributes:
        skill_id: Unique skill identifier (directory name).
        name: Human-readable skill name.
        description: Short description for routing decisions.
        instructions: Full Markdown instructions for the agent.
        path: Filesystem path to the skill directory.
        keywords: List of trigger keywords for matching.
        metadata: Arbitrary frontmatter metadata beyond name/description/keywords.
    """

    def __init__(
            self,
            skill_id: str,
            name: str,
            description: str,
            instructions: str,
            path: Path,
            keywords: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
    ):
        """Initialize a SkillInfo instance.

        Args:
            skill_id: Unique skill identifier (directory name).
            name: Human-readable skill name.
            description: Short description for routing decisions.
            instructions: Full Markdown instructions for the agent.
            path: Filesystem path to the skill directory.
            keywords: List of trigger keywords for matching.
            metadata: Arbitrary frontmatter metadata.
        """
        self.skill_id = skill_id
        self.name = name
        self.description = description
        self.instructions = instructions
        self.path = path
        self.keywords = keywords or []
        self.metadata = metadata or {}

    def to_summary(self) -> dict[str, Any]:
        """Return a summary suitable for routing decisions (minimal tokens)."""
        return {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
        }


class SkillRegistry:
    """Dynamic skill registry that loads skills from a directory structure.

    Supports hot-reloading by checking file modification times on access.
    """

    _FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
    _JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
    _SCHEMA_SECTION_RE = re.compile(r"^## Schema\s*$", re.MULTILINE)
    _RENDER_SPEC_SECTION_RE = re.compile(r"^## Render Spec\s*$", re.MULTILINE)
    _QUALITY_GUIDELINES_SECTION_RE = re.compile(r"^## Quality Guidelines\s*$", re.MULTILINE)
    SUPPORTED_RENDER_TEMPLATES = {"markdown_blocks"}

    def __init__(self, skills_dir: str | Path | list[str | Path] | None = None):
        """Initialize the skill registry.

        Args:
            skills_dir: Path or list of paths to skills directories.
                Defaults to [.deepagents/skills/, docs/.deepagents/skills/].
        """
        base_dir = Path(__file__).resolve().parent.parent.parent
        if skills_dir is None:
            self.skills_dirs: list[Path] = [
                base_dir / ".deepagents" / "skills",
                base_dir / "docs" / ".deepagents" / "skills",
            ]
        elif isinstance(skills_dir, (str, Path)):
            self.skills_dirs = [Path(skills_dir)]
        else:
            self.skills_dirs = [Path(p) for p in skills_dir]

        self._skills: dict[str, SkillInfo] = {}
        self._load_timestamps: dict[str, float] = {}
        self._skill_definitions: dict[str, dict[str, Any]] = {}
        self._skills_ids: set[str] | None = None  # cached; populated by _load_all_skills() at import time
        self._load_all_skills()

    @property
    def SKILL_IDS(self) -> set[str]:
        """Skill IDs auto-discovered from configured skills directories.

        Populated eagerly by ``_load_all_skills()`` at import time so that
        accessing this property during graph execution never triggers
        synchronous filesystem I/O (which ``langgraph dev``'s ``blockbuster``
        would flag as a blocking call).
        """
        if self._skills_ids is not None:
            return self._skills_ids

        # Fallback lazy path (should rarely be needed — _load_all_skills()
        # sets _skills_ids during __init__).  Kept for callers that
        # construct a SkillRegistry without triggering full skill loading.
        self._skills_ids = set(self._skills.keys())
        return self._skills_ids

    def _load_all_skills(self) -> None:
        """Scan and load all skills from the skills directories."""
        loaded_count = 0

        for s_dir in self.skills_dirs:
            if not s_dir.exists():
                continue

            # Iterate through all subdirectories in the skills folder
            for skill_path in sorted(s_dir.iterdir()):
                if not skill_path.is_dir():
                    continue

                skill_file = skill_path / "SKILL.md"
                if not skill_file.exists():
                    continue

                try:
                    parsed_skill = self._parse_skill_file(skill_file)
                    if parsed_skill:
                        # Use the 'name' field from frontmatter as skill_id (not directory name)
                        skill_id = parsed_skill.get("name", skill_path.name)

                        parsed_skill["skill_id"] = skill_id
                        parsed_skill["path"] = skill_path
                        self._skills[skill_id] = SkillInfo(
                            **{k: v for k, v in parsed_skill.items() if k not in ["skill_definition"]})
                        if "skill_definition" in parsed_skill:
                            self._skill_definitions.update(parsed_skill["skill_definition"])
                        self._load_timestamps[skill_id] = skill_file.stat().st_mtime
                        loaded_count += 1
                except Exception as e:
                    logger.error(f"Warning: Failed to load skill from {skill_file}: {e}")
                    continue

        logger.info(f"Loaded {loaded_count} skills across {len(self.skills_dirs)} directories")

        # Pre-cache skill IDs so that SKILL_IDS never triggers lazy disk I/O
        # during graph execution (avoids langgraph dev blockbuster errors).
        self._skills_ids = set(self._skills.keys())

    def _parse_skill_file(self, file_path: Path) -> dict[str, Any] | None:
        """Parse a SKILL.md file, extracting frontmatter and body.

        Args:
            file_path: Path to the SKILL.md file

        Returns:
            Dictionary with skill metadata and instructions, or None if parsing fails
        """
        content = file_path.read_text(encoding="utf-8")

        match = self._FRONTMATTER_RE.match(content)
        if not match:
            logger.info(f"Warning: {file_path} is missing YAML frontmatter.")
            return None

        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as e:
            logger.error(f"Warning: Failed to parse YAML in {file_path}: {e}")
            return None

        body = match.group(2)

        # Extract required fields
        name = frontmatter.get("name")
        description = frontmatter.get("description")

        if not name or not description:
            logger.error(f"Warning: {file_path} missing required 'name' or 'description' in frontmatter")
            return None

        # Extract optional fields
        keywords = frontmatter.get("keywords", [])
        metadata = {k: v for k, v in frontmatter.items() if k not in ["name", "description", "keywords"]}

        instructions, schema = self._extract_schema_block(body, file_path)
        render_spec = self._extract_render_spec(body, file_path)
        render_template = frontmatter.get("render_template")
        if render_template and render_template not in self.SUPPORTED_RENDER_TEMPLATES:
            supported = ", ".join(sorted(self.SUPPORTED_RENDER_TEMPLATES))
            raise ValueError(
                f"Skill file {file_path} uses unsupported render_template '{render_template}'. "
                f"Supported templates: {supported}."
            )

        skill_id = name or file_path.parent.name
        skill_definition = {
            skill_id: {
                "id": skill_id,
                "title": frontmatter.get("title", skill_id.replace("-", " ").title()),
                "description": description.strip(),
                "instructions": instructions,
                "quality_guidelines": self._extract_quality_guidelines(body),
                "schema": schema,
                "render": {"template": render_template, "spec": render_spec} if render_spec else None,
                "defaults": frontmatter.get("defaults", {}),
                "skill_path": str(file_path),
            }
        }

        return {
            "name": name,
            "description": description,
            "instructions": body.strip(),
            "keywords": keywords,
            "metadata": metadata,
            "skill_definition": skill_definition
        }

    def get_all_summaries(self) -> list[dict[str, Any]]:
        """Get summaries of all available skills for routing decisions.

        Returns:
            List of skill summaries with id, name, description, and keywords
        """
        return [skill.to_summary() for skill in self._skills.values()]

    def get_skill_instructions(self, skill_id: str, force_reload: bool = False) -> str | None:
        """Get the full instructions for a specific skill.

        Supports hot-reloading by checking file modification time.

        Args:
            skill_id: The skill identifier (directory name)
            force_reload: If True, always reload from disk

        Returns:
            The skill instructions (markdown body), or None if not found
        """
        # Check if skill exists
        if skill_id not in self._skills:
            return None

        skill_info = self._skills[skill_id]
        skill_file = skill_info.path / "SKILL.md"

        # Hot-reload check: compare file modification time
        if not force_reload:
            current_mtime = skill_file.stat().st_mtime
            cached_mtime = self._load_timestamps.get(skill_id, 0)
            if current_mtime > cached_mtime:
                # File has been modified, reload it
                logger.info(f"Hot-reloading skill: {skill_id}")
                parsed_skill = self._parse_skill_file(skill_file)
                if parsed_skill:
                    parsed_skill["skill_id"] = skill_id
                    parsed_skill["path"] = skill_info.path
                    self._skills[skill_id] = SkillInfo(
                        **{k: v for k, v in parsed_skill.items() if k not in ["skill_definition"]})
                    if "skill_definition" in parsed_skill:
                        self._skill_definitions.update(parsed_skill["skill_definition"])
                    self._load_timestamps[skill_id] = current_mtime
                    return self._skills[skill_id].instructions
                else:
                    logger.warning(f"Warning: Failed to reload skill {skill_id}, using cached version")
                    return skill_info.instructions

        return skill_info.instructions

    def get_skill_info(self, skill_id: str) -> SkillInfo | None:
        """Get full skill information object.

        Args:
            skill_id: The skill identifier

        Returns:
            SkillInfo object or None if not found
        """
        return self._skills.get(skill_id)

    def find_skills_by_keyword(self, query: str) -> list[SkillInfo]:
        """Find skills matching a keyword or search query.

        Args:
            query: Search query (will be matched against keywords and descriptions)

        Returns:
            List of matching SkillInfo objects
        """
        query_lower = query.lower()
        matches = []

        for skill in self._skills.values():
            # Check keywords
            for keyword in skill.keywords:
                if re.search(keyword, query_lower):
                    matches.append(skill)
                    break
            else:
                # Check description if no keyword match
                if query_lower in skill.description.lower():
                    matches.append(skill)

        return matches

    def get_supporting_files(self, skill_id: str) -> list[Path]:
        """Get paths to supporting files in a skill directory.

        This enables lazy loading of supporting files like CSS, templates, etc.

        Args:
            skill_id: The skill identifier

        Returns:
            List of file paths in the skill directory (excluding SKILL.md)
        """
        skill_info = self._skills.get(skill_id)
        if not skill_info:
            return []

        skill_path = skill_info.path
        return [f for f in skill_path.iterdir() if f.is_file() and f.name != "SKILL.md"]

    def read_supporting_file(self, skill_id: str, filename: str) -> str | None:
        """Read a supporting file from a skill directory.

        Args:
            skill_id: The skill identifier
            filename: Name of the file to read

        Returns:
            File contents as string, or None if not found
        """
        skill_info = self._skills.get(skill_id)
        if not skill_info:
            return None

        file_path = skill_info.path / filename
        if not file_path.exists() or not file_path.is_file():
            return None

        try:
            return file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Warning: Failed to read supporting file {file_path}: {e}")
            return None

    def reload_all(self, reload_config: bool = False) -> None:
        """Force reload all skills from disk.

        Args:
            reload_config: If True, also reload the skill configuration file
        """
        self._skills_ids = None
        self._skills.clear()
        self._load_timestamps.clear()
        self._skill_definitions.clear()

        self._load_all_skills()

        logger.info(f"Reloaded {len(self._skills)} skills from {self.skills_dirs}")

    @property
    def skill_ids(self) -> list[str]:
        """Get list of all loaded skill IDs."""
        return list(self._skills.keys())

    @property
    def num_skills(self) -> int:
        """Get number of loaded skills."""
        return len(self._skills)

    def __repr__(self) -> str:
        return f"SkillRegistry(num_skills={self.num_skills}, dirs={self.skills_dirs})"

    def _extract_schema_block(self, body: str, path: Path) -> tuple[str, dict[str, Any] | None]:
        """Extract a JSON schema block from the ``## Schema`` section of a SKILL.md body.

        Args:
            body: The full Markdown body after the YAML frontmatter.
            path: Path to the SKILL.md file (used for error messages).

        Returns:
            A tuple of ``(instructions_without_schema, schema_dict)``, or
            ``(body, None)`` if no ``## Schema`` section is present.

        Raises:
            ValueError: If a ``## Schema`` heading exists but no JSON block
                follows.
        """
        schema_heading = self._SCHEMA_SECTION_RE.search(body)
        if not schema_heading:
            return body.strip(), None

        schema_body = body[schema_heading.end():]
        json_match = self._JSON_BLOCK_RE.search(schema_body)
        if not json_match:
            raise ValueError(f"Skill file {path} is missing a JSON schema block in `## Schema`.")

        schema = robust_json_loads(json_match.group(1))
        instructions = body[:schema_heading.start()].strip()
        return instructions, schema

    def _extract_render_spec(self, body: str, path: Path) -> list[dict[str, Any]] | None:
        """Extract a JSON render spec from the ``## Render Spec`` section of a SKILL.md body.

        Args:
            body: The full Markdown body after the YAML frontmatter.
            path: Path to the SKILL.md file (used for error messages).

        Returns:
            A list of render-spec dicts, or ``None`` if no ``## Render Spec``
            section is present.

        Raises:
            ValueError: If a ``## Render Spec`` heading exists but no valid
                JSON array block follows.
        """
        render_heading = self._RENDER_SPEC_SECTION_RE.search(body)
        if not render_heading:
            return None

        render_body = body[render_heading.end():]
        json_match = self._JSON_BLOCK_RE.search(render_body)
        if not json_match:
            raise ValueError(
                f"Skill file {path} is missing a JSON render spec block in `## Render Spec`."
            )

        render_spec = robust_json_loads(json_match.group(1))
        if not isinstance(render_spec, list):
            raise ValueError(f"Skill file {path} render spec must be a JSON array.")
        return render_spec

    def _extract_quality_guidelines(self, body: str) -> str:
        """Extract the raw Markdown text of the ## Quality Guidelines section, or empty string."""
        guidelines_heading = self._QUALITY_GUIDELINES_SECTION_RE.search(body)
        if not guidelines_heading:
            return ""
        # Grab everything from after the heading to the next ## section (or end of file)
        start = guidelines_heading.end()
        next_section = re.search(r"^## ", body[start:], re.MULTILINE)
        end = (start + next_section.start()) if next_section else len(body)
        return body[start:end].strip()

    def list_skill_ids(self) -> list[str]:
        """List available skill definition ids for structured output."""
        return sorted(self._skill_definitions.keys())

    def get_skill_definition(self, skill_id: str) -> dict[str, Any]:
        """Get one skill definition by id."""
        registry = get_skill_registry()
        try:
            return registry._skill_definitions[skill_id]
        except KeyError as exc:
            available = ", ".join(self.list_skill_ids()) or "(none)"
            raise ValueError(
                f"Unknown skill '{skill_id}'. Available skills: {available}."
            ) from exc

    def format_skill_catalog(self) -> str:
        """Format a skill catalog for injection into agent prompt text.

        Returns:
            A Markdown-formatted string listing each skill's ID, type
            (structured JSON vs. unstructured Markdown), title, and
            description.
        """
        lines = []
        for skill_id in self.list_skill_ids():
            definition = self.get_skill_definition(skill_id)
            has_schema = bool(definition.get("schema"))
            skill_type = "Structured JSON (Requires render_skill_output tool)" if has_schema else "Unstructured Markdown Document"
            lines.append(
                f"- `{skill_id}` [{skill_type}]: {definition['title']} — {definition['description']}"
            )
        return "\n".join(lines)

    def format_skill_quality_guidelines(self) -> str:
        """Aggregate Quality Guidelines from all skill SKILL.md files into a prompt block.

        Only includes skills that have a ``## Quality Guidelines`` section.
        Each skill's guidelines are prefaced with the skill name so the LLM
        knows which skill they apply to.

        Returns:
            A Markdown-formatted string with all quality guidelines, or an
            empty string if no skills define guidelines.
        """
        blocks: list[str] = []
        for skill_id in self.list_skill_ids():
            definition = self.get_skill_definition(skill_id)
            guidelines = definition.get("quality_guidelines", "").strip()
            if guidelines:
                blocks.append(f"### `{skill_id}` Quality Guidelines\n\n{guidelines}")
        if not blocks:
            return ""
        return "\n\n".join(blocks)


# --- Skill Registry Singleton ---
_skill_registry_instance: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry | None:
    """Get or create the skill registry instance."""
    global _skill_registry_instance
    if _skill_registry_instance is None:
        _skill_registry_instance = SkillRegistry()
    return _skill_registry_instance
