import { describe, expect, it } from 'vitest';

import {
  applyCronListResponse,
  buildCreateCronPayload,
  buildCronAgentDropdownOptions,
  buildCronAgentOptions,
  buildCronPresetOptions,
  buildUpdateCronPayload,
  createCronFormValues,
  createCronViewState,
  CRON_PRESET_CUSTOM,
  cronFormFingerprint,
  cronPresetExpression,
  cronPresetForExpression,
  describeCronExpression,
  formatTimestamp,
  toDateTimeLocalInput,
  visibleCronJobs,
} from '../cronView.js';

describe('cron time and history projection', () => {
  it('shows a persisted instant in the schedule timezone in list and form', () => {
    const job = {
      id: 'job-once',
      agent_id: 'main',
      prompt: 'Run once',
      schedule_type: 'once',
      run_at: '2026-07-18T16:00:00+00:00',
      timezone: 'UTC',
      effective_timezone: 'UTC',
      status: 'active',
    };

    const [normalized] = visibleCronJobs([job]);
    const form = createCronFormValues(job, 'Europe/Berlin');

    expect(normalized.schedule_description).toContain('16:00');
    expect(form.run_at).toBe('2026-07-18T16:00');
    expect(formatTimestamp(job.run_at, 'Europe/Berlin')).toContain('18:00');
    expect(toDateTimeLocalInput(job.run_at, 'Europe/Berlin')).toBe(
      '2026-07-18T18:00',
    );
  });

  it('keeps completed and missed jobs visible as manageable history', () => {
    const jobs = visibleCronJobs([
      { id: 'active', status: 'active' },
      { id: 'completed', status: 'completed' },
      { id: 'missed', status: 'missed' },
    ]);
    expect(jobs.map((job) => job.id)).toEqual([
      'active',
      'completed',
      'missed',
    ]);
  });

  it('stores the server IANA timezone from cron.list', () => {
    const state = createCronViewState();
    applyCronListResponse(state, {
      jobs: [],
      system_timezone: 'Europe/Berlin',
    });
    expect(state.systemTimezone).toBe('Europe/Berlin');
  });

  it('detects form edits without including server-only execution state', () => {
    const form = createCronFormValues(null, 'UTC');
    const baseline = cronFormFingerprint(form);
    form.prompt = 'Changed';
    expect(cronFormFingerprint(form)).not.toBe(baseline);
  });
});

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

describe('cron schedule presets', () => {
  it('lists the Custom fallback first, then every named preset', () => {
    const options = buildCronPresetOptions((key) => `label:${key}`);
    expect(options.map((option) => option.value)).toEqual([
      CRON_PRESET_CUSTOM,
      'every15Minutes',
      'hourly',
      'dailyMorning',
      'weekdayMornings',
      'mondayMornings',
      'monthlyFirst',
    ]);
    expect(options[0].label).toBe('label:custom');
    expect(options[1].label).toBe('label:every15Minutes');
  });

  it('fills the exact expression of a named preset', () => {
    expect(cronPresetExpression('every15Minutes')).toBe('*/15 * * * *');
    expect(cronPresetExpression('hourly')).toBe('0 * * * *');
    expect(cronPresetExpression('dailyMorning')).toBe('0 9 * * *');
    expect(cronPresetExpression('weekdayMornings')).toBe('0 9 * * 1-5');
    expect(cronPresetExpression('mondayMornings')).toBe('0 9 * * 1');
    expect(cronPresetExpression('monthlyFirst')).toBe('0 9 1 * *');
  });

  it('fills nothing for the Custom preset or an unknown key', () => {
    expect(cronPresetExpression(CRON_PRESET_CUSTOM)).toBe('');
    expect(cronPresetExpression('not-a-preset')).toBe('');
  });

  it('derives the matching preset from an expression by exact match', () => {
    expect(cronPresetForExpression('0 9 * * 1-5')).toBe('weekdayMornings');
    expect(cronPresetForExpression('  */15 * * * *  ')).toBe('every15Minutes');
  });

  it('flips to Custom when the expression matches no preset', () => {
    expect(cronPresetForExpression('0 9 * * 2')).toBe(CRON_PRESET_CUSTOM);
    expect(cronPresetForExpression('')).toBe(CRON_PRESET_CUSTOM);
    expect(cronPresetForExpression('   ')).toBe(CRON_PRESET_CUSTOM);
  });

  it('round-trips a filled expression back to its preset', () => {
    for (const key of [
      'every15Minutes',
      'hourly',
      'dailyMorning',
      'weekdayMornings',
      'mondayMornings',
      'monthlyFirst',
    ]) {
      expect(cronPresetForExpression(cronPresetExpression(key))).toBe(key);
    }
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
