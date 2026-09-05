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
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));
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
  input.dispatchEvent(
    new Event(input.tagName === 'SELECT' ? 'change' : 'input', {
      bubbles: true,
    }),
  );
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
  expect(document.getElementById('calendar-action-session').value).toBe('');
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
  expect(document.getElementById('calendar-action-session').value).toBe(
    'existing',
  );
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
