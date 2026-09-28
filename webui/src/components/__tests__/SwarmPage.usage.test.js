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

const USAGE = t('swarm.tabs.usage');
const UNAVAILABLE = t('swarm.usage.unavailable');

const summary = () =>
  [...document.querySelectorAll('.usage-summary dd')].map((el) =>
    el.textContent.trim(),
  );
const tableRows = () => [...document.querySelectorAll('.table-wrap tbody tr')];
const cells = (row) => [...row.cells].map((cell) => cell.textContent.trim());

async function openUsage(bridge) {
  await openSwarm(bridge);
  await settle();
  button(USAGE).click();
  await vi.waitFor(() => expect(tableRows().length).toBeGreaterThan(0));
  await tick();
}
// Adjusts each fixture Usage reply before the page receives it.
function editUsage(operation, edit) {
  overrideOperations(operation, {
    'swarms.usage': async (_args, fallback) => {
      const result = await fallback();
      edit(result.usage);
      return result;
    },
  });
}
// A Usage report whose token, tool-call and Run counts all equal `value`.
function uniformReport(value, participantId = null) {
  const counts = {
    measured_input_tokens: value / 2,
    measured_output_tokens: 0,
    estimated_input_tokens: 0,
    estimated_output_tokens: value / 2,
  };
  return {
    participant_id: participantId,
    usage: {
      totals: counts,
      models: [{ provider: 'demo', model: 'model', runs: value, ...counts }],
    },
    tools: { total_calls: value },
  };
}

describe('Swarm Usage', () => {
  it('renders canonical per-participant Model usage and tool-call totals', async () => {
    const { bridge, operation } = createBridge();
    await openUsage(bridge);
    expect(summary()).toEqual(['65', '4']);
    expect(tableRows().map(cells)).toEqual([
      ['Alpha', 'demo/model', '65', '4', '1'],
      ['Beta', 'demo/fallback', '65', '0', '1'],
    ]);
    expect(document.body.textContent).toContain(t('swarm.usage.toolCalls'));
    expect(operation).toHaveBeenCalledWith('swarms.usage', {
      swarm_id: 'swr-a',
    });
    expect(callsTo(operation, 'swarms.usage')).toHaveLength(1);
    expect(callsTo(operation, 'swarms.events')).toHaveLength(0);
  });

  it('shows participant tool calls once across Models and without token usage', async () => {
    const { bridge, operation } = createBridge();
    editUsage(operation, (usage) => {
      const [first, second] = usage.participants;
      first.usage.models.push({ ...first.usage.models[0], model: 'second' });
      second.usage = null;
      second.tools.total_calls = 7;
    });
    await openUsage(bridge);
    const rows = tableRows();
    expect(rows.map(cells)).toEqual([
      ['Alpha', 'demo/model', '65', '4', '1'],
      ['demo/second', '65', '1'],
      ['Beta', 'demo/fallback', UNAVAILABLE, '7', UNAVAILABLE],
    ]);
    expect(rows[0].cells[0].rowSpan).toBe(2);
    expect(rows[0].cells[3].rowSpan).toBe(2);
  });

  it('formats Usage counts consistently across the summary and participant rows', async () => {
    const formats = [
      [0, '0'],
      [999, '999'],
      [1000, '1 k'],
      [12523, '12.5 k'],
      [1000000, '1 mio'],
      [11512523, '11.5 mio'],
      [1250000000, '1.3 mrd'],
    ];
    const participants = formats.map(([value]) => ({
      ...swarm.participants[0],
      id: `prt-${value}`,
      display_name: `Peer ${value}`,
    }));
    const { bridge, operation } = createBridge(profile, {
      ...structuredClone(swarm),
      participants,
    });
    overrideOperations(operation, {
      'swarms.usage': () => ({
        usage: {
          ...uniformReport(12523),
          participants: formats.map(([value]) =>
            uniformReport(value, `prt-${value}`),
          ),
        },
      }),
    });
    await openUsage(bridge);
    expect(summary()).toEqual(['12.5 k', '12.5 k']);
    expect(tableRows().map((row) => cells(row).slice(2))).toEqual(
      formats.map(([, expected]) => [expected, expected, expected]),
    );
  });

  it('preserves unavailable totals and tool counts', async () => {
    const { bridge, operation } = createBridge();
    editUsage(operation, (usage) => {
      for (const report of [usage, ...usage.participants]) {
        report.usage = null;
        report.tools = null;
      }
    });
    await openUsage(bridge);
    expect(summary()).toEqual([UNAVAILABLE, UNAVAILABLE]);
    for (const row of tableRows())
      expect(cells(row).slice(2)).toEqual([
        UNAVAILABLE,
        UNAVAILABLE,
        UNAVAILABLE,
      ]);
  });
});

