// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  swarm,
  button,
  createBridge,
  render,
} from './SwarmPage.support.js';

describe('SwarmPage', () => {
  it('opens participant Activity directly from the Board and detaches its Run when leaving', async () => {
    const { bridge } = createBridge();
    const previousRun = swarm.participants[0].lifecycle_run_id;
    swarm.participants[0].lifecycle_run_id = 'run-live-test';
    bridge.subscribeRun.mockResolvedValue({
      subscription_id: 'subscription-live-test',
    });
    try {
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.participant-pane')).not.toBeNull(),
      );
      button('Alpha').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.history')).not.toBeNull(),
      );
      expect(
        document.querySelector('[role="tab"][aria-selected="true"]')
          .textContent,
      ).toContain('Activity');
      expect(bridge.subscribeRun).toHaveBeenCalledWith(
        'swr-a',
        'run-live-test',
      );
      bridge.emitRun('subscription-live-test', {
        type: 'model_step_usage',
        run_id: 'run-live-test',
        sequence: 1,
        payload: { context_usage: { tokens: 2468, estimated: true } },
      });
      await tick();
      expect(document.querySelector('.context-usage').textContent).toContain(
        '~2,468',
      );
      button('New run').click();
      await tick();
      expect(bridge.unsubscribeRun).toHaveBeenCalledWith(
        'subscription-live-test',
      );
      expect(document.querySelector('.history')).toBeNull();
    } finally {
      swarm.participants[0].lifecycle_run_id = previousRun;
    }
  });

  it('reconciles canonical final output even when completion arrives before the subscription reply', async () => {
    const { bridge } = createBridge();
    const previousRun = swarm.participants[0].lifecycle_run_id;
    const previousActive = swarm.participants[0].run_active;
    swarm.participants[0].lifecycle_run_id = 'run-final-test';
    swarm.participants[0].run_active = true;
    bridge.readHistory.mockResolvedValueOnce({
      messages: [],
    });
    bridge.readHistory.mockResolvedValue({
      messages: [
        {
          id: 'msg-final-test',
          role: 'assistant',
          content: 'final-output-sentinel',
          timestamp: '2026-09-08T09:00:00+00:00',
        },
      ],
    });
    bridge.subscribeRun.mockImplementation(() => {
      bridge.emitRun('subscription-final-test', {
        type: 'run_completed',
        run_id: 'run-final-test',
        sequence: 1,
      });
      return Promise.resolve({ subscription_id: 'subscription-final-test' });
    });
    try {
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.participant-pane')).not.toBeNull(),
      );
      button('Alpha').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.history')?.textContent).toContain(
          'final-output-sentinel',
        ),
      );
      expect(bridge.readHistory).toHaveBeenCalledTimes(2);
      expect(document.querySelectorAll('.history article')).toHaveLength(1);
      expect(document.querySelector('.history .streaming-text')).toBeNull();
    } finally {
      swarm.participants[0].lifecycle_run_id = previousRun;
      swarm.participants[0].run_active = previousActive;
    }
  });

  it('renders canonical per-participant Model usage and tool-call totals', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    button('Usage').click();
    await tick();
    expect(
      [...document.querySelectorAll('.usage-summary dd')].map((el) =>
        el.textContent.trim(),
      ),
    ).toEqual(['65', '4']);
    expect(
      [...document.querySelectorAll('.table-wrap tbody tr')].map((row) =>
        [...row.querySelectorAll('td')].map((el) => el.textContent.trim()),
      ),
    ).toEqual([
      ['Alpha', 'demo/model', '65', '4', '1'],
      ['Beta', 'demo/fallback', '65', '0', '1'],
    ]);
    expect(document.body.textContent).toContain('Tool Calls');
    expect(document.body.textContent).toContain('Alpha');
    expect(document.body.textContent).toContain('demo/model');
    expect(operation).toHaveBeenCalledWith('swarms.usage', {
      swarm_id: 'swr-a',
      participant_id: 'prt-a',
    });
  });

  it('shows participant tool calls once across Models and without token usage', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    operation.mockImplementation(async (name, args) => {
      const result = await original(name, args);
      if (name === 'swarms.usage' && args.participant_id === 'prt-a') {
        result.usage.usage.models.push({
          ...result.usage.usage.models[0],
          model: 'second',
        });
      }
      if (name === 'swarms.usage' && args.participant_id === 'prt-b') {
        result.usage.usage = null;
        result.usage.tools.total_calls = 7;
      }
      return result;
    });
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Usage').click();
    await tick();
    const rows = [...document.querySelectorAll('.table-wrap tbody tr')];
    expect(
      rows.map((row) => [...row.cells].map((cell) => cell.textContent.trim())),
    ).toEqual([
      ['Alpha', 'demo/model', '65', '4', '1'],
      ['demo/second', '65', '1'],
      ['Beta', 'demo/fallback', 'Unavailable', '7', 'Unavailable'],
    ]);
    expect(rows[0].cells[0].rowSpan).toBe(2);
    expect(rows[0].cells[3].rowSpan).toBe(2);
  });

  it.each([
    [0, '0'],
    [999, '999'],
    [1000, '1 k'],
    [12523, '12.5 k'],
    [1000000, '1 mio'],
    [11512523, '11.5 mio'],
    [1250000000, '1.3 mrd'],
  ])('formats Usage counts consistently for %s', async (value, expected) => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    operation.mockImplementation(async (name, args) => {
      const result = await original(name, args);
      if (name === 'swarms.usage') {
        const counts = {
          measured_input_tokens: value / 2,
          measured_output_tokens: 0,
          estimated_input_tokens: 0,
          estimated_output_tokens: value / 2,
        };
        Object.assign(result.usage.usage.totals, counts);
        Object.assign(result.usage.usage.models[0], counts, { runs: value });
        result.usage.tools.total_calls = value;
      }
      return result;
    });
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Usage').click();
    await tick();
    expect(
      [...document.querySelectorAll('.usage-summary dd')].map((el) =>
        el.textContent.trim(),
      ),
    ).toEqual([expected, expected]);
    for (const row of document.querySelectorAll('.table-wrap tbody tr')) {
      expect(
        [...row.querySelectorAll('td')]
          .slice(2)
          .map((el) => el.textContent.trim()),
      ).toEqual([expected, expected, expected]);
    }
  });

  it('preserves unavailable totals and tool counts', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    operation.mockImplementation(async (name, args) => {
      const result = await original(name, args);
      if (name === 'swarms.usage') {
        result.usage.usage = null;
        result.usage.tools = null;
      }
      return result;
    });
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Usage').click();
    await tick();
    expect(
      [...document.querySelectorAll('.usage-summary dd')].map((el) =>
        el.textContent.trim(),
      ),
    ).toEqual(['Unavailable', 'Unavailable']);
    for (const row of document.querySelectorAll('.table-wrap tbody tr')) {
      expect(
        [...row.cells].slice(2).map((cell) => cell.textContent.trim()),
      ).toEqual(['Unavailable', 'Unavailable', 'Unavailable']);
    }
  });

  it('opens Activity links through the extension bridge', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Alpha').click();
    await tick();
    button('Activity').click();
    await tick();
    const link = document.querySelector('.history a[href]');
    expect(link).not.toBeNull();
    link.click();
    await tick();
    expect(operation).toHaveBeenCalledWith('link.open', {
      url: 'https://example.test/',
    });
  });
});
