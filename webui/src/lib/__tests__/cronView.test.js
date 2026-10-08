import { describe, expect, it } from 'vitest';

import {
  applyCronListResponse,
  buildCreateCronPayload,
  buildCronExpression,
  buildUpdateCronPayload,
  createCronFormValues,
  createCronViewState,
  cronFormFingerprint,
  cronScheduleFields,
  describeCronExpression,
  updateCronSchedule,
  visibleCronJobs,
} from '../cronView.js';

describe('cron form payloads and history projection', () => {
  it('builds interval and repeat payloads while allowing an omitted name', () => {
    const form = createCronFormValues();
    form.agent_id = 'main';
    form.prompt = 'Check status';
    updateCronSchedule(form, {
      frequency: 'interval',
      interval_value: '120',
      interval_unit: 'minutes',
    });
    form.repeat = '3';

    expect(buildCreateCronPayload(form)).toEqual({
      agent_id: 'main',
      prompt: 'Check status',
      schedule_type: 'interval',
      interval_seconds: 7200,
      repeat: 3,
    });
  });

  it('round-trips interval cadence and the repeat limit through the edit form', () => {
    const job = {
      id: 'job-interval',
      agent_id: 'main',
      name: 'Status check',
      prompt: 'Check status',
      schedule_type: 'interval',
      interval_seconds: 10800,
      remaining_runs: 2,
      status: 'active',
    };

    const [normalized] = visibleCronJobs([job]);
    const form = createCronFormValues(job);

    expect(normalized.schedule_description).toBe('every 3h');
    expect(form.frequency).toBe('interval');
    expect(form.interval_value).toBe('3');
    expect(form.interval_unit).toBe('hours');
    expect(form.repeat).toBe('2');
    // An unchanged form keeps the finite count; clearing it sends an explicit
    // null so the server drops the limit.
    expect(buildUpdateCronPayload(form)).toMatchObject({
      interval_seconds: 10800,
      repeat: 2,
    });
    form.repeat = '';
    expect(buildUpdateCronPayload(form).repeat).toBeNull();
  });

  it('shows persisted instants in the server timezone and sends them back without a per-job timezone', () => {
    const job = {
      id: 'job-once',
      agent_id: 'main',
      name: 'One-time run',
      prompt: 'Run once',
      schedule_type: 'once',
      run_at: '2026-07-18T16:00:00+00:00',
      status: 'active',
    };
    const state = createCronViewState();

    const [normalized] = applyCronListResponse(state, {
      jobs: [job],
      system_timezone: 'Europe/Berlin',
    });
    const form = createCronFormValues(job, state.systemTimezone);

    expect(state.systemTimezone).toBe('Europe/Berlin');
    expect(normalized.schedule_description).toContain('18:00');
    expect(form.name).toBe('One-time run');
    expect(form.run_at).toBe('2026-07-18T18:00');
    expect(buildCreateCronPayload(form)).not.toHaveProperty('timezone');
    expect(buildUpdateCronPayload(form)).not.toHaveProperty('timezone');
    expect(buildUpdateCronPayload(form).run_at).toBe(
      '2026-07-18T16:00:00+00:00',
    );
  });

  it('uses the prompt as a readable fallback for legacy payloads without a name', () => {
    const [job] = visibleCronJobs([
      {
        id: 'legacy-job',
        agent_id: 'main',
        prompt: 'Review weekly reports',
        schedule_type: 'cron',
        cron_expression: '0 9 * * 1',
        status: 'active',
      },
    ]);

    expect(job.name).toBe('Review weekly reports');
  });

  it('detects form edits without including server-only execution state', () => {
    const form = createCronFormValues(null, 'UTC');
    const baseline = cronFormFingerprint(form);
    form.prompt = 'Changed';
    expect(cronFormFingerprint(form)).not.toBe(baseline);
  });

  it.each([
    ['a Project job', 'builder', 'vbot', 'builder@vbot'],
    ['an Identity job', 'researcher', null, 'researcher'],
  ])(
    'addresses %s by its full target in the form and both payloads',
    (_label, agentId, projectId, address) => {
      const job = {
        id: 'job-1',
        agent_id: agentId,
        project_id: projectId,
        target: address,
        name: 'Work',
        prompt: 'do work',
        schedule_type: 'cron',
        cron_expression: '0 9 * * *',
        status: 'active',
      };
      const form = createCronFormValues(job);

      // The full address is what the dropdown option value and the cron
      // payloads key on; the bare id alone would drop the Project.
      expect(form.agent_id).toBe(address);
      expect(buildCreateCronPayload(form)).toMatchObject({
        agent_id: address,
        name: 'Work',
      });
      expect(buildUpdateCronPayload(form)).toMatchObject({
        agent_id: address,
        name: 'Work',
      });
      // Without a target, the address is formatted from agent_id/project_id.
      const [listed] = visibleCronJobs([{ ...job, target: undefined }]);
      expect(listed.agent_id).toBe(address);
    },
  );
});

