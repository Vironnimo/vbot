import {
  rpc,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  ApiClientError,
  requireNonEmptyString,
  requirePlainObject,
} from './transport.js';

export function listAgents(options = {}) {
  return rpc('agent.list', {}, options);
}

export function reorderAgents(agentIds, expectedRevision, options = {}) {
  if (
    !Array.isArray(agentIds) ||
    agentIds.some((agentId) => typeof agentId !== 'string' || !agentId)
  ) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent order must be a list of non-empty ids',
      { method: 'agent.reorder' },
    );
  }
  if (!Number.isInteger(expectedRevision) || expectedRevision < 0) {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Agent order revision must be a non-negative integer',
      { method: 'agent.reorder' },
    );
  }
  return rpc(
    'agent.reorder',
    { agent_ids: agentIds, expected_revision: expectedRevision },
    options,
  );
}

export function getAgent(id, options = {}) {
  requireNonEmptyString(id, 'Agent id must be a non-empty string', 'agent.get');
  return rpc('agent.get', { id }, options);
}

export function createAgent(params = {}, options = {}) {
  requirePlainObject(params, 'Agent payload must be an object', 'agent.create');
  return rpc('agent.create', params, options);
}

export function updateAgent(params = {}, options = {}) {
  requirePlainObject(params, 'Agent payload must be an object', 'agent.update');
  return rpc('agent.update', params, options);
}

export function renameAgent(id, newId, options = {}) {
  requireNonEmptyString(
    id,
    'Agent id must be a non-empty string',
    'agent.rename',
  );
  requireNonEmptyString(
    newId,
    'New agent id must be a non-empty string',
    'agent.rename',
  );
  return rpc('agent.rename', { id, new_id: newId }, options);
}

export function deleteAgent(id, options = {}) {
  requireNonEmptyString(
    id,
    'Agent id must be a non-empty string',
    'agent.delete',
  );
  return rpc('agent.delete', { id }, options);
}

export function listModels(params = {}, options = {}) {
  requirePlainObject(params, 'Model filters must be an object', 'model.list');
  return rpc('model.list', params, options);
}

export function refreshModelDatabase(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Model refresh options must be an object',
    'model.refresh_db',
  );
  return rpc('model.refresh_db', params, options);
}

export function listConnections(options = {}) {
  return rpc('connection.list', {}, options);
}

export function setConnectionEnabled(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Connection update must be an object',
    'connection.set_enabled',
  );
  return rpc('connection.set_enabled', params, options);
}

export function listTools(options = {}) {
  return rpc('tool.list', {}, options);
}

export function listSkills(params = {}, options = {}) {
  requirePlainObject(params, 'Skill filters must be an object', 'skill.list');
  return rpc('skill.list', params, options);
}

export function readSkills(scope, options = {}) {
  requireNonEmptyString(
    scope,
    'Skill scope must be a non-empty string',
    'skill.read',
  );
  return rpc('skill.read', { scope }, options);
}

export function createSkill(params = {}, options = {}) {
  requirePlainObject(params, 'Skill payload must be an object', 'skill.create');
  return rpc('skill.create', params, options);
}

export function updateSkill(params = {}, options = {}) {
  requirePlainObject(params, 'Skill payload must be an object', 'skill.update');
  return rpc('skill.update', params, options);
}

export function deleteSkill(scope, name, options = {}) {
  requireNonEmptyString(
    scope,
    'Skill scope must be a non-empty string',
    'skill.delete',
  );
  requireNonEmptyString(
    name,
    'Skill name must be a non-empty string',
    'skill.delete',
  );
  return rpc('skill.delete', { scope, name }, options);
}

export function inspectSkill(id, options = {}) {
  requireNonEmptyString(
    id,
    'Skill id must be a non-empty string',
    'skill.inspect',
  );
  return rpc('skill.inspect', { id }, options);
}

export function skillInventory(options = {}) {
  return rpc('skill.inventory', {}, options);
}

export function setSkillDisabled(name, disabled, options = {}) {
  requireNonEmptyString(
    name,
    'Skill name must be a non-empty string',
    'skill.set_disabled',
  );
  if (typeof disabled !== 'boolean') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Disabled flag must be a boolean',
      { method: 'skill.set_disabled' },
    );
  }
  return rpc('skill.set_disabled', { name, disabled }, options);
}

