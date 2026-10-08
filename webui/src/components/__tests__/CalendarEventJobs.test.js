// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../lib/i18n.js';
import {
  compactTimestamp,
  outcomeLabel,
  statusLabel,
} from '../cron/presentation.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const rpcMock = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listSessions: (agentId, query = {}) =>
      rpcMock('session.list', { agent_id: agentId, ...query }),
    createCronJob: (params) => rpcMock('cron.create', params),
    updateCronJob: (params) => rpcMock('cron.update', params),
    deleteCronJob: (id) => rpcMock('cron.delete', { id }),
  }),
);
const { default: CalendarEventJobs } =
  await import('../CalendarEventJobs.svelte');
let component;

// One occurrence of the event `event1`, 12:00 to 13:00 UTC.
const OCCURRENCE = {
  id: 'event1',
  event_id: 'event1',
  title: 'Review',
  all_day: false,
  recurring: false,
  start: '2027-01-01T12:00:00',
  end: '2027-01-01T13:00:00',
  start_utc: '2027-01-01T12:00:00+00:00',
  end_utc: '2027-01-01T13:00:00+00:00',
};

function job(overrides = {}) {
  return {
    id: 'cron-1',
    target: 'main',
    agent_id: 'main',
    name: 'Prepare the agenda',
    prompt: 'Prepare the agenda',
    schedule_type: 'event',
    event_id: 'event1',
    event_title: 'Review',
    event_edge: 'start',
    event_offset_minutes: -30,
    session_id: null,
    status: 'active',
    last_outcome: null,
    last_error: null,
    last_fired_at: null,
    last_completed_at: null,
    next_fire_at: '2027-01-01T11:30:00+00:00',
    ...overrides,
  };
}

function mountJobs(props = {}) {
  component = mount(CalendarEventJobs, {
    target: document.body,
    props: {
      eventId: 'event1',
      occurrence: OCCURRENCE,
      timeZone: 'UTC',
      ...props,
    },
  });
}

