// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listSessions: (agentId, query = {}) =>
      rpcMock('session.list', { agent_id: agentId, ...query }),
  }),
);
const { default: CalendarActions } = await import('../CalendarActions.svelte');
let component;

async function settle() {
  for (let i = 0; i < 30; i++) {
    await Promise.resolve();
    flushSync();
  }
}
function button(label) {
  return [...document.querySelectorAll('button')].find(
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

beforeEach(() => {
  init('en');
  rpcMock.mockReset();
  rpcMock.mockImplementation(async (method) => {
    if (method === 'agent.list')
      return { agents: [{ id: 'main', name: 'Main' }] };
    if (method === 'project.list') return { projects: [] };
    if (method === 'session.list')
      return {
        sessions: [{ id: 'existing', title: 'Existing discussion' }],
        next_cursor: null,
      };
    return { action: { id: 'a1' } };
  });
});
afterEach(() => {
  if (component) unmount(component);
  component = null;
});

it('creates an action with a relative time and a fresh Session by default', async () => {
  const onChanged = vi.fn();
  component = mount(CalendarActions, {
    target: document.body,
    props: {
      eventId: 'event1',
      occurrenceStart: '2027-01-01T12:00',
      onChanged,
    },
  });
  await settle();
  button('Add action').click();
  await settle();
  expect(triggerLabel('calendar-action-target')).toBe('Main');
  expect(triggerLabel('calendar-action-session')).toBe(
    'New Session for each execution',
  );
  change('calendar-action-prompt', 'Prepare meeting');
  button('Save').click();
  await settle();
  expect(rpcMock).toHaveBeenCalledWith('calendar.add_action', {
    id: 'event1',
    when: 'start - 1h',
    prompt: 'Prepare meeting',
    target: 'main',
    session: null,
  });
  expect(onChanged).toHaveBeenCalledOnce();
});

it('preserves the Session and result after a single event moves', async () => {
  const open = vi.fn();
  component = mount(CalendarActions, {
    target: document.body,
    props: {
      eventId: 'event1',
      occurrenceStart: '2027-01-01T12:00',
      onOpenSession: open,
      actions: [
        {
          id: 'a1',
          event_id: 'event1',
          target: 'main',
          when: 'end + 30m',
          session: 'existing',
          prompt: 'Review meeting',
        },
      ],
      executions: [
        {
          action_id: 'a1',
          occurrence_start: '2026-12-31T12:00',
          target: 'main',
          session: 'existing',
          run_id: 'run1',
          status: 'completed',
          scheduled_at: '2027-01-01T13:30:00Z',
          expires_at: '2027-01-01T14:30:00Z',
        },
      ],
    },
  });
  await settle();
  button('Open Session').click();
  expect(open).toHaveBeenCalledWith('main', 'existing');
  button('Edit').click();
  await settle();
  expect(triggerLabel('calendar-action-session')).toBe('Existing discussion');
  change('calendar-action-amount', '45');
  button('Save').click();
  await settle();
  expect(rpcMock).toHaveBeenCalledWith('calendar.update_action', {
    id: 'a1',
    when: 'end + 45m',
    prompt: 'Review meeting',
    target: 'main',
    session: 'existing',
  });
});

it('keeps failed edits visible and does not claim they were saved', async () => {
  const onChanged = vi.fn();
  component = mount(CalendarActions, {
    target: document.body,
    props: {
      eventId: 'event1',
      occurrenceStart: '2027-01-01T12:00',
      onChanged,
    },
  });
  await settle();
  button('Add action').click();
  await settle();
  change('calendar-action-prompt', 'Prepare');
  rpcMock.mockRejectedValueOnce(new Error('test-owned failure'));
  button('Save').click();
  await settle();
  expect(document.body.textContent).toContain('test-owned failure');
  expect(document.querySelector('.calendar-action-editor')).not.toBeNull();
  expect(onChanged).not.toHaveBeenCalled();
});

it('keeps Identity and healthy Project targets when another Team fails', async () => {
  rpcMock.mockImplementation(async (method, params) => {
    if (method === 'agent.list')
      return { agents: [{ id: 'main', name: 'Main' }] };
    if (method === 'project.list')
      return {
        projects: [{ project_id: 'broken' }, { project_id: 'healthy' }],
      };
    if (method === 'project.show') {
      if (params.project_id === 'broken')
        throw new Error('test-project-unavailable');
      return { scan: { team: [{ agent_id: 'coder' }] } };
    }
    return { sessions: [] };
  });
  component = mount(CalendarActions, {
    target: document.body,
    props: { eventId: 'event1', occurrenceStart: '2027-01-01T12:00' },
  });
  await settle();
  button('Add action').click();
  await settle();
  const targets = optionLabels('calendar-action-target');
  expect(targets).toContain('Main');
  expect(targets).toContain('coder@healthy');
});

it('builds the timing and Session from the choice fields', async () => {
  rpcMock.mockImplementation(async (method, params) => {
    if (method === 'agent.list')
      return {
        agents: [
          { id: 'main', name: 'Main' },
          { id: 'helper', name: 'Helper' },
        ],
      };
    if (method === 'project.list') return { projects: [] };
    if (method === 'session.list')
      return {
        sessions:
          params.agent_id === 'helper'
            ? [{ id: 'helper-s1', title: 'Helper thread' }]
            : [{ id: 'existing', title: 'Existing discussion' }],
        next_cursor: null,
      };
    return { action: { id: 'a1' } };
  });
  component = mount(CalendarActions, {
    target: document.body,
    props: { eventId: 'event1', occurrenceStart: '2027-01-01T12:00' },
  });
  await settle();
  button('Add action').click();
  await settle();
  choose('calendar-action-session', 'Existing discussion');
  // Switching the Agent drops the Session chosen for the previous Agent and
  // lists the new Agent's Sessions.
  choose('calendar-action-target', 'Helper');
  await settle();
  expect(triggerLabel('calendar-action-session')).toBe(
    'New Session for each execution',
  );
  choose('calendar-action-session', 'Helper thread');
  choose('calendar-action-direction', 'After');
  choose('calendar-action-unit', 'minutes');
  choose('calendar-action-anchor', 'End');
  change('calendar-action-prompt', 'Summarize');
  button('Save').click();
  await settle();
  expect(rpcMock).toHaveBeenCalledWith('calendar.add_action', {
    id: 'event1',
    when: 'end + 1m',
    prompt: 'Summarize',
    target: 'helper',
    session: 'helper-s1',
  });
});

it('hides the offset fields for an action at the event anchor', async () => {
  component = mount(CalendarActions, {
    target: document.body,
    props: { eventId: 'event1', occurrenceStart: '2027-01-01T12:00' },
  });
  await settle();
  button('Add action').click();
  await settle();
  choose('calendar-action-direction', 'At');
  expect(document.getElementById('calendar-action-amount')).toBeNull();
  expect(document.getElementById('calendar-action-unit')).toBeNull();
  change('calendar-action-prompt', 'Join');
  button('Save').click();
  await settle();
  expect(rpcMock).toHaveBeenCalledWith(
    'calendar.add_action',
    expect.objectContaining({ when: 'start' }),
  );
});
