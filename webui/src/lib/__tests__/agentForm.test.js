import { describe, expect, it } from 'vitest';

import {
  AGENT_FORM_MODE_EDIT,
  createAgentFormValues,
  listToText,
  normalizeAgentForm,
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
      temperature: '',
      thinking_effort: '',
      allowed_tools: ['*'],
      allowed_skills: ['*'],
    });
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
      allowed_tools: ['read', 'write'],
      allowed_skills: ['debugging'],
    });

    expect(values.allowed_tools).toEqual(['read', 'write']);
    expect(values.allowed_skills).toEqual(['debugging']);
    expect(values.temperature).toBe('0.2');
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
      allowed_tools: [' read ', '', 'write '],
      allowed_skills: [' debugging ', ''],
    });

    expect(result.isValid).toBe(true);
    expect(result.payload).toEqual({
      id: 'coder',
      name: 'Coder',
      model: 'openai/gpt-4.1',
      fallback_model: '',
      temperature: 0.25,
      thinking_effort: 'low',
      allowed_tools: ['read', 'write'],
      allowed_skills: ['debugging'],
    });
    expect(result.payload).not.toHaveProperty('workspace');
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
    expect(listToText(['alpha', 'beta'])).toBe('alpha\nbeta');
  });
});