async function settle() {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    flushSync();
  }
}
function button(label, root = document) {
  return [...root.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
}
function change(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}
// Choice fields use the shared Dropdown: a trigger button carrying the field id
// and a listbox portaled to <body> while open.
function triggerLabel(id) {
  return document
    .getElementById(id)
    .querySelector('.dropdown-primitive__trigger-label')
    .textContent.trim();
}
function openChoices(id) {
  document.getElementById(id).click();
  flushSync();
  return [...document.querySelectorAll(`#${id}-listbox [role="option"]`)];
}
function optionLabels(id) {
  const labels = openChoices(id).map((option) =>
    option
      .querySelector('.dropdown-primitive__option-label')
      .textContent.trim(),
  );
  document.getElementById(id).click();
  flushSync();
  return labels;
}
function choose(id, label) {
  const option = openChoices(id).find(
    (item) =>
      item
        .querySelector('.dropdown-primitive__option-label')
        .textContent.trim() === label,
  );
  expect(option).toBeTruthy();
  option.click();
  flushSync();
}
function catalogRpc({ agents = [{ id: 'main', name: 'Main' }] } = {}) {
  return async (method, params) => {
    if (method === 'agent.list') return { agents };
    if (method === 'project.list') return { projects: [] };
    if (method === 'session.list')
      return {
        sessions:
          params.agent_id === 'helper'
            ? [{ id: 'helper-s1', title: 'Helper thread' }]
            : [{ id: 'existing', title: 'Existing discussion' }],
        next_cursor: null,
      };
    return { job: { id: 'cron-new' } };
  };
}

beforeEach(() => {
  init('en');
  rpcMock.mockReset();
  rpcMock.mockImplementation(catalogRpc());
});
afterEach(() => {
  if (component) unmount(component);
  component = null;
});

it('shows when each job of the event runs, for which Agent, and how it went', async () => {
  const open = vi.fn();
  const onChanged = vi.fn();
  const failed = job({
    session_id: 'existing',
    last_outcome: 'failed',
    last_error: 'Provider timed out',
    last_fired_at: '2026-12-25T11:30:00+00:00',
    last_completed_at: '2026-12-25T11:31:00+00:00',
  });
  mountJobs({
    jobs: [
      failed,
      // A job of another event is not this event's.
      job({ id: 'cron-other', event_id: 'event2', prompt: 'Other event' }),
    ],
    onOpenSession: open,
    onChanged,
  });
  await settle();

  expect(document.querySelectorAll('.calendar-job-row')).toHaveLength(1);
  const row = document.querySelector('[data-testid="calendar-job-cron-1"]');
  const text = row.textContent;
  expect(text).toContain('30 minutes before start');
  // The due time of the shown occurrence, on its own day.
  expect(row.querySelector('.calendar-job-due').textContent).toContain('11:30');
  expect(text).toContain('Main');
  expect(text).toContain(statusLabel('active'));
  expect(text).toContain('Prepare the agenda');
  expect(text).toContain(
    t('calendar.jobs.next', {
      time: compactTimestamp(failed.next_fire_at, 'UTC'),
    }),
  );
  expect(text).toContain(
    t('calendar.jobs.lastAt', {
      outcome: outcomeLabel('failed'),
      time: compactTimestamp(failed.last_completed_at, 'UTC'),
    }),
  );
  expect(text).toContain(
    t('calendar.jobs.error', { reason: 'Provider timed out' }),
  );

  // A job with a fixed Session opens it.
  button(t('calendar.jobs.openSession'), row).click();
  expect(open).toHaveBeenCalledWith('main', 'existing');

  // Delete asks first.
  button(t('common.delete'), row).click();
  flushSync();
  expect(rpcMock).not.toHaveBeenCalledWith('cron.delete', expect.anything());
  const confirm = [...row.querySelectorAll('.calendar-job-controls')].at(-1);
  button(t('common.delete'), confirm).click();
  await settle();
  expect(rpcMock).toHaveBeenCalledWith('cron.delete', { id: 'cron-1' });
  expect(onChanged).toHaveBeenCalledOnce();
});

it('adds a job 30 minutes before the start in a new Session, keeping the edit after a failed save', async () => {
  const onChanged = vi.fn();
  mountJobs({ jobs: [], onChanged });
  await settle();
  expect(document.body.textContent).toContain(t('calendar.jobs.empty'));

  button(t('calendar.jobs.add')).click();
  await settle();
  expect(triggerLabel('calendar-job-target')).toBe('Main');
  expect(triggerLabel('calendar-job-session')).toBe(
    t('calendar.jobs.newSession'),
  );
  expect(triggerLabel('calendar-job-direction')).toBe(
    t('cron.eventTime.before'),
  );
  expect(document.getElementById('calendar-job-amount').value).toBe('30');
  change('calendar-job-prompt', 'Prepare meeting');

  rpcMock.mockRejectedValueOnce(new Error('test-owned failure'));
  button(t('common.save')).click();
  await settle();
  expect(document.body.textContent).toContain('test-owned failure');
  expect(document.querySelector('.calendar-job-editor')).not.toBeNull();
  expect(onChanged).not.toHaveBeenCalled();

  button(t('common.save')).click();
  await settle();
  expect(rpcMock).toHaveBeenLastCalledWith('cron.create', {
    agent_id: 'main',
    prompt: 'Prepare meeting',
    event_time: 'start - 30m',
    schedule_type: 'event',
    event_id: 'event1',
  });
  expect(onChanged).toHaveBeenCalledOnce();
  expect(document.querySelector('.calendar-job-editor')).toBeNull();
});

it('edits the Agent, Session and timing of a job from the choice fields', async () => {
  rpcMock.mockImplementation(
    catalogRpc({
      agents: [
        { id: 'main', name: 'Main' },
        { id: 'helper', name: 'Helper' },
      ],
    }),
  );
  mountJobs({ jobs: [job({ session_id: 'existing' })] });
  await settle();
  button(t('common.edit')).click();
  await settle();
  expect(triggerLabel('calendar-job-session')).toBe('Existing discussion');

  // Switching the Agent drops the Session chosen for the previous Agent and
  // lists the new Agent's Sessions.
  choose('calendar-job-target', 'Helper');
  await settle();
  expect(triggerLabel('calendar-job-session')).toBe(
    t('calendar.jobs.newSession'),
  );
  choose('calendar-job-session', 'Helper thread');

  // At the start or end takes no amount.
  choose('calendar-job-direction', t('cron.eventTime.at'));
  expect(document.getElementById('calendar-job-amount')).toBeNull();
  expect(document.getElementById('calendar-job-unit')).toBeNull();
  choose('calendar-job-direction', t('cron.eventTime.after'));
  change('calendar-job-amount', '2');
  choose('calendar-job-unit', t('cron.form.intervalUnit.hours'));
  choose('calendar-job-edge', t('cron.eventTime.end'));
  button(t('common.save')).click();
  await settle();

  expect(rpcMock).toHaveBeenCalledWith('cron.update', {
    id: 'cron-1',
    agent_id: 'helper',
    prompt: 'Prepare the agenda',
    event_time: 'end + 2h',
    session_id: 'helper-s1',
  });
});

it.each([
  [
    'another Team fails',
    { projects: [{ project_id: 'broken' }, { project_id: 'healthy' }] },
    ['Main', 'coder@healthy'],
    () => t('calendar.jobs.targetsPartial'),
  ],
  [
    'the Project list fails',
    new Error('test-projects-unavailable'),
    ['Main'],
    () => 'test-projects-unavailable',
  ],
])(
  'keeps Identity and healthy Project targets when %s',
  async (_label, projectList, targets, message) => {
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'agent.list')
        return { agents: [{ id: 'main', name: 'Main' }] };
      if (method === 'project.list') {
        if (projectList instanceof Error) throw projectList;
        return projectList;
      }
      if (method === 'project.show') {
        if (params.project_id === 'broken')
          throw new Error('test-project-unavailable');
        return { scan: { team: [{ agent_id: 'coder' }] } };
      }
      return { sessions: [] };
    });
    mountJobs({ jobs: [] });
    await settle();
    expect(document.body.textContent).toContain(message());
    button(t('calendar.jobs.add')).click();
    await settle();
    expect(optionLabels('calendar-job-target')).toEqual(targets);
  },
);

it('names the Agents anew after an Agent change and keeps the open edit', async () => {
  let name = 'Main';
  rpcMock.mockImplementation(async (method) => {
    if (method === 'agent.list') return { agents: [{ id: 'main', name }] };
    if (method === 'project.list') return { projects: [] };
    return { sessions: [], next_cursor: null };
  });
  const props = reactiveProps({
    eventId: 'event1',
    occurrence: OCCURRENCE,
    timeZone: 'UTC',
    agentsRefreshToken: 0,
    jobs: [job()],
  });
  component = mount(CalendarEventJobs, { target: document.body, props });
  await settle();
  button(t('common.edit')).click();
  await settle();
  change('calendar-job-prompt', 'Review the agenda');

  name = 'Main Desk';
  props.agentsRefreshToken += 1;
  await settle();

  expect(document.querySelector('.calendar-job-summary').textContent).toContain(
    'Main Desk',
  );
  expect(triggerLabel('calendar-job-target')).toBe('Main Desk');
  expect(document.getElementById('calendar-job-prompt').value).toBe(
    'Review the agenda',
  );
});