describe('Swarm Usage refresh', () => {
  it('shares an in-flight Usage report while tabs and invalidations change', async () => {
    const { bridge, operation } = createBridge();
    let finish;
    overrideOperations(operation, {
      'swarms.usage': (_args, fallback) =>
        new Promise((resolve) => {
          finish = () => resolve(fallback());
        }),
    });
    await openSwarm(bridge);
    button(USAGE).click();
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    expect(document.querySelector('[role="status"]')).toBeNull();
    button(t('swarm.tabs.board')).click();
    await tick();
    button(USAGE).click();
    bridge.invalidate();
    await settle(150);
    expect(callsTo(operation, 'swarms.usage')).toHaveLength(1);
    finish();
    await vi.waitFor(() =>
      expect(document.querySelector('.usage-summary')).not.toBeNull(),
    );
    await tick();
    expect(document.querySelector('[role="status"]')).toBeNull();
  });

  it('refreshes Usage without inserting progress text or hiding the last report', async () => {
    const { bridge, operation } = createBridge();
    await openSwarm(bridge);
    button(USAGE).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.usage-summary')).not.toBeNull(),
    );
    const report = document.querySelector('.usage-summary');
    const before = report.textContent;
    let finishUsage;
    overrideOperations(operation, {
      'swarms.usage': () =>
        new Promise((resolve) => {
          finishUsage = resolve;
        }),
    });
    bridge.invalidate();
    await vi.waitFor(() => expect(finishUsage).toBeTypeOf('function'));
    expect(document.querySelector('.usage-summary')).toBe(report);
    expect(report.textContent).toBe(before);
    expect(
      report.closest('[role="tabpanel"]').querySelector('[role="status"]'),
    ).toBeNull();
    finishUsage({
      usage: {
        usage: { totals: { input_tokens: 987654 }, models: [] },
        tools: { total_calls: 42 },
        participants: [],
      },
    });
    await vi.waitFor(() => expect(report.textContent).not.toBe(before));
  });

  it('ignores a late Usage report from the previously selected Swarm', async () => {
    const { bridge, operation } = createBridge();
    const other = {
      ...structuredClone(swarm),
      id: 'swr-b',
      prompt: 'Second swarm',
    };
    const pending = [];
    const result = (id) => ({
      usage: {
        usage: {
          totals: {
            measured_input_tokens: id === 'swr-a' ? 111 : 222,
            measured_output_tokens: 0,
            estimated_input_tokens: 0,
            estimated_output_tokens: 0,
          },
          models: [],
        },
        tools: { total_calls: 0 },
      },
    });
    overrideOperations(operation, {
      'swarms.list': () => ({ entries: [swarm, other] }),
      'swarms.get': (args) => ({
        swarm: structuredClone(args.swarm_id === 'swr-b' ? other : swarm),
      }),
      'swarms.usage': (args) =>
        args.swarm_id === 'swr-a'
          ? new Promise((resolve) => pending.push(resolve))
          : result(args.swarm_id),
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button(USAGE)).toBeDefined());
    button(USAGE).click();
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    button('Second swarm').click();
    await vi.waitFor(() =>
      expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/swarms/swr-b'),
    );
    button(USAGE).click();
    await tick();
    const report = () => document.querySelector('.usage-summary').textContent;
    await vi.waitFor(() => expect(report()).toContain('222'));
    for (const resolve of pending) resolve(result('swr-a'));
    await settle();
    expect(report()).toContain('222');
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/swarms/swr-b');
  });
});
