import { describe, expect, it } from 'vitest';

import {
  AGENT_FORM_MODE_EDIT,
  THINKING_EFFORT_OPTIONS,
  agentIdValidationError,
  buildAgentTargetCatalog,
  createAgentFormValues,
  effortOptionsForReasoning,
  normalizeAgentForm,
  reasoningForModelValue,
} from '../agentForm.js';

describe('createAgentFormValues', () => {
  it('creates default values for a new agent form', () => {
    expect(createAgentFormValues()).toEqual({
      id: '',
      name: '',
      model: '',
      fallback_models: [],
      workspace: '',
      root_project_id: null,
      temperature: '',
      thinking_effort: '',
      memory_prompt_mode: 'agent_user',
      tool_access: { mode: 'all' },
      allowed_skills: ['*'],
      excluded_skills: [],
      tools: {},
      compaction_policy: null,
      custom_system_prompt_enabled: false,
    });
  });

  it.each([
    [
      'the raw config, not the baked values',
      {
        config: {
          model: '',
          fallback_models: [],
          temperature: null,
          thinking_effort: null,
        },
      },
      { model: '', fallback_models: [], temperature: '', thinking_effort: '' },
    ],
    [
      'the top-level values without a config block',
      {},
      {
        model: 'openai/gpt-5.2',
        fallback_models: ['openai/gpt-5.2-mini'],
        temperature: '0.7',
        thinking_effort: 'high',
      },
    ],
  ])('seeds the inheritable fields from %s', (_label, extra, expected) => {
    const values = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-5.2',
      fallback_models: ['openai/gpt-5.2-mini'],
      temperature: 0.7,
      thinking_effort: 'high',
      ...extra,
    });

    expect(values).toMatchObject({ ...expected, name: 'Coder' });
  });

  it('maps access and prompt fields without reinterpreting legacy shapes', () => {
    expect(
      createAgentFormValues({
        temperature: 0.2,
        memory_prompt_mode: 'agent',
        tool_access: { mode: 'selected', allowed: ['read', 'write'] },
        allowed_skills: ['debugging'],
        custom_system_prompt_enabled: true,
      }),
    ).toMatchObject({
      temperature: '0.2',
      memory_prompt_mode: 'agent',
      tool_access: { mode: 'selected', allowed: ['read', 'write'] },
      allowed_skills: ['debugging'],
      custom_system_prompt_enabled: true,
    });
    // Memory prompt visibility and a Memory Tool denial stay independent.
    expect(
      createAgentFormValues({
        memory_prompt_mode: 'agent_user',
        tool_access: { mode: 'all', denied: ['memory'] },
      }),
    ).toMatchObject({
      memory_prompt_mode: 'agent_user',
      tool_access: { mode: 'all', denied: ['memory'] },
    });
    expect(
      createAgentFormValues({ allowed_skills: 'debugging\nctx7' })
        .allowed_skills,
    ).toEqual(['*']);
  });
});

