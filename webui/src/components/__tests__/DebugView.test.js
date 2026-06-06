// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const debugStatusMock = vi.fn();
const debugTraceListMock = vi.fn();
const debugTraceGetMock = vi.fn();
const debugTraceClearMock = vi.fn();
const debugModelProbeMock = vi.fn();
const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('../../lib/api.js', () => ({
  debugStatus: (...args) => debugStatusMock(...args),
  debugTraceList: (...args) => debugTraceListMock(...args),
  debugTraceGet: (...args) => debugTraceGetMock(...args),
  debugTraceClear: (...args) => debugTraceClearMock(...args),
  debugModelProbe: (...args) => debugModelProbeMock(...args),
  rpc: (...args) => rpcMock(...args),
}));

const { default: DebugView } = await import('../DebugView.svelte');

describe('DebugView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
    mountedComponent = null;

    debugStatusMock.mockReset();
    debugTraceListMock.mockReset();
    debugTraceGetMock.mockReset();
    debugTraceClearMock.mockReset();
    debugModelProbeMock.mockReset();
    rpcMock.mockReset();

    debugStatusMock.mockResolvedValue({
      enabled: true,
      trace_limit: 50,
      trace_count: 0,
      data_directory: 'C:/data/debug',
    });
    debugTraceListMock.mockResolvedValue({ traces: [] });
    debugTraceClearMock.mockResolvedValue({ cleared: true });
    debugModelProbeMock.mockResolvedValue({});
    rpcMock.mockImplementation(async (method) => {
      if (method === 'settings.get') {
        return {
          general: { server: { listen_host: '127.0.0.1', listen_port: 8420 } },
          providers: { items: [] },
        };
      }
      throw new Error(`Unexpected RPC method: ${method}`);
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    localStorage.clear();
    vi.useRealTimers();
  });

  it('renders a useful empty state heading instead of (none)', async () => {
    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('No traces captured yet');

    const text = document.body.textContent ?? '';
    expect(text).toContain('No traces captured yet');
    expect(text).toContain('Enable debug mode in Settings');
    expect(text).not.toContain('(none)');
  });

  it('exposes full provider and model values via the title attribute on trace rows', async () => {
    debugTraceListMock.mockResolvedValue({
      traces: [
        traceListEntry({
          trace_id: 'trace-long',
          provider_id: 'openai-subscription-with-a-very-long-name',
          model_id: 'gpt-5.2-with-extra-context-and-suffix',
        }),
      ],
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2-with-extra-context-and-suffix');

    const providerCell = document.querySelector('.debug-trace__provider');
    const modelCell = document.querySelector('.debug-trace__model');

    expect(providerCell?.getAttribute('title')).toBe(
      'openai-subscription-with-a-very-long-name',
    );
    expect(modelCell?.getAttribute('title')).toBe(
      'gpt-5.2-with-extra-context-and-suffix',
    );
  });

  it('toggles a row-level full-value expand affordance and switches off when clicked again', async () => {
    debugTraceListMock.mockResolvedValue({
      traces: [
        traceListEntry({
          trace_id: 'trace-expand',
          provider_id: 'openai',
          model_id: 'gpt-5.2',
        }),
      ],
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2');

    const row = document.querySelector('.debug-trace');
    expect(row?.classList.contains('debug-trace--expanded')).toBe(false);

    const expandButton = document.querySelector('.debug-trace__expand');
    expect(expandButton).toBeTruthy();
    expect(expandButton?.getAttribute('aria-expanded')).toBe('false');

    expandButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(row?.classList.contains('debug-trace--expanded')).toBe(true);
    expect(
      document
        .querySelector('.debug-trace__expand')
        ?.getAttribute('aria-expanded'),
    ).toBe('true');

    document
      .querySelector('.debug-trace__expand')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(row?.classList.contains('debug-trace--expanded')).toBe(false);
    expect(
      document
        .querySelector('.debug-trace__expand')
        ?.getAttribute('aria-expanded'),
    ).toBe('false');
  });

  it('keeps the latest selection visible when an earlier click resolves after a later click', async () => {
    const traceA = traceListEntry({
      trace_id: 'trace-a',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    const traceB = traceListEntry({
      trace_id: 'trace-b',
      provider_id: 'anthropic',
      model_id: 'claude-sonnet-4',
    });

    debugTraceListMock.mockResolvedValue({ traces: [traceA, traceB] });

    let resolveA;
    const pendingA = new Promise((resolve) => {
      resolveA = resolve;
    });
    let resolveB;
    const pendingB = new Promise((resolve) => {
      resolveB = resolve;
    });

    debugTraceGetMock.mockImplementation(async (traceId) => {
      if (traceId === 'trace-a') {
        await pendingA;
        return {
          trace: fullTraceFixture(traceA, {
            request: { method: 'POST', body: '{"trace":"a"}' },
            response: { status_code: 200, body: '{"trace":"a"}' },
          }),
        };
      }
      if (traceId === 'trace-b') {
        await pendingB;
        return {
          trace: fullTraceFixture(traceB, {
            request: { method: 'POST', body: '{"trace":"b"}' },
            response: { status_code: 200, body: '{"trace":"b"}' },
          }),
        };
      }
      throw new Error(`Unexpected trace id: ${traceId}`);
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('claude-sonnet-4');

    clickTraceRow('trace-a');
    flushSync();
    clickTraceRow('trace-b');
    flushSync();

    resolveB();
    flushSync();
    await switchToDetailTabWhenReady('Request');

    expect(getBodyBlockText()).toContain('"trace":"b"');

    resolveA();
    flushSync();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(getSelectedTraceId()).toBe('trace-b');
    await switchToDetailTabWhenReady('Request');
    flushSync();
    expect(getBodyBlockText()).toContain('"trace":"b"');
    expect(getBodyBlockText()).not.toContain('"trace":"a"');
  });

  it('shows raw and formatted request body panes with the raw view selected by default', async () => {
    const trace = traceListEntry({
      trace_id: 'trace-body',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    debugTraceListMock.mockResolvedValue({ traces: [trace] });
    debugTraceGetMock.mockResolvedValue({
      trace: fullTraceFixture(trace, {
        request: {
          method: 'POST',
          url: 'https://api.openai.com/v1/responses',
          headers: { 'content-type': 'application/json' },
          body: '{"prompt":"hi","options":{"temperature":0.7}}',
        },
        response: {
          status_code: 200,
          body: '{"ok":true,"answer":"42"}',
        },
      }),
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2');

    clickTraceRow('trace-body');
    flushSync();
    await switchToDetailTabWhenReady('Request');
    flushSync();
    await waitForBodyText('"prompt":"hi"');

    const requestBlock = getBodyBlock();
    expect(
      requestBlock?.classList.contains('debug-view__code-block--raw'),
    ).toBe(true);
    expect(requestBlock?.textContent).toBe(
      '{"prompt":"hi","options":{"temperature":0.7}}',
    );

    const tabs = Array.from(
      document.querySelectorAll('.debug-view__body-tabs .debug-view__body-tab'),
    );
    expect(tabs).toHaveLength(2);
    expect(tabs[0]?.textContent?.trim()).toBe('Raw');
    expect(tabs[1]?.textContent?.trim()).toBe('Parsed');

    tabs[1]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const formattedBlock = getBodyBlock();
    expect(
      formattedBlock?.classList.contains('debug-view__code-block--formatted'),
    ).toBe(true);
    expect(formattedBlock?.textContent).toBe(
      '{\n  "prompt": "hi",\n  "options": {\n    "temperature": 0.7\n  }\n}',
    );
  });

  it('omits the body view toggle when the body is not parseable JSON', async () => {
    const trace = traceListEntry({
      trace_id: 'trace-no-json',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    debugTraceListMock.mockResolvedValue({ traces: [trace] });
    debugTraceGetMock.mockResolvedValue({
      trace: fullTraceFixture(trace, {
        request: {
          method: 'POST',
          body: 'this is plain text that is not JSON',
        },
        response: {
          status_code: 200,
          body: 'plain text response',
        },
      }),
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2');

    clickTraceRow('trace-no-json');
    flushSync();
    await switchToDetailTabWhenReady('Request');
    flushSync();
    await waitForBodyText('this is plain text that is not JSON');

    expect(
      document.querySelector(
        '.debug-view__detail-section .debug-view__body-tabs',
      ),
    ).toBeNull();
    expect(getBodyBlock()?.textContent).toBe(
      'this is plain text that is not JSON',
    );
  });

  it('shows the aggregate streaming response body under the Response tab and exposes no Stream Events tab', async () => {
    const trace = traceListEntry({
      trace_id: 'trace-stream',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    const aggregateStreamBody =
      'event: message\ndata: {"delta":"hi"}\n\n' +
      'event: message\ndata: {"delta":" there"}\n\n' +
      'data: [DONE]\n\n';

    debugTraceListMock.mockResolvedValue({ traces: [trace] });
    debugTraceGetMock.mockResolvedValue({
      trace: fullTraceFixture(trace, {
        request: {
          method: 'POST',
          url: 'https://api.openai.com/v1/responses',
          headers: { 'content-type': 'application/json' },
          body: '{"stream":true}',
        },
        response: {
          status_code: 200,
          headers: { 'content-type': 'text/event-stream' },
          body: aggregateStreamBody,
        },
      }),
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2');

    clickTraceRow('trace-stream');
    flushSync();
    await switchToDetailTabWhenReady('Response');
    flushSync();

    const tabLabels = Array.from(
      document.querySelectorAll('.debug-view__tab'),
    ).map((tab) => tab.textContent?.trim() ?? '');
    expect(tabLabels).toEqual(['Metadata', 'Request', 'Response']);
    expect(tabLabels).not.toContain('Stream Events');

    await waitForBodyText('data: [DONE]');

    const responseBlock = getBodyBlock();
    expect(
      responseBlock?.classList.contains('debug-view__code-block--raw'),
    ).toBe(true);
    expect(responseBlock?.textContent).toBe(aggregateStreamBody);
    expect(responseBlock?.textContent).toContain('"delta":"hi"');
    expect(responseBlock?.textContent).toContain('"delta":" there"');
  });

  it('keeps the selected list entry when refreshTraces returns a list still containing the id', async () => {
    const traceA = traceListEntry({
      trace_id: 'trace-keep',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    const traceB = traceListEntry({
      trace_id: 'trace-other',
      provider_id: 'anthropic',
      model_id: 'claude-sonnet-4',
    });
    debugTraceListMock.mockResolvedValueOnce({ traces: [traceA, traceB] });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('claude-sonnet-4');

    debugTraceGetMock.mockResolvedValue({
      trace: fullTraceFixture(traceA, {
        request: { method: 'POST', body: '{"x":1}' },
      }),
    });

    clickTraceRow('trace-keep');
    flushSync();
    await switchToDetailTabWhenReady('Request');
    flushSync();
    await waitForBodyText('"x":1');

    expect(getSelectedTraceId()).toBe('trace-keep');

    debugTraceListMock.mockResolvedValueOnce({
      traces: [
        traceA,
        traceB,
        traceListEntry({
          trace_id: 'trace-fresh',
          provider_id: 'openai',
          model_id: 'gpt-5.2',
        }),
      ],
    });

    clickRefresh();
    await waitForCondition(
      () =>
        document.querySelectorAll('.debug-trace[data-trace-id]').length === 3,
    );

    expect(getSelectedTraceId()).toBe('trace-keep');
  });

  it('clears the selection when refreshTraces returns a list without the selected id', async () => {
    const traceA = traceListEntry({
      trace_id: 'trace-vanish',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    debugTraceListMock.mockResolvedValueOnce({ traces: [traceA] });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2');

    clickTraceRow('trace-vanish');
    flushSync();

    debugTraceListMock.mockResolvedValueOnce({ traces: [] });
    clickRefresh();
    await waitForCondition(() =>
      (document.body.textContent ?? '').includes('No traces captured yet'),
    );

    expect(document.querySelector('.debug-view__detail-panel')).toBeNull();
  });

  it('falls back to the placeholder when headers are missing and never shows (none)', async () => {
    const trace = traceListEntry({
      trace_id: 'trace-headers',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
    });
    debugTraceListMock.mockResolvedValue({ traces: [trace] });
    debugTraceGetMock.mockResolvedValue({
      trace: {
        ...fullTraceFixture(trace, {
          request: { method: 'POST' },
          response: { status_code: 200 },
        }),
        request: {
          method: 'POST',
          url: 'https://api.openai.com/v1/responses',
          body: '{}',
        },
        response: {
          status_code: 200,
          body: '{}',
        },
      },
    });

    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForText('gpt-5.2');

    clickTraceRow('trace-headers');
    flushSync();
    await switchToDetailTabWhenReady('Request');
    flushSync();
    expect(getHeadersBlockText()).toBe('—');

    await switchToDetailTabWhenReady('Response');
    flushSync();
    expect(getHeadersBlockText()).toBe('—');
    flushSync();
    expect(getHeadersBlockText()).toBe('—');

    expect(document.body.textContent ?? '').not.toContain('(none)');
  });
});

function traceListEntry(overrides = {}) {
  return {
    trace_id: 'trace-default',
    timestamp: '2026-05-11T10:00:00Z',
    provider_id: 'openai',
    model_id: 'gpt-5.2',
    method: 'POST',
    url: 'https://api.openai.com/v1/responses',
    status_code: 200,
    duration_ms: 1234,
    type: 'provider_request',
    ...overrides,
  };
}

function fullTraceFixture(listEntry, overrides = {}) {
  return {
    trace_id: listEntry.trace_id,
    type: listEntry.type ?? 'provider_request',
    timestamp: listEntry.timestamp ?? '2026-05-11T10:00:00Z',
    duration_ms: listEntry.duration_ms ?? 1234,
    context: {
      run_id: 'run-1',
      agent_id: 'agent-1',
      session_id: 'session-1',
      connection_id: 'default',
      iteration_number: 1,
      streaming: false,
    },
    provider_id: listEntry.provider_id,
    model_id: listEntry.model_id,
    request: {
      method: 'POST',
      url: 'https://api.openai.com/v1/responses',
      headers: { 'content-type': 'application/json' },
      body: '{}',
      ...overrides.request,
    },
    response: {
      status_code: 200,
      headers: {},
      body: '{}',
      ...overrides.response,
    },
    ...(overrides.stream ? { stream: overrides.stream } : {}),
  };
}

function clickTraceRow(traceId) {
  const container = document.querySelector(
    `.debug-trace[data-trace-id="${traceId}"]`,
  );
  const row = container?.querySelector('.debug-trace__row');
  if (!row) {
    throw new Error(`Trace row not found for ${traceId}`);
  }
  row.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

function clickRefresh() {
  const button = document.querySelector('.debug-view__refresh-btn');
  if (!button) {
    throw new Error('Refresh button not found');
  }
  button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

async function switchToDetailTabWhenReady(label, attempts = 40) {
  for (let i = 0; i < attempts; i += 1) {
    flushSync();
    const tab = Array.from(document.querySelectorAll('.debug-view__tab')).find(
      (button) => button.textContent?.trim() === label,
    );
    if (tab) {
      tab.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      flushSync();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`Detail tab not found: ${label}`);
}

function getSelectedTraceId() {
  const selectedRow = document.querySelector('.debug-trace--selected');
  return selectedRow?.getAttribute('data-trace-id') ?? null;
}

function getBodyBlock() {
  const sections = Array.from(
    document.querySelectorAll('.debug-view__detail-section'),
  );
  const target = sections.find((section) => {
    const heading = section.querySelector('.debug-view__detail-heading');
    return heading?.textContent?.trim().toLowerCase() === 'body';
  });
  if (!target) {
    return null;
  }
  return target.querySelector('pre.debug-view__code-block');
}

function getBodyBlockText() {
  return getBodyBlock()?.textContent ?? '';
}

function getHeadersBlockText() {
  const sections = Array.from(
    document.querySelectorAll('.debug-view__detail-section'),
  );
  const target = sections.find((section) => {
    const heading = section.querySelector('.debug-view__detail-heading');
    return heading?.textContent?.trim().toLowerCase() === 'headers';
  });
  if (!target) {
    return '';
  }
  return target.querySelector('pre')?.textContent ?? '';
}

async function waitForText(text, attempts = 60) {
  for (let i = 0; i < attempts; i += 1) {
    flushSync();
    if ((document.body.textContent ?? '').includes(text)) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`Timed out waiting for text: ${text}`);
}

async function waitForBodyText(text, attempts = 60) {
  for (let i = 0; i < attempts; i += 1) {
    flushSync();
    if ((getBodyBlock()?.textContent ?? '').includes(text)) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`Timed out waiting for body text: ${text}`);
}

async function waitForCondition(check, attempts = 60) {
  for (let i = 0; i < attempts; i += 1) {
    flushSync();
    if (check()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error('Timed out waiting for condition');
}