describe('cron expressions and the schedule planner', () => {
  it('describes five-field expressions in 24-hour plain text and blanks invalid input', () => {
    expect(describeCronExpression('0 9 * * 1-5')).toBe(
      'At 09:00, Monday through Friday',
    );
    expect(describeCronExpression('30 17 * * *')).toBe('At 17:30');
    for (const blank of [
      '',
      '   ',
      null,
      undefined,
      'not a cron',
      '99 99 * *',
    ]) {
      expect(describeCronExpression(blank)).toBe('');
    }
  });

  it.each([
    ['5 * * * *', { frequency: 'hourly', minute: '5' }],
    ['30 7 * * *', { frequency: 'daily', time: '07:30' }],
    ['0 9 * * 1-5', { frequency: 'weekly', weekdays: [1, 2, 3, 4, 5] }],
    ['0 9 * * 1,3,5', { frequency: 'weekly', weekdays: [1, 3, 5] }],
    ['0 18 * * 0,6', { frequency: 'weekly', weekdays: [6, 0] }],
    ['0 9 31 * *', { frequency: 'monthly', month_day: '31' }],
  ])('reads %s into the planner and builds it back', (expression, fields) => {
    const parsed = cronScheduleFields(expression);
    expect(parsed).toMatchObject(fields);
    expect(buildCronExpression(parsed)).toBe(expression);
  });

  it('normalizes equivalent weekday spellings to Monday-first days and ranges', () => {
    const parsed = cronScheduleFields('0 9 * * 7,1,2,3');
    expect(parsed.weekdays).toEqual([1, 2, 3, 0]);
    expect(buildCronExpression(parsed)).toBe('0 9 * * 0-3');
  });

  it.each([
    '*/15 * * * *',
    '0 9 1 1 *',
    '0 9 1 * 1',
    '0 8-17 * * *',
    '0 9 * * MON',
    'not a cron',
  ])('keeps %s as a custom expression', (expression) => {
    expect(cronScheduleFields(expression).frequency).toBe('custom');
  });

  it('builds no expression while planner fields are incomplete', () => {
    const base = cronScheduleFields('0 9 * * *');
    expect(buildCronExpression({ ...base, time: '' })).toBe('');
    expect(
      buildCronExpression({ ...base, frequency: 'weekly', weekdays: [] }),
    ).toBe('');
    expect(
      buildCronExpression({ ...base, frequency: 'monthly', month_day: '32' }),
    ).toBe('');
    expect(
      buildCronExpression({ ...base, frequency: 'hourly', minute: '60' }),
    ).toBe('');
  });

  it('keeps the persisted schedule in step with planner edits', () => {
    const form = createCronFormValues();
    expect(form).toMatchObject({
      frequency: 'daily',
      schedule_type: 'cron',
      cron_expression: '0 9 * * *',
    });

    updateCronSchedule(form, { frequency: 'weekly', time: '07:30' });
    expect(form.cron_expression).toBe('30 7 * * 1-5');

    // A one-time schedule gets its fixed repeat limit and gives it back.
    updateCronSchedule(form, { frequency: 'once' });
    expect(form).toMatchObject({ schedule_type: 'once', repeat: '1' });
    updateCronSchedule(form, { frequency: 'interval' });
    expect(form).toMatchObject({ schedule_type: 'interval', repeat: '' });

    // Custom keeps the last expression editable and syncs the planner fields
    // from hand edits, so switching back continues from them.
    updateCronSchedule(form, { frequency: 'custom' });
    expect(form).toMatchObject({
      schedule_type: 'cron',
      cron_expression: '30 7 * * 1-5',
    });
    updateCronSchedule(form, { cron_expression: '15 6 * * 2' });
    updateCronSchedule(form, { frequency: 'weekly' });
    expect(form.cron_expression).toBe('15 6 * * 2');
  });
});
