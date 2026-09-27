import { describe, expect, it } from 'vitest';

import {
  applyCronListResponse,
  buildCreateCronPayload,
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
  visibleCronJobs,
} from '../cronView.js';

const PRESET_EXPRESSIONS = {
  every15Minutes: '*/15 * * * *',
  hourly: '0 * * * *',
  dailyMorning: '0 9 * * *',
  weekdayMornings: '0 9 * * 1-5',
  mondayMornings: '0 9 * * 1',
  monthlyFirst: '0 9 1 * *',
};

describe('cron form payloads and history projection', () => {
  it('builds interval and repeat payloads while allowing an omitted name', () => {
    const form = createCronFormValues();
    form.agent_id = 'main';
    form.prompt = 'Check status';
    form.schedule_type = 'interval';
    form.interval_minutes = '120';
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
    expect(form.interval_minutes).toBe('180');
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
    expect(formatTimestamp(job.run_at, 'Europe/Berlin')).toContain('18:00');
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

describe('cron expressions and schedule presets', () => {
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

  it('lists the Custom fallback first, then every named preset', () => {
    const options = buildCronPresetOptions((key) => `label:${key}`);
    expect(options).toEqual(
      [CRON_PRESET_CUSTOM, ...Object.keys(PRESET_EXPRESSIONS)].map((key) => ({
        value: key,
        label: `label:${key}`,
      })),
    );
  });

  it('maps presets to exact expressions and derives Custom for anything else', () => {
    for (const [key, expression] of Object.entries(PRESET_EXPRESSIONS)) {
      expect(cronPresetExpression(key)).toBe(expression);
      expect(cronPresetForExpression(expression)).toBe(key);
    }
    expect(cronPresetForExpression('  */15 * * * *  ')).toBe('every15Minutes');
    expect(cronPresetExpression(CRON_PRESET_CUSTOM)).toBe('');
    expect(cronPresetExpression('not-a-preset')).toBe('');
    for (const expression of ['0 9 * * 2', '', '   ']) {
      expect(cronPresetForExpression(expression)).toBe(CRON_PRESET_CUSTOM);
    }
  });
});
