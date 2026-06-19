import { describe, expect, it } from 'vitest';

import {
  buildCreateCronPayload,
  buildCronAgentDropdownOptions,
  buildCronAgentOptions,
  buildUpdateCronPayload,
  createCronFormValues,
  describeCronExpression,
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

// The combined identity + project agent option builders now live in the shared
// `agentTargetOptions` module, where their exhaustive coverage moved too. Cron
// keeps a thin smoke test to lock the re-export aliases — and thus the project-
// aware `agent@projekt` option values cron saves — against accidental breakage.
describe('cron agent option re-exports', () => {
  const identityAgents = [{ id: 'researcher', name: 'Researcher' }];
  const projectTeams = [
    {
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    },
  ];

  it('still builds project-aware options under the cron names', () => {
    expect(
      buildCronAgentOptions(identityAgents, projectTeams).map((o) => o.value),
    ).toEqual(['researcher', 'builder@vbot']);
  });

  it('still inserts group headers under the cron names', () => {
    const options = buildCronAgentDropdownOptions(
      identityAgents,
      projectTeams,
      {
        identityGroupLabel: 'Identity agents',
        projectGroupLabel: 'Project agents',
      },
    );
    expect(options.filter((o) => o.isGroupHeader).map((o) => o.label)).toEqual([
      'Identity agents',
      'Project agents',
    ]);
  });
});
