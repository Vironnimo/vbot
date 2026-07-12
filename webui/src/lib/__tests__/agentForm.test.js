import { describe, expect, it } from 'vitest';

import {
  AGENT_FORM_MODE_EDIT,
  THINKING_EFFORT_OPTIONS,
  createAgentFormValues,
  effortOptionsForReasoning,
  normalizeAgentForm,
  reasoningForModelValue,
  textToList,
} from '../agentForm.js';

describe('agent form helpers', () => {
  it('creates default values for a new agent form', () => {
    expect(createAgentFormValues()).toEqual({
      id: '',
      name: '',
      model: '',
      fallback_model: '',
      workspace: '',
      root_project_id: null,
      temperature: '',
      thinking_effort: '',
      memory_prompt_mode: 'agent_user',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
      compaction_policy: null,
      custom_system_prompt_enabled: false,
    });
  });

  it('seeds the four inheritable fields from the raw config, not the baked values', () => {
    // The top-level fields carry the baked (default-resolved) values; the four
    // inheritable fields must bind to the raw own values (`agent.config`) so an
    // empty raw value reads as the inherit state.
    const values = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-5.2',
      fallback_model: 'openai/gpt-5.2-mini',
      temperature: 0.7,
      thinking_effort: 'high',
      config: {
        model: '',
        fallback_model: '',
        temperature: null,
        thinking_effort: null,
      },
    });

    expect(values.model).toBe('');
    expect(values.fallback_model).toBe('');
    expect(values.temperature).toBe('');
    expect(values.thinking_effort).toBe('');
    // Non-inheritable fields still come from the top level.
    expect(values.name).toBe('Coder');
  });

  it('falls back to the top-level values when no config block is present', () => {
    const values = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-5.2',
      temperature: 0.7,
      thinking_effort: 'high',
    });

    expect(values.model).toBe('openai/gpt-5.2');
    expect(values.temperature).toBe('0.7');
    expect(values.thinking_effort).toBe('high');
  });

  it('includes Project only in sparse edit payloads', () => {
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      workspace: 'C:/agents/coder',
      root_project_id: null,
    });
    const values = { ...initialValues, root_project_id: 'vbot' };

    const edit = normalizeAgentForm(values, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues,
    });
    const create = normalizeAgentForm(values);

    expect(edit.payload).toEqual({ id: 'coder', root_project_id: 'vbot' });
    expect(create.payload).not.toHaveProperty('root_project_id');
  });

  it('preserves inherit payload semantics when seeding from raw config', () => {
    // A raw-empty inheritable set round-trips to the inherit payload (empty
    // strings / null) with no changed fields against its own baseline.
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      workspace: 'C:/workspace-coder',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
      config: {
        model: '',
        fallback_model: '',
        temperature: null,
        thinking_effort: null,
      },
    });

    const result = normalizeAgentForm(
      { ...initialValues },
      { mode: AGENT_FORM_MODE_EDIT, initialValues },
    );

    expect(result.isValid).toBe(true);
    // Nothing changed, so only the id is present.
    expect(result.payload).toEqual({ id: 'coder' });
  });

  it('maps an agent into editable form values with allowed tools as an array', () => {
    const values = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-4.1',
      fallback_model: 'openai/gpt-4.1-mini',
      workspace: 'C:/workspace-coder',
      temperature: 0.2,
      thinking_effort: 'medium',
      memory_prompt_mode: 'agent',
      allowed_tools: ['read', 'write'],
      allowed_skills: ['debugging'],
      compaction_policy: null,
      custom_system_prompt_enabled: true,
    });

    expect(values.allowed_tools).toEqual(['read', 'write']);
    expect(values.allowed_skills).toEqual(['debugging']);
    expect(values.temperature).toBe('0.2');
    expect(values.memory_prompt_mode).toBe('agent');
    expect(values.custom_system_prompt_enabled).toBe(true);
  });

  it('removes memory from editable allowed tools because memory mode owns it', () => {
    const values = createAgentFormValues({
      allowed_tools: ['read', 'memory', 'write'],
    });

    expect(values.allowed_tools).toEqual(['read', 'write']);
  });

  it('normalizes create payloads with trimmed scalar fields and array-based access lists', () => {
    const result = normalizeAgentForm({
      id: ' coder ',
      name: ' Coder ',
      model: ' openai/gpt-4.1 ',
      fallback_model: ' ',
      workspace: ' C:/workspace-coder ',
      temperature: '0.25',
      thinking_effort: ' low ',
      memory_prompt_mode: ' off ',
      allowed_tools: [' read ', '', 'write '],
      allowed_skills: [' debugging ', ''],
      custom_system_prompt_enabled: true,
    });

    expect(result.isValid).toBe(true);
    expect(result.payload).toEqual({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-4.1',
      fallback_model: '',
      temperature: 0.25,
      thinking_effort: 'low',
      memory_prompt_mode: 'off',
      allowed_tools: ['read', 'write'],
      allowed_skills: ['debugging'],
      compaction_policy: null,
      custom_system_prompt_enabled: true,
    });
    expect(result.payload).not.toHaveProperty('workspace');
  });

  it('accepts a comma decimal separator in temperature', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      temperature: '0,25',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.temperature).toBe(0.25);
  });

  it('removes memory from allowed tool payloads', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      temperature: '0.1',
      allowed_tools: ['read', 'memory'],
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.allowed_tools).toEqual(['read']);
  });

  it('normalizes cleared temperature and thinking effort to null', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      model: '',
      fallback_model: '',
      temperature: '',
      thinking_effort: '',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.temperature).toBeNull();
    expect(result.payload.thinking_effort).toBeNull();
    expect(result.payload.memory_prompt_mode).toBe('agent_user');
  });

  it('round-trips all-tools access with the wildcard array', () => {
    const formValues = createAgentFormValues({
      allowed_tools: ['*'],
    });

    expect(formValues.allowed_tools).toEqual(['*']);

    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      temperature: '0.1',
      allowed_tools: formValues.allowed_tools,
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.allowed_tools).toEqual(['*']);
  });

  it('round-trips no-tools access with an empty array', () => {
    const formValues = createAgentFormValues({
      allowed_tools: [],
    });

    expect(formValues.allowed_tools).toEqual([]);

    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      temperature: '0.1',
      allowed_tools: formValues.allowed_tools,
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.allowed_tools).toEqual([]);
  });

  it('does not parse legacy string allowed skills when creating form values', () => {
    const values = createAgentFormValues({
      allowed_skills: 'debugging\nctx7',
    });

    expect(values.allowed_skills).toEqual(['*']);
  });

  it('does not parse legacy string allowed skills when normalizing payloads', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      temperature: '0.1',
      allowed_tools: ['*'],
      allowed_skills: 'debugging\nctx7',
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.allowed_skills).toEqual(['*']);
  });

  it('keeps legacy string allowed tools parsing unchanged', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      temperature: '0.1',
      allowed_tools: 'read\nwrite',
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload.allowed_tools).toEqual(['read', 'write']);
  });

  it('omits blank workspace from create payloads', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      workspace: ' ',
      temperature: '0.1',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload).not.toHaveProperty('workspace');
  });

  it('omits nonblank workspace from create payloads', () => {
    const result = normalizeAgentForm({
      id: 'coder',
      name: 'Coder',
      workspace: 'C:/workspace-coder',
      temperature: '0.1',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload).not.toHaveProperty('workspace');
  });

  it('keeps id create-only while editing', () => {
    const result = normalizeAgentForm(
      {
        id: 'coder',
        name: 'Coder Prime',
        workspace: 'C:/workspace-coder',
        temperature: '0.1',
        allowed_tools: ['*'],
        allowed_skills: ['*'],
      },
      { mode: AGENT_FORM_MODE_EDIT },
    );

    expect(result.isValid).toBe(true);
    expect(result.payload.id).toBe('coder');
    expect(result.payload.name).toBe('Coder Prime');
    expect(result.payload.workspace).toBe('C:/workspace-coder');
  });

  it('sends changed workspace when editing with baseline values', () => {
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      workspace: 'C:/workspace-coder',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });

    const result = normalizeAgentForm(
      {
        ...initialValues,
        workspace: 'D:/workspace-coder',
      },
      {
        mode: AGENT_FORM_MODE_EDIT,
        initialValues,
      },
    );

    expect(result.isValid).toBe(true);
    expect(result.payload).toEqual({
      id: 'coder',
      workspace: 'D:/workspace-coder',
    });
  });

  it('sends only changed fields when editing with baseline values', () => {
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-5.2',
      fallback_model: 'openai/gpt-5.2-mini',
      workspace: 'C:/workspace-coder',
      temperature: 0.2,
      thinking_effort: 'high',
      memory_prompt_mode: 'agent_user',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });

    const result = normalizeAgentForm(
      {
        ...initialValues,
        name: 'Coder Prime',
      },
      {
        mode: AGENT_FORM_MODE_EDIT,
        initialValues,
      },
    );

    expect(result.isValid).toBe(true);
    expect(result.payload).toEqual({
      id: 'coder',
      name: 'Coder Prime',
    });
  });

  it('sends changed custom system prompt toggle when editing with baseline values', () => {
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      workspace: 'C:/workspace-coder',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
      custom_system_prompt_enabled: false,
    });

    const result = normalizeAgentForm(
      {
        ...initialValues,
        custom_system_prompt_enabled: true,
      },
      {
        mode: AGENT_FORM_MODE_EDIT,
        initialValues,
      },
    );

    expect(result.isValid).toBe(true);
    expect(result.payload).toEqual({
      id: 'coder',
      custom_system_prompt_enabled: true,
    });
  });

  it('sends changed memory prompt mode when editing with baseline values', () => {
    const initialValues = createAgentFormValues({
      id: 'coder',
      name: 'Coder',
      workspace: 'C:/workspace-coder',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
      memory_prompt_mode: 'agent_user',
    });

    const result = normalizeAgentForm(
      {
        ...initialValues,
        memory_prompt_mode: 'agent',
      },
      {
        mode: AGENT_FORM_MODE_EDIT,
        initialValues,
      },
    );

    expect(result.isValid).toBe(true);
    expect(result.payload).toEqual({
      id: 'coder',
      memory_prompt_mode: 'agent',
    });
  });

  it('reports required create fields and invalid temperature', () => {
    const result = normalizeAgentForm({
      id: '',
      name: '',
      temperature: 'warm',
    });

    expect(result.isValid).toBe(false);
    expect(result.errors).toEqual({
      id: 'required',
      name: 'required',
      temperature: 'invalid_number',
    });
  });

  it('reports unsafe agent ids before submitting', () => {
    const result = normalizeAgentForm({
      id: '../bad',
      name: 'Bad',
      temperature: '0.1',
    });

    expect(result.isValid).toBe(false);
    expect(result.errors.id).toBe('invalid_id');
  });

  it('reports a required workspace while editing', () => {
    const result = normalizeAgentForm(
      {
        id: 'coder',
        name: 'Coder',
        workspace: '',
      },
      { mode: AGENT_FORM_MODE_EDIT },
    );

    expect(result.isValid).toBe(false);
    expect(result.errors.workspace).toBe('required');
  });

  it('converts list text using one item per line', () => {
    expect(textToList('alpha\n\n beta \n')).toEqual(['alpha', 'beta']);
  });

  describe('reasoningForModelValue', () => {
    const models = [
      {
        id: 'openai/gpt-5.2',
        capabilities: {
          reasoning: { supported: true, levels: ['high', 'xhigh'] },
        },
      },
    ];

    it('returns the reasoning block for a known model', () => {
      expect(reasoningForModelValue('openai/gpt-5.2', models)).toEqual({
        supported: true,
        levels: ['high', 'xhigh'],
      });
    });

    it('strips the connection suffix before matching', () => {
      expect(reasoningForModelValue('openai/gpt-5.2::api-key', models)).toEqual(
        { supported: true, levels: ['high', 'xhigh'] },
      );
    });

    it('returns null for an empty value or an unknown model', () => {
      expect(reasoningForModelValue('', models)).toBeNull();
      expect(reasoningForModelValue('unknown/model', models)).toBeNull();
    });
  });

  describe('effortOptionsForReasoning', () => {
    it('keeps the full ladder when there is no published ladder', () => {
      expect(effortOptionsForReasoning(null)).toEqual(THINKING_EFFORT_OPTIONS);
      expect(effortOptionsForReasoning({ levels: [] })).toEqual(
        THINKING_EFFORT_OPTIONS,
      );
    });

    it('narrows to the empty option, none, and the model levels in order', () => {
      expect(effortOptionsForReasoning({ levels: ['high', 'xhigh'] })).toEqual([
        '',
        'none',
        'high',
        'xhigh',
      ]);
    });
  });
});
