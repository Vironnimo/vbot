// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest';

import {
  api,
  buttonByText,
  chooseRowAction,
  confirmDialog,
  flushSync,
  rowCount,
  setupSessionListDrawerSuite,
  waitForCondition,
} from './SessionListDrawer.support.js';
import { t } from '../../lib/i18n.js';

// The `sessions.delete` menu label has no catalog entry yet.
const DELETE_ITEM = t('sessions.delete');

function typeRename(value) {
  const input = document.querySelector('.session-row__edit-input');
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return input;
}

function keydown(target, key, init = {}) {
  target.dispatchEvent(
    new KeyboardEvent('keydown', { key, bubbles: true, ...init }),
  );
}

// Each row mutation: its API mock, how to complete it from the row menu of
// `agent`'s Session, and the callback calls it reports once the server
// answers.
const MUTATIONS = {
  rename: {
    mock: () => api.renameSession,
    complete(agent) {
      chooseRowAction(t('sessions.rename'), `${agent} Session`);
      keydown(typeRename('Updated title'), 'Enter');
    },
    reports: () => ({}),
  },
  delete: {
    mock: () => api.deleteSession,
    complete(agent) {
      chooseRowAction(DELETE_ITEM, `${agent} Session`);
      confirmDialog(t('common.delete'));
    },
    reports: (agent) => ({
      onSessionDeleted: [
        [
          {
            deletedSessionId: `session-${agent}`,
            nextSessionId: 'landing-session',
            agentAddress: agent,
          },
        ],
      ],
    }),
  },
  policy: {
    mock: () => api.setSessionCompactionPolicy,
    complete(agent) {
      chooseRowAction(t('sessions.compactionPolicy'), `${agent} Session`);
      buttonByText(t('common.save')).click();
    },
    reports: (agent, effective) => ({
      onCompactionPolicyChange: [[agent, `session-${agent}`, effective]],
    }),
  },
};