describe('normalizeAgentForm', () => {
  it('normalizes create payloads with trimmed scalar fields and array-based access lists', () => {
    const result = normalizeAgentForm({
      id: ' coder ',
      name: ' Coder ',
      model: ' openai/gpt-4.1 ',
      fallback_models: [' openai/gpt-4.1-mini ', ' '],
      workspace: ' C:/workspace-coder ',
      root_project_id: 'vbot',
      temperature: '0.25',
      thinking_effort: ' low ',
      memory_prompt_mode: ' off ',
      tool_access: {
        mode: 'selected',
        allowed: [' read ', '', 'write '],
      },
      allowed_skills: [' debugging ', ''],
      excluded_skills: [' ctx7 ', ''],
      tools: {
        subagent: { allowed_agents: [' worker ', 'builder@vbot'] },
      },
      custom_system_prompt_enabled: true,
    });

    expect(result.isValid).toBe(true);
    // Workspace and Project are edit-only; create payloads omit them.
    expect(result.payload).toEqual({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-4.1',
      fallback_models: ['openai/gpt-4.1-mini'],
      temperature: 0.25,
      thinking_effort: 'low',
      memory_prompt_mode: 'off',
      tool_access: { mode: 'selected', allowed: ['read', 'write'] },
      allowed_skills: ['debugging'],
      excluded_skills: ['ctx7'],
      tools: {
        subagent: { allowed_agents: ['worker', 'builder@vbot'] },
      },
      compaction_policy: null,
      custom_system_prompt_enabled: true,
    });
  });

  it.each([
    ['', 'required'],
    ['../unsafe', 'invalid_id'],
    ['minimal', ''],
  ])('validates Agent id %j as %j for rename and create', (id, error) => {
    expect(agentIdValidationError(id)).toBe(error);
    const result = normalizeAgentForm({ id, name: '' });
    expect(result.errors).toEqual(error ? { id: error } : {});
    expect(result.isValid).toBe(!error);
    expect(result.payload.id).toBe(id);
    expect(result.payload).not.toHaveProperty('name');
  });

  it.each([
    ['0,25', 0.25, {}],
    ['', null, {}],
    ['warm', null, { temperature: 'invalid_number' }],
  ])('parses temperature %j as %j', (temperature, expected, errors) => {
    const result = normalizeAgentForm({
      id: 'coder',
      temperature,
      thinking_effort: '',
    });

    expect(result.errors).toEqual(errors);
    expect(result.payload).toMatchObject({
      temperature: expected,
      thinking_effort: null,
      memory_prompt_mode: 'agent_user',
    });
  });

  it.each([
    ['all Tools with a Memory denial', { mode: 'all', denied: ['memory'] }],
    ['no Tools', { mode: 'none' }],
  ])('round-trips %s explicitly', (_label, toolAccess) => {
    const formValues = createAgentFormValues({ tool_access: toolAccess });
    const result = normalizeAgentForm({
      id: 'coder',
      tool_access: formValues.tool_access,
    });

    expect(formValues.tool_access).toEqual(toolAccess);
    expect(result.payload.tool_access).toEqual(toolAccess);
  });

  it('does not reinterpret the retired allowed_tools field or string Skill lists', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      allowed_tools: 'read\nwrite',
      allowed_skills: 'debugging\nctx7',
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.tool_access).toEqual({ mode: 'all' });
    expect(result.payload.allowed_skills).toEqual(['*']);
    expect(result.payload).not.toHaveProperty('allowed_tools');
  });

  it('keeps id create-only and sends empty name and workspace in full edit payloads', () => {
    const result = normalizeAgentForm(
      { id: 'coder', name: '', workspace: '' },
      { mode: AGENT_FORM_MODE_EDIT },
    );

    expect(result.isValid).toBe(true);
    expect(result.errors).toEqual({});
    expect(result.payload).toMatchObject({
      id: 'coder',
      name: '',
      workspace: '',
      root_project_id: null,
      tools: {},
    });
  });

  it('sends only the id and the changed fields when editing against a baseline', () => {
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-5.2',
      fallback_models: ['openai/gpt-5.2-mini'],
      workspace: 'C:/workspace-coder',
      temperature: 0.2,
      thinking_effort: 'high',
      memory_prompt_mode: 'agent_user',
      tool_access: { mode: 'all' },
      allowed_skills: ['*'],
      custom_system_prompt_enabled: false,
    });
    const edit = (change) =>
      normalizeAgentForm(
        { ...initialValues, ...change },
        { mode: AGENT_FORM_MODE_EDIT, initialValues },
      ).payload;
    const change = {
      name: 'Coder Prime',
      workspace: 'D:/workspace-coder',
      root_project_id: 'vbot',
      custom_system_prompt_enabled: true,
      memory_prompt_mode: 'agent',
      excluded_skills: ['debugging'],
    };

    expect(edit({})).toEqual({ id: 'coder' });
    expect(edit(change)).toEqual({ id: 'coder', ...change });
  });
});

describe('buildAgentTargetCatalog', () => {
  it('builds canonical Identity and Project Agent target addresses', () => {
    expect(
      buildAgentTargetCatalog({
        identityAgents: [
          { id: 'alpha', name: 'Alpha' },
          { id: 'worker', name: 'Worker' },
        ],
        projectTeams: [
          {
            projectId: 'vbot',
            displayName: 'vBot',
            team: [{ agent_id: 'builder', display_name: 'Builder' }],
          },
        ],
      }),
    ).toEqual([
      { name: 'alpha', displayName: 'Alpha', kind: 'identity' },
      { name: 'worker', displayName: 'Worker', kind: 'identity' },
      {
        name: 'builder@vbot',
        displayName: 'Builder',
        kind: 'project',
        projectId: 'vbot',
        projectName: 'vBot',
      },
    ]);
  });
});

describe('thinking-effort gating', () => {
  const reasoning = { supported: true, levels: ['high', 'xhigh'] };
  const models = [{ id: 'openai/gpt-5.2', capabilities: { reasoning } }];

  it('reads the reasoning block by canonical model id', () => {
    expect(reasoningForModelValue('openai/gpt-5.2', models)).toEqual(reasoning);
    expect(reasoningForModelValue('openai/gpt-5.2::api-key', models)).toEqual(
      reasoning,
    );
    expect(reasoningForModelValue('', models)).toBeNull();
    expect(reasoningForModelValue('unknown/model', models)).toBeNull();
  });

  it('keeps the full ladder without a published one and narrows to it otherwise', () => {
    expect(effortOptionsForReasoning(null)).toEqual(THINKING_EFFORT_OPTIONS);
    expect(effortOptionsForReasoning({ levels: [] })).toEqual(
      THINKING_EFFORT_OPTIONS,
    );
    expect(effortOptionsForReasoning(reasoning)).toEqual([
      '',
      'none',
      'high',
      'xhigh',
    ]);
  });
});
