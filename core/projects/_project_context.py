"""Resolved Agent instructions and the Project and Skill scopes used by prompts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.projects._runtime_agent import ConfigAgent, RuntimeAgent
from core.projects.projects import ProjectError

if TYPE_CHECKING:
    from core.projects.projects import Project
    from core.projects.store import ProjectStore


def runtime_agent_body(agent: RuntimeAgent) -> str:
    """Return the verbatim prompt body of a runtime agent, or ``""``.

    The single seam that maps the resolver's two agent forms onto the prompt
    builder's ``agent_body`` parameter: a :class:`ConfigAgent` carries an imported
    body, an identity ``Agent`` carries none. Keeping this here (not in the prompt
    domain) lets prompt assembly stay on its Protocols without importing
    ``ConfigAgent`` or probing types — the chat loop calls this on the agent it
    already resolved and hands the result over as an explicit argument.
    """
    if isinstance(agent, ConfigAgent):
        return agent.body
    # Owner-managed temporary Agents carry their reviewed profile instructions
    # in the protected Session binding rather than an Identity workspace.
    instructions = getattr(agent, "instructions", "")
    return instructions if isinstance(instructions, str) else ""


def resolve_prompt_project(
    projects: ProjectStore, working_project_id: str | None
) -> Project | None:
    """Return the project whose auto-load files belong in this run's system prompt.

    The one policy shared by the chat loop and the prompt-preview RPC, so the
    preview can never drift from what a run actually sends:

    - ``working_project_id`` set → the Project the Session works in.
    - ``working_project_id is None`` → no Project context.

    Kept beside :func:`runtime_agent_body` for the same reason: the chat loop and
    the RPC call it with the already-resolved working Project, so prompt assembly
    never learns working-Project or Session-addressing policy itself.
    """
    if working_project_id is not None:
        project = projects.get(working_project_id)
        if not Path(project.cwd).is_dir():
            raise ProjectError(f"Project repository is unavailable: {project.cwd}")
        return replace(project, auto_load=project.instruction_files)
    return None


def resolve_skill_scope(
    project_id: str | None, prompt_project: Project | None, agent: RuntimeAgent
) -> tuple[str | None, str | None]:
    """Return ``(skill_project_id, identity_agent_id)`` for a run's skill pool.

    The one skill-scoping policy shared by the chat loop, the prompt-preview RPC,
    and ``$``-autocomplete, so no surface can drift from the pool a run actually
    activates against. ``prompt_project`` is the already-resolved working Project
    from :func:`resolve_prompt_project` (pure — no second store lookup here), and
    ``agent`` the resolved runtime Agent:

    - ``skill_project_id`` — the effective skill project: the run's own project,
      or, for an identity Session working in a Project (``project_id is None``),
      that Project; else ``None``.
    - ``identity_agent_id`` — the agent's private-skill layer applies to identity
      runs only: a project run executes a config agent whose project-local slug
      must never resolve a same-named identity agent's private home (private
      skills bypass the project skill whitelist as always-allowed). It names the
      Agent whose Skills the run works on (``skill_subject_id``): the Agent
      itself, or the subject of a Librarian Session.
    """
    if project_id is not None:
        return project_id, None
    from core.agents import skill_subject_id

    working_project_id = prompt_project.project_id if prompt_project is not None else None
    return working_project_id, skill_subject_id(agent)
