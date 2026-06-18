import { describe, expect, it } from 'vitest';

import {
  buildCreateCronPayload,
  buildCronAgentDropdownOptions,
  buildCronAgentOptions,
  buildUpdateCronPayload,
  CRON_AGENT_GROUP_IDENTITY,
  CRON_AGENT_GROUP_PROJECT,
  createCronFormValues,
  describeCronExpression,
  projectIdsFromList,
  projectTeamEntry,
  visibleCronJobs,
} from '../cronView.js';

describe('describeCronExpression', () => {
  it('describes a standard five-field expression in plain text', () => {
    expect(describeCronExpression('0 9 * * 1-5')).toBe(
      'At 09:00, Monday through Friday',
    );
  });

  it('uses 24-hour time', () => {
    expect(describeCronExpression('30 17 * * *')).toBe('At 17:30');
  });

  it('returns an empty string for blank input', () => {
    expect(describeCronExpression('')).toBe('');
    expect(describeCronExpression('   ')).toBe('');
    expect(describeCronExpression(null)).toBe('');
    expect(describeCronExpression(undefined)).toBe('');
  });

  it('returns an empty string for unparseable expressions', () => {
    expect(describeCronExpression('not a cron')).toBe('');
    expect(describeCronExpression('99 99 * *')).toBe('');
  });
});

describe('cron job target normalization (project-aware)', () => {
  it('pre-fills the form agent_id from the formatted target of a project job', () => {
    const form = createCronFormValues({
      id: 'job-1',
      agent_id: 'builder',
      project_id: 'vbot',
      target: 'builder@vbot',
      schedule_type: 'cron',
      cron_expression: '0 9 * * *',
      status: 'active',
    });
    // The full address is what the dropdown option value and the cron.update
    // payload key on — never the bare id (which would drop the project).
    expect(form.agent_id).toBe('builder@vbot');
  });

  it('keeps an identity job byte-identical (bare agent_id, no project)', () => {
    const form = createCronFormValues({
      id: 'job-2',
      agent_id: 'researcher',
      project_id: null,
      target: 'researcher',
      schedule_type: 'cron',
      cron_expression: '0 9 * * *',
      status: 'active',
    });
    expect(form.agent_id).toBe('researcher');
  });

  it('falls back to formatting from agent_id/project_id when target is absent', () => {
    const [job] = visibleCronJobs([
      {
        id: 'job-3',
        agent_id: 'builder',
        project_id: 'vbot',
        schedule_type: 'cron',
        cron_expression: '0 9 * * *',
        status: 'active',
      },
    ]);
    expect(job.agent_id).toBe('builder@vbot');
  });

  it('sends the full address as the agent_id of cron create/update payloads', () => {
    const form = createCronFormValues({
      id: 'job-4',
      agent_id: 'builder',
      project_id: 'vbot',
      target: 'builder@vbot',
      prompt: 'do work',
      schedule_type: 'cron',
      cron_expression: '0 9 * * *',
      status: 'active',
    });
    form.prompt = 'do work';
    expect(buildCreateCronPayload(form).agent_id).toBe('builder@vbot');
    expect(buildUpdateCronPayload(form).agent_id).toBe('builder@vbot');
  });

  it('sends the bare id for an identity job payload, unchanged from today', () => {
    const form = createCronFormValues({
      id: 'job-5',
      agent_id: 'researcher',
      project_id: null,
      target: 'researcher',
      prompt: 'do work',
      schedule_type: 'cron',
      cron_expression: '0 9 * * *',
      status: 'active',
    });
    form.prompt = 'do work';
    expect(buildCreateCronPayload(form).agent_id).toBe('researcher');
    expect(buildUpdateCronPayload(form).agent_id).toBe('researcher');
  });
});

describe('buildCronAgentOptions', () => {
  const identityAgents = [{ id: 'researcher', name: 'Researcher' }];
  const projectTeams = [
    {
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    },
  ];

  it('lists identity agents as bare-id options (unchanged)', () => {
    const options = buildCronAgentOptions(identityAgents, []);
    expect(options).toEqual([
      {
        value: 'researcher',
        label: 'Researcher',
        secondaryLabel: 'researcher',
        group: CRON_AGENT_GROUP_IDENTITY,
        projectId: null,
      },
    ]);
  });

  it('lists project agents with the address as the option value', () => {
    const [, projectOption] = buildCronAgentOptions(
      identityAgents,
      projectTeams,
    );
    expect(projectOption.value).toBe('builder@vbot');
    expect(projectOption.group).toBe(CRON_AGENT_GROUP_PROJECT);
    expect(projectOption.projectId).toBe('vbot');
  });

  it('orders identity agents before project agents', () => {
    const options = buildCronAgentOptions(identityAgents, projectTeams);
    expect(options.map((option) => option.value)).toEqual([
      'researcher',
      'builder@vbot',
    ]);
  });

  it('tolerates missing/empty inputs', () => {
    expect(buildCronAgentOptions(null, null)).toEqual([]);
    expect(buildCronAgentOptions([{ id: '' }], [{ projectId: '' }])).toEqual([]);
  });
});

describe('buildCronAgentDropdownOptions', () => {
  const identityAgents = [{ id: 'researcher', name: 'Researcher' }];
  const projectTeams = [
    {
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    },
  ];

  it('inserts no group headers when only identity agents exist (unchanged)', () => {
    const options = buildCronAgentDropdownOptions(identityAgents, [], {
      identityGroupLabel: 'Identity agents',
      projectGroupLabel: 'Project agents',
    });
    expect(options.some((option) => option.isGroupHeader)).toBe(false);
    expect(options.map((option) => option.value)).toEqual(['researcher']);
  });

  it('separates the two kinds with disabled group headers', () => {
    const options = buildCronAgentDropdownOptions(identityAgents, projectTeams, {
      identityGroupLabel: 'Identity agents',
      projectGroupLabel: 'Project agents',
    });
    expect(options.map((option) => option.label)).toEqual([
      'Identity agents',
      'Researcher',
      'Project agents',
      'builder@vbot',
    ]);
    const headers = options.filter((option) => option.isGroupHeader);
    expect(headers.every((header) => header.disabled)).toBe(true);
  });
});

describe('project team gathering helpers', () => {
  it('extracts non-empty project ids from a project.list response', () => {
    expect(
      projectIdsFromList({
        projects: [{ project_id: 'vbot' }, { project_id: '' }, {}],
      }),
    ).toEqual(['vbot']);
    expect(projectIdsFromList(null)).toEqual([]);
  });

  it('projects a project.show response into a team entry', () => {
    const entry = projectTeamEntry('vbot', {
      project: { display_name: 'vBot' },
      scan: {
        team: [
          { agent_id: 'builder', display_name: 'Builder' },
          { agent_id: '' },
        ],
      },
    });
    expect(entry).toEqual({
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    });
  });

  it('yields an empty team for a bare/empty project (normal case)', () => {
    expect(projectTeamEntry('empty', { project: {}, scan: {} })).toEqual({
      projectId: 'empty',
      displayName: 'empty',
      team: [],
    });
  });
});