describe('SessionListDrawer row actions', () => {
  const drawer = setupSessionListDrawerSuite();

  it('portals the complete row menu, behind a vertical ellipsis, outside the clipped drawer', async () => {
    drawer.mount();
    await waitForCondition(() => rowCount() === 1);

    const trigger = document.querySelector('.session-row__menu-trigger');
    const dots = [...trigger.querySelectorAll('circle')];
    expect(
      dots.map((dot) => [dot.getAttribute('cx'), dot.getAttribute('cy')]),
    ).toEqual([
      ['8', '3'],
      ['8', '8'],
      ['8', '13'],
    ]);

    trigger.click();
    flushSync();
    await waitForCondition(
      () =>
        document.querySelector('.session-row__menu')?.style.visibility !==
        'hidden',
    );
    const menu = document.querySelector('.session-row__menu');
    expect(menu.parentElement).toBe(document.body);
    expect(document.querySelector('.session-drawer').contains(menu)).toBe(
      false,
    );
    expect(menu.dataset.positioning).toBe('fixed');
    expect(
      [...menu.querySelectorAll('.session-row__menu-item')].map((item) =>
        item.textContent.trim(),
      ),
    ).toEqual([
      t('sessions.rename'),
      t('sessions.compactionPolicy'),
      DELETE_ITEM,
    ]);
  });

  it('renames inline: IME keys stay with the composition, Escape cancels and Enter saves', async () => {
    drawer.mount();
    await waitForCondition(() => rowCount() === 1);
    const loadsBefore = api.listSessions.mock.calls.length;

    chooseRowAction(t('sessions.rename'));
    typeRename('Abandoned');
    keydown(document.querySelector('.session-row__edit-input'), 'Escape');
    flushSync();
    expect(document.querySelector('.session-row__edit-input')).toBeNull();

    chooseRowAction(t('sessions.rename'));
    const input = typeRename('Release planning');
    // An Enter or Escape that confirms or cancels an IME candidate stays with
    // the composition instead of committing or abandoning the rename.
    for (const key of ['Enter', 'Escape']) {
      keydown(input, key, { isComposing: true });
    }
    flushSync();
    await Promise.resolve();
    expect(api.renameSession).not.toHaveBeenCalled();
    expect(document.querySelector('.session-row__edit-input')).toBe(input);

    keydown(input, 'Enter');
    flushSync();
    await waitForCondition(() => api.renameSession.mock.calls.length === 1);
    expect(api.renameSession).toHaveBeenCalledWith(
      'alpha',
      'session-1',
      'Release planning',
    );
    // A successful rename re-fetches so the row shows the server-stored title.
    await waitForCondition(
      () => api.listSessions.mock.calls.length === loadsBefore + 1,
    );
  });

  it('deletes a Session only after the confirm dialog is accepted', async () => {
    const onSessionDeleted = vi.fn();
    drawer.mount({ onSessionDeleted });
    await waitForCondition(() => rowCount() === 1);
    const loadsBefore = api.listSessions.mock.calls.length;

    chooseRowAction(DELETE_ITEM);
    confirmDialog(t('common.cancel'));
    flushSync();
    expect(document.querySelector('.modal-footer')).toBeNull();
    expect(api.deleteSession).not.toHaveBeenCalled();

    chooseRowAction(DELETE_ITEM);
    expect(api.deleteSession).not.toHaveBeenCalled();
    confirmDialog(t('common.delete'));
    flushSync();

    // The callback fires only after the delete request resolved.
    await waitForCondition(() => onSessionDeleted.mock.calls.length === 1);
    expect(api.deleteSession).toHaveBeenCalledWith('alpha', 'session-1');
    expect(onSessionDeleted).toHaveBeenCalledWith({
      deletedSessionId: 'session-1',
      nextSessionId: 'session-2',
      agentAddress: 'alpha',
    });
    // A successful delete re-fetches so the removed row disappears.
    await waitForCondition(
      () => api.listSessions.mock.calls.length === loadsBefore + 1,
    );
  });

  it.each([
    {
      name: 'a busy Session',
      error: Object.assign(
        new Error(
          'cannot delete session with an active or queued run: session-1',
        ),
        { code: 'session_busy' },
      ),
      shown: [t('sessions.delete_busy')],
      hidden: ['cannot delete session'],
    },
    {
      // The refusal names what uses the Session by the names the user knows,
      // not by the internal references of the server message.
      name: 'a Session that automations use',
      error: Object.assign(
        new Error(
          'cannot delete Session referenced by calendar:act-1, cron:cron-1',
        ),
        {
          code: 'session_in_use',
          details: {
            data: {
              references: [
                { kind: 'calendar', id: 'act-1', name: 'Weekly review' },
                { kind: 'cron', id: 'cron-1', name: 'Daily report' },
              ],
            },
          },
        },
      ),
      shown: ['Weekly review', 'Daily report'],
      hidden: ['calendar:act-1', 'cron:cron-1'],
    },
  ])(
    'surfaces a refused delete of $name as an inline error',
    async ({ error, shown, hidden }) => {
      api.deleteSession.mockRejectedValueOnce(error);
      drawer.mount();
      await waitForCondition(() => rowCount() === 1);

      chooseRowAction(DELETE_ITEM);
      confirmDialog(t('common.delete'));
      flushSync();

      await waitForCondition(
        () => document.querySelector('.session-drawer__state--error') !== null,
      );
      const text = document.querySelector(
        '.session-drawer__state--error',
      ).textContent;
      for (const part of shown) {
        expect(text).toContain(part);
      }
      for (const part of hidden) {
        expect(text).not.toContain(part);
      }
    },
  );

  it('hands a saved Session Compaction Policy to Chat', async () => {
    const effective = {
      enabled: false,
      trigger: { type: 'context_ratio', threshold: 0.8 },
      strategy: { type: 'summary_tail', tail_tokens: 15000 },
    };
    api.setSessionCompactionPolicy.mockResolvedValue({
      override: null,
      effective,
    });
    const onCompactionPolicyChange = vi.fn();
    drawer.mount({ onCompactionPolicyChange });
    await waitForCondition(() => rowCount() === 1);

    chooseRowAction(t('sessions.compactionPolicy'));
    buttonByText(t('common.save')).click();

    await vi.waitFor(() =>
      expect(onCompactionPolicyChange).toHaveBeenCalledWith(
        'alpha',
        'session-1',
        effective,
      ),
    );
    expect(api.setSessionCompactionPolicy).toHaveBeenCalledWith(
      'alpha',
      'session-1',
      null,
    );
  });

  it.each([
    ['rename', 'Agent selection', 'alpha', 'beta'],
    ['delete', 'Agent selection', 'alpha', 'beta'],
    ['policy', 'Agent selection', 'alpha', 'beta'],
    ['rename', 'All agents filter', 'beta', 'alpha'],
    ['delete', 'All agents filter', 'beta', 'alpha'],
  ])(
    'refreshes the current list after a pending %s and a change to %s',
    async (operation, transition, mutationAgent, displayedAgent) => {
      const changeAgent = transition === 'Agent selection';
      const mutation = MUTATIONS[operation];
      let finishMutation;
      mutation
        .mock()
        .mockImplementationOnce(
          () => new Promise((resolve) => (finishMutation = resolve)),
        );
      api.listSessions.mockImplementation(async (requested, query) => {
        const addresses = Array.isArray(requested) ? requested : [requested];
        // Mirror the server contract: a required Session must belong to one
        // of the requested Agents, including during post-mutation refreshes.
        if (!addresses.includes(query.requiredSession?.agentId)) {
          throw new Error('required Session is outside the requested Agents');
        }
        const requestNumber = api.listSessions.mock.calls.length;
        return {
          sessions: addresses.map((address) => ({
            id: `session-${address}`,
            agent_address: address,
            title: `${address} Session ${requestNumber}`,
          })),
        };
      });
      const callbacks = {
        onSessionDeleted: vi.fn(),
        onCompactionPolicyChange: vi.fn(),
      };
      const props = drawer.mount({
        agentId: 'alpha',
        currentSessionId: 'session-alpha',
        agents: [{ address: 'alpha' }, { address: 'beta' }],
        initialFilters: { allAgents: !changeAgent },
        ...callbacks,
      });
      await waitForCondition(() => rowCount() === (changeAgent ? 1 : 2));

      mutation.complete(mutationAgent);
      flushSync();
      await waitForCondition(() => mutation.mock().mock.calls.length === 1);

      if (changeAgent) {
        props.agentId = displayedAgent;
        props.currentSessionId = `session-${displayedAgent}`;
      } else {
        document
          .querySelector(`[aria-label="${t('sessions.filters.allAgents')}"]`)
          .click();
      }
      flushSync();
      const firstName = () =>
        document.querySelector('.session-row__name')?.textContent ?? '';
      await waitForCondition(() =>
        firstName().includes(`${displayedAgent} Session 2`),
      );
      const effective = { enabled: false };
      finishMutation({ next_session_id: 'landing-session', effective });
      flushSync();

      // The final server snapshot must reach the new list without reverting
      // its Agent scope or reporting a mismatched required-Session error.
      await waitForCondition(() =>
        firstName().includes(`${displayedAgent} Session 3`),
      );
      expect(rowCount()).toBe(1);
      expect(document.querySelector('[role="alert"]')).toBeNull();
      expect(api.listSessions.mock.calls.at(-1)[0]).toBe(displayedAgent);
      expect(mutation.mock().mock.calls[0].slice(0, 2)).toEqual([
        mutationAgent,
        `session-${mutationAgent}`,
      ]);
      expect({
        onSessionDeleted: callbacks.onSessionDeleted.mock.calls,
        onCompactionPolicyChange: callbacks.onCompactionPolicyChange.mock.calls,
      }).toEqual({
        onSessionDeleted: [],
        onCompactionPolicyChange: [],
        ...mutation.reports(mutationAgent, effective),
      });
    },
  );
});