export function shareSkill(
  agentId,
  name,
  shared,
  receivers = [],
  options = {},
) {
  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'skill.share',
  );
  requireNonEmptyString(
    name,
    'Skill name must be a non-empty string',
    'skill.share',
  );
  if (typeof shared !== 'boolean') {
    throw new ApiClientError(
      RPC_ERROR_INVALID_CLIENT_REQUEST,
      'Shared flag must be a boolean',
      { method: 'skill.share' },
    );
  }
  return rpc(
    'skill.share',
    { agent_id: agentId, name, shared, receivers },
    options,
  );
}

export function listPrompts(params = {}, options = {}) {
  requirePlainObject(params, 'Prompt scope must be an object', 'prompt.list');
  return rpc('prompt.list', params, options);
}

export function updatePromptBlock(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt update must be an object',
    'prompt.update',
  );
  return rpc('prompt.update', params, options);
}

export function resetPromptBlock(params = {}, options = {}) {
  requirePlainObject(params, 'Prompt reset must be an object', 'prompt.reset');
  return rpc('prompt.reset', params, options);
}

export function createPromptBlock(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt block must be an object',
    'prompt.create_block',
  );
  return rpc('prompt.create_block', params, options);
}

export function removePromptBlock(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt block must be an object',
    'prompt.remove_block',
  );
  return rpc('prompt.remove_block', params, options);
}

export function resetPromptLayout(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt scope must be an object',
    'prompt.reset_layout',
  );
  return rpc('prompt.reset_layout', params, options);
}

export function setPromptLayout(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt layout must be an object',
    'prompt.set_layout',
  );
  return rpc('prompt.set_layout', params, options);
}

export function previewPrompt(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Prompt preview must be an object',
    'prompt.preview',
  );
  return rpc('prompt.preview', params, options);
}

export function addProject(params = {}, options = {}) {
  requirePlainObject(
    params,
    'Project payload must be an object',
    'project.add',
  );

  requireNonEmptyString(
    params.cwd,
    'Project cwd must be a non-empty string',
    'project.add',
  );

  return rpc('project.add', params, options);
}

export function listProjects(options = {}) {
  return rpc('project.list', {}, options);
}

// Probe a cwd for per-format agent/skill presence and context files. Called by
// the add dialog while the user types a path; a nonexistent cwd is a success
// with `cwd_exists: false`, never an error.
export function detectProject(cwd, options = {}) {
  requireNonEmptyString(
    cwd,
    'Project cwd must be a non-empty string',
    'project.detect',
  );

  return rpc('project.detect', { cwd }, options);
}

export function showProject(projectId, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.show',
  );

  return rpc('project.show', { project_id: projectId }, options);
}

export function setProject(projectId, changes = {}, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.set',
  );

  requirePlainObject(
    changes,
    'Project changes must be an object',
    'project.set',
  );

  return rpc('project.set', { ...changes, project_id: projectId }, options);
}

export function setOverride(projectId, agentId, field, value, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.set_override',
  );

  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'project.set_override',
  );

  requireNonEmptyString(
    field,
    'Override field must be a non-empty string',
    'project.set_override',
  );

  // A per-agent override (model / temperature / thinking_effort) becomes the top
  // tier of that field's resolution chain for this agent in this project. The value
  // shape is field-specific (a model address string, a number, an effort string);
  // the server validates it against the canonical agent rules.
  return rpc(
    'project.set_override',
    { project_id: projectId, agent_id: agentId, field, value },
    options,
  );
}

export function clearOverride(projectId, agentId, field, options = {}) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.clear_override',
  );

  requireNonEmptyString(
    agentId,
    'Agent id must be a non-empty string',
    'project.clear_override',
  );

  requireNonEmptyString(
    field,
    'Override field must be a non-empty string',
    'project.clear_override',
  );

  // Drop one overridden field for this agent; clearing the agent's last field
  // removes the override entry entirely (server-side). The field falls back through
  // its chain.
  return rpc(
    'project.clear_override',
    { project_id: projectId, agent_id: agentId, field },
    options,
  );
}

export function removeProject(
  projectId,
  copyRootedAgentIdentityFiles = false,
  options = {},
) {
  requireNonEmptyString(
    projectId,
    'Project id must be a non-empty string',
    'project.rm',
  );

  const requestOptions =
    copyRootedAgentIdentityFiles &&
    typeof copyRootedAgentIdentityFiles === 'object'
      ? copyRootedAgentIdentityFiles
      : options;
  const copyFiles =
    typeof copyRootedAgentIdentityFiles === 'boolean'
      ? copyRootedAgentIdentityFiles
      : false;

  return rpc(
    'project.rm',
    {
      project_id: projectId,
      copy_rooted_agent_identity_files: copyFiles,
    },
    requestOptions,
  );
}
