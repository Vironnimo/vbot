// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  profile,
  swarm,
  button,
  createBridge,
  overrideOperations,
  callsTo,
  settle,
  render,
  openSwarm,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';
import { sessionReferenceRules } from '../../../../resources/extensions/swarm/ui/referenceLinks.js';

const participantTab = () =>
  document.getElementById('swarm-run-tab-participants');
const participantPanel = () =>
  document.getElementById('swarm-run-panel-participants');

describe('Swarm participant Sessions', () => {
  it('keeps display names and numeric links available as the roster outgrows one reference rule', () => {
    const roster = (length) =>
      Array.from({ length }, (_, i) => ({
        id: `prt-${i}`,
        display_name: `Participant ${i}`,
      }));
    for (const length of [257, 3000]) {
      const rules = sessionReferenceRules({
        participants: roster(length),
        newest_post_sequence: 0,
        newest_wiki_page_number: 1,
      });
      expect(rules.slice(0, 2).map((rule) => rule.prefix)).toEqual(['#', 'w']);
      expect(rules.length).toBeLessThanOrEqual(8);
      expect(
        new TextEncoder().encode(JSON.stringify(rules)).length,
      ).toBeLessThan(64 * 1024);
      const groups = rules.slice(2).map((rule) => rule.names);
      expect(groups.every((names) => names.length <= 512)).toBe(true);
      expect(groups.flat()).toContainEqual(['Participant 256', 'prt-256']);
      if (length === 257)
        expect(groups.flat()).toContainEqual(['prt-256', 'prt-256']);
    }
  });

  it('opens the selected Session directly, releases it on switching or leaving, and remembers the selection across tabs', async () => {
    const { bridge, operation } = createBridge();
    await openSwarm(bridge);
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(bridge.mountSession).toHaveBeenCalledTimes(1),
    );
    const first = bridge.mountSession.mock.lastCall;
    expect(participantPanel().contains(first[0])).toBe(true);
    expect(first.slice(1, 3)).toEqual([swarm.id, 'prt-a']);
    expect(first[4]).toBe('full');
    expect(participantTab().getAttribute('aria-selected')).toBe('true');
    expect(participantPanel().getAttribute('aria-labelledby')).toBe(
      participantTab().id,
    );
    expect(callsTo(operation, 'catalog')).toHaveLength(0);
    const releaseAlpha = bridge.mountSession.mock.results[0].value;

    button('Beta').click();
    await vi.waitFor(() =>
      expect(bridge.mountSession).toHaveBeenCalledTimes(2),
    );
    expect(releaseAlpha).toHaveBeenCalledTimes(1);
    expect(bridge.mountSession.mock.lastCall.slice(1, 3)).toEqual([
      swarm.id,
      'prt-b',
    ]);
    const releaseBeta = bridge.mountSession.mock.results[1].value;
    button(t('swarm.tabs.board')).click();
    await vi.waitFor(() => expect(releaseBeta).toHaveBeenCalledTimes(1));

    participantTab().click();
    await vi.waitFor(() =>
      expect(bridge.mountSession).toHaveBeenCalledTimes(3),
    );
    expect(bridge.mountSession.mock.lastCall.slice(1, 3)).toEqual([
      swarm.id,
      'prt-b',
    ]);
    button(t('common.close')).click();
    await tick();
    expect(bridge.mountSession.mock.results[2].value).toHaveBeenCalledTimes(1);
    expect(participantPanel().textContent).toContain(t('swarm.activity.empty'));
  });

  it('honors the accepted tab route and ignores a late reply after choosing another participant', async () => {
    const { bridge } = createBridge();
    await openSwarm(bridge);
    const pending = Promise.withResolvers();
    bridge.pushRoute.mockReturnValueOnce(pending.promise);
    button('Beta').click();
    await tick();
    expect(bridge.mountSession).not.toHaveBeenCalled();
    expect(bridge.route).toBe('/swarms/swr-a');
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(bridge.mountSession).toHaveBeenCalledTimes(1),
    );
    pending.resolve({});
    await settle();
    expect(bridge.mountSession.mock.lastCall.slice(1, 3)).toEqual([
      swarm.id,
      'prt-a',
    ]);
    expect(bridge.mountSession).toHaveBeenCalledTimes(1);
  });

  it('resumes only the selected failed, cancelled or interrupted participant', async () => {
    const stopped = [
      ['prt-b', 'Beta', 'failed', 'not_started'],
      ['prt-c', 'Gamma', 'cancelled', null],
      ['prt-d', 'Delta', 'interrupted', 'network'],
    ];
    const { bridge, operation } = createBridge(profile, {
      ...swarm,
      participants: [
        { ...swarm.participants[0], run_active: true },
        ...stopped.map(([id, display_name, state, stop_reason]) => ({
          ...swarm.participants[1],
          id,
          display_name,
          state,
          stop_reason,
          run_active: false,
        })),
      ],
    });
    await openSwarm(bridge);
    const resume = () => button(t('swarm.resumeParticipant'));
    const notices = {
      not_started: t('swarm.activity.stop.notStarted'),
      network: t('swarm.activity.stop.network'),
    };
    for (const [id, name, , reason] of stopped) {
      button(name).click();
      await vi.waitFor(() =>
        expect(bridge.mountSession.mock.lastCall?.[2]).toBe(id),
      );
      const stop = participantPanel().querySelector('.activity-stop');
      expect(stop?.textContent.trim()).toBe(notices[reason]);
      resume().click();
      await vi.waitFor(() =>
        expect(operation).toHaveBeenCalledWith(
          'swarms.resume',
          expect.objectContaining({
            swarm_id: swarm.id,
            participant_id: id,
          }),
        ),
      );
    }
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(bridge.mountSession.mock.lastCall?.[2]).toBe('prt-a'),
    );
    expect(resume()).toBeUndefined();
  });

  it('refreshes pending counts while the participant stays in the same active Run', async () => {
    const { bridge, operation } = createBridge();
    let pendingCount = 12;
    overrideOperations(operation, {
      'swarms.get': () => ({
        swarm: {
          ...structuredClone(swarm),
          participants: swarm.participants.map((participant) => ({
            ...participant,
            lifecycle_run_id: 'run-pending',
            pending_count: participant.id === 'prt-a' ? pendingCount : 0,
          })),
        },
      }),
    });
    await render(bridge);
    button('Investigate').click();
    const pending = () =>
      button('Alpha')?.getAttribute('aria-label') ||
      button('Alpha')?.querySelector('small')?.textContent;
    await vi.waitFor(() => expect(pending()).toMatch(/12\s+pending/));
    for (const remaining of [8, 4, 0]) {
      pendingCount = remaining;
      bridge.invalidate();
      await vi.waitFor(() =>
        expect(pending()).toMatch(
          new RegExp(`running.*${remaining}\\s+pending`, 'i'),
        ),
      );
    }
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(bridge.mountSession).toHaveBeenCalledTimes(1),
    );
    pendingCount = 2;
    bridge.invalidate();
    await vi.waitFor(() => expect(pending()).toMatch(/2\s+pending/));
    expect(bridge.mountSession).toHaveBeenCalledTimes(1);
  });
});
