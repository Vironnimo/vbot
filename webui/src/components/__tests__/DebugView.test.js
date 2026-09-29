// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../lib/tooltip.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const debugStatusMock = vi.fn();
const debugTraceListMock = vi.fn();
const debugTraceGetMock = vi.fn();
const debugTraceClearMock = vi.fn();
const debugModelProbeMock = vi.fn();
const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('../../lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    debugStatus: (...args) => debugStatusMock(...args),
    debugTraceList: (...args) => debugTraceListMock(...args),
    debugTraceGet: (...args) => debugTraceGetMock(...args),
    debugTraceClear: (...args) => debugTraceClearMock(...args),
    debugModelProbe: (...args) => debugModelProbeMock(...args),
  }),
);

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
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('renders the shared empty state when no traces exist', async () => {
    mountedComponent = mount(DebugView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('.debug-view .empty-state'),
    );

    expect(document.querySelector('.debug-view .empty-state')).toBeTruthy();
    expect(document.querySelector('.debug-view.view-frame')).toBeTruthy();
    expect(document.querySelector('.debug-view .view-header')).toBeTruthy();
    expect(
      document.querySelector('.debug-view .view-toolbar--split'),
    ).toBeTruthy();
  });

  it('exposes full provider and model values via the quick tooltip on trace rows', async () => {
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

    vi.useFakeTimers();
    expect(await hoveredTooltipText(providerCell)).toBe(
      'openai-subscription-with-a-very-long-name',
    );
    expect(await hoveredTooltipText(modelCell)).toBe(
      'gpt-5.2-with-extra-context-and-suffix',
    );
  });

  it('combines list search, status and Provider filters without changing the inspected trace', async () => {
    const a = traceListEntry({ trace_id: 'a' });
    const b = traceListEntry({
      trace_id: 'b',
      provider_id: 'anthropic',
      model_id: 'claude',
      status_code: 429,
    });
    debugTraceListMock.mockResolvedValue({ traces: [a, b] });
    debugTraceGetMock.mockResolvedValue({ trace: fullTraceFixture(a) });
    mountedComponent = mount(DebugView, { target: document.body });
    await waitForText('claude');
    clickTraceRow('a');
    await switchToDetailTabWhenReady('request');
    const search = document.querySelector('.trace-filters input');
    search.value = 'CLAUDE';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelectorAll('.debug-trace')).toHaveLength(1);
    expect(document.querySelector('.debug-trace').dataset.traceId).toBe('b');
    expect(document.querySelector('.detail-header h3').textContent).toBe(
      a.model_id,
    );
    // Both filters use the shared dropdown, not native selects.
    expect(document.querySelector('.debug-view select')).toBeNull();
    expect(
      document
        .getElementById('debug-trace-status-filter')
        .getAttribute('aria-label'),
    ).toBe('Status filter');
    expect(
      document
        .getElementById('debug-trace-provider-filter')
        .getAttribute('aria-label'),
    ).toBe('Provider');
    const allStatuses = t('debug.allStatuses');
    const statusOk = t('debug.statusOk');
    expect(dropdownOptionLabels('debug-trace-status-filter')).toEqual([
      allStatuses,
      statusOk,
      t('debug.statusErrors'),
      t('debug.statusOther'),
    ]);
    chooseDropdownOption('debug-trace-status-filter', statusOk);
    expect(dropdownTriggerLabel('debug-trace-status-filter')).toBe(statusOk);
    expect(document.querySelectorAll('.debug-trace')).toHaveLength(0);
    document.querySelector('.trace-no-matches button').click();
    flushSync();
    expect(document.querySelectorAll('.debug-trace')).toHaveLength(2);
    expect(dropdownTriggerLabel('debug-trace-status-filter')).toBe(allStatuses);
    expect(dropdownOptionLabels('debug-trace-provider-filter')).toEqual([
      t('debug.allProviders'),
      'anthropic',
      'openai',
    ]);
    chooseDropdownOption('debug-trace-provider-filter', 'anthropic');
    expect(document.querySelectorAll('.debug-trace')).toHaveLength(1);
    expect(document.querySelector('.debug-trace').dataset.traceId).toBe('b');
  });

  it('sets Model and Provider ids in Mono but keeps the Model Probe label in Sans', async () => {
    const request = traceListEntry({ trace_id: 'model-trace' });
    const probe = traceListEntry({
      trace_id: 'probe-trace',
      model_id: '',
      type: 'model_probe',
    });
    debugTraceListMock.mockResolvedValue({ traces: [request, probe] });
    debugTraceGetMock.mockResolvedValue({ trace: fullTraceFixture(request) });
    mountedComponent = mount(DebugView, { target: document.body });
    await waitForText('gpt-5.2');

    const modelCell = (traceId) =>
      document.querySelector(
        `.debug-trace[data-trace-id="${traceId}"] .debug-trace__model`,
      );
    expect(
      modelCell('model-trace').classList.contains('debug-trace__model--id'),
    ).toBe(true);
    expect(
      modelCell('probe-trace').classList.contains('debug-trace__model--id'),
    ).toBe(false);

    clickTraceRow('model-trace');
    await waitForCondition(() => document.querySelector('.detail-header h3'));
    const title = document.querySelector('.detail-header h3');
    expect(title.textContent).toBe('gpt-5.2');
    expect(title.classList.contains('detail-title--id')).toBe(true);
    expect(document.querySelector('.detail-provider').textContent).toBe(
      'openai',
    );
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
    await switchToDetailTabWhenReady('request');

    expect(getBodyBlockText()).toContain('"trace":"b"');

    resolveA();
    flushSync();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(getSelectedTraceId()).toBe('trace-b');
    await switchToDetailTabWhenReady('request');
    flushSync();
    expect(getBodyBlockText()).toContain('"trace":"b"');
    expect(getBodyBlockText()).not.toContain('"trace":"a"');
  });

  it('opens the Request in readable JSON and preserves exact raw and formatted alternatives', async () => {
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
    await waitForCondition(() =>
      document.querySelector('.debug-view__body-tab-list'),
    );
    expect(
      document
        .querySelector('#debug-detail-tab-request')
        .getAttribute('aria-selected'),
    ).toBe('true');
    expect(
      document
        .querySelector('#debug-request-body-tab-readable')
        .getAttribute('aria-selected'),
    ).toBe('true');
    expect(document.querySelector('.json-string').textContent).toBe('hi');
    await switchToDetailTabWhenReady('request');
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
      document.querySelectorAll('.debug-view__body-tab-list .tab-list__tab'),
    );
    expect(tabs).toHaveLength(3);
    expect(tabs[0]?.getAttribute('id')).toContain('readable');
    expect(tabs[2]?.getAttribute('aria-selected')).toBe('true');

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
    await switchToDetailTabWhenReady('response');
    flushSync();

    const tabLabels = Array.from(
      document.querySelectorAll('.debug-view__detail-tab-list .tab-list__tab'),
    ).map((tab) => tab.textContent?.trim() ?? '');
    expect(tabLabels).toEqual([
      t('debug.request'),
      t('debug.response'),
      t('debug.metadata'),
    ]);
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

  it('inspects the trace named by the place and steps through rows and back to the list', async () => {
    const traceA = traceListEntry({ trace_id: 'trace-a', model_id: 'gpt-5.2' });
    const traceB = traceListEntry({
      trace_id: 'trace-b',
      provider_id: 'anthropic',
      model_id: 'claude-sonnet-4',
    });
    debugTraceListMock.mockResolvedValue({ traces: [traceA, traceB] });
    debugTraceGetMock.mockImplementation(async (traceId) => ({
      trace: fullTraceFixture(traceId === 'trace-a' ? traceA : traceB),
    }));
    const navigation = createStandaloneNavigation(['trace-b']);
    mountedComponent = mount(DebugView, {
      target: document.body,
      props: { navigation },
    });

    await waitForCondition(
      () =>
        document.querySelector('.detail-header h3')?.textContent ===
        'claude-sonnet-4',
    );
    expect(getSelectedTraceId()).toBe('trace-b');

    clickTraceRow('trace-a');
    expect(navigation.place).toEqual(['trace-a']);
    await waitForCondition(
      () =>
        document.querySelector('.detail-header h3')?.textContent === 'gpt-5.2',
    );

    document.querySelector('.detail-back button').click();
    flushSync();
    expect(navigation.place).toEqual([]);
    expect(getSelectedTraceId()).toBeNull();
  });

  it('keeps the inspected trace across refreshes while it is listed and closes it once it is gone', async () => {
    const navigation = createStandaloneNavigation();
    const props = reactiveProps({ debugTracesRefreshToken: 0, navigation });
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
    debugTraceGetMock.mockResolvedValue({
      trace: fullTraceFixture(traceA, {
        request: { method: 'POST', body: '{"x":1}' },
      }),
    });

    mountedComponent = mount(DebugView, { target: document.body, props });
    await waitForText('claude-sonnet-4');
    clickTraceRow('trace-keep');
    await switchToDetailTabWhenReady('request');
    await waitForBodyText('"x":1');
    expect(navigation.place).toEqual(['trace-keep']);

    debugTraceListMock.mockResolvedValueOnce({
      traces: [traceA, traceB, traceListEntry({ trace_id: 'trace-fresh' })],
    });
    props.debugTracesRefreshToken += 1;
    await waitForCondition(
      () =>
        document.querySelectorAll('.debug-trace[data-trace-id]').length === 3,
    );
    expect(getSelectedTraceId()).toBe('trace-keep');
    expect(getBodyBlockText()).toBe('{"x":1}');

    debugTraceListMock.mockResolvedValueOnce({ traces: [] });
    props.debugTracesRefreshToken += 1;
    await waitForCondition(() =>
      document.querySelector('.debug-view .empty-state'),
    );
    expect(document.querySelector('.debug-view__detail-panel')).toBeNull();
    expect(document.querySelector('.debug-view__refresh-btn')).toBeNull();
    // The vanished trace's entry is corrected to the plain list.
    expect(navigation.place).toEqual([]);
  });

  it('falls back to the placeholder when headers are missing and never shows (none)', async () => {
    const trace = traceListEntry({ trace_id: 'trace-headers' });
    const detail = fullTraceFixture(trace);
    delete detail.request.headers;
    delete detail.response.headers;
    debugTraceListMock.mockResolvedValue({ traces: [trace] });
    debugTraceGetMock.mockResolvedValue({ trace: detail });

    mountedComponent = mount(DebugView, { target: document.body });
    await waitForText(trace.model_id);
    clickTraceRow('trace-headers');

    await switchToDetailTabWhenReady('request');
    expect(getHeadersBlockText()).toBe('—');
    await switchToDetailTabWhenReady('response');
    expect(getHeadersBlockText()).toBe('—');
  });

  it('shows a capture failure even if the HTTP status was successful, and retries detail loading', async () => {
    const entry = traceListEntry();
    const trace = {
      ...fullTraceFixture(entry),
      error: { type: 'ReadError', message: 'test-owned-stream-error' },
    };
    debugTraceListMock.mockResolvedValue({ traces: [entry] });
    debugTraceGetMock
      .mockRejectedValueOnce(new Error('test-owned-load-error'))
      .mockResolvedValueOnce({ trace });
    mountedComponent = mount(DebugView, { target: document.body });
    await waitForText(entry.model_id);
    clickTraceRow(entry.trace_id);
    await waitForText('test-owned-load-error');
    document
      .querySelector('.debug-view__detail-panel button.btn-secondary')
      .click();
    await waitForText('test-owned-stream-error');
    expect(document.querySelector('.detail-status').dataset.tone).toBe('error');
    expect(debugTraceGetMock).toHaveBeenCalledTimes(2);
  });

  it('does not resurrect a cleared trace when its pending detail arrives', async () => {
    const entry = traceListEntry();
    let resolveDetail;
    debugTraceListMock.mockResolvedValueOnce({ traces: [entry] });
    debugTraceGetMock.mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve;
      }),
    );
    mountedComponent = mount(DebugView, { target: document.body });
    await waitForText(entry.model_id);
    clickTraceRow(entry.trace_id);
    flushSync();
    clearAllTraces();
    await waitForCondition(() =>
      document.querySelector('.debug-view .empty-state'),
    );
    resolveDetail({ trace: fullTraceFixture(entry) });
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    expect(document.querySelector('.debug-view__detail-panel')).toBeNull();
    expect(debugTraceClearMock).toHaveBeenCalledOnce();
  });

  it('keeps a failed clear visible without dropping the inspected payload', async () => {
    const entry = traceListEntry();
    debugTraceListMock.mockResolvedValue({ traces: [entry] });
    debugTraceGetMock.mockResolvedValue({
      trace: fullTraceFixture(entry, {
        request: { body: 'test-owned-retained-body' },
      }),
    });
    debugTraceClearMock.mockRejectedValueOnce(
      new Error('test-owned-clear-error'),
    );
    mountedComponent = mount(DebugView, { target: document.body });
    await waitForText(entry.model_id);
    clickTraceRow(entry.trace_id);
    await switchToDetailTabWhenReady('request');
    clearAllTraces();
    await waitForText('test-owned-clear-error');
    expect(getBodyBlockText()).toBe('test-owned-retained-body');
    expect(debugTraceListMock).toHaveBeenCalledOnce();
  });

  it('preserves a newer retention draft through a slow save and validates before submission', async () => {
    let resolveSave;
    rpcMock.mockImplementation(async (method) => {
      if (method === 'settings.get') return { providers: { items: [] } };
      if (method === 'settings.update')
        return new Promise((resolve) => {
          resolveSave = resolve;
        });
      throw new Error(method);
    });
    mountedComponent = mount(DebugView, { target: document.body });
    await waitForCondition(() =>
      document.querySelector('.debug-view .empty-state'),
    );
    const input = document.querySelector('input[type="number"]');
    const save = storageButton(t('common.save'));
    input.value = '0';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    save.click();
    await waitForCondition(() =>
      document.querySelector('.debug-storage-content .banner--error'),
    );
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'settings.update'),
    ).toHaveLength(0);
    input.value = '75';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    save.click();
    await waitForCondition(() => resolveSave);
    input.value = '100';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    resolveSave({ debug: { trace_limit: 75 } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    expect(input.value).toBe('100');
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      debug: { trace_limit: 75 },
    });
  });

  it('does not overwrite saved retention with an older status refresh', async () => {
    const props = reactiveProps({ debugTracesRefreshToken: 0 });
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'settings.get') return { providers: { items: [] } };
      if (method === 'settings.update') return params;
      throw new Error(method);
    });
    mountedComponent = mount(DebugView, { target: document.body, props });
    await waitForCondition(() =>
      document.querySelector('.debug-view .empty-state'),
    );
    let resolveStatus;
    debugStatusMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveStatus = resolve;
        }),
    );
    props.debugTracesRefreshToken += 1;
    await waitForCondition(() => resolveStatus);
    const input = document.querySelector('input[type="number"]');
    input.value = '75';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    storageButton(t('common.save')).click();
    await waitForCondition(() =>
      document.querySelector('.debug-utilities').textContent.includes('/ 75'),
    );
    resolveStatus({ enabled: true, trace_limit: 50, trace_count: 0 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    expect(input.value).toBe('75');
    expect(document.querySelector('.debug-utilities').textContent).toContain(
      '/ 75',
    );
  });

  it('probes the selected Provider connection and shows its raw response and model preview', async () => {
    rpcMock.mockImplementation(async (method) => {
      if (method === 'settings.get') {
        return {
          general: { server: { listen_host: '127.0.0.1', listen_port: 8420 } },
          providers: {
            items: [
              {
                id: 'openai',
                name: 'OpenAI',
                models_endpoint: '/v1/models',
                connections: [{ id: 'default', name: 'Default' }],
              },
            ],
          },
        };
      }
      throw new Error(`Unexpected RPC method: ${method}`);
    });
    debugModelProbeMock
      .mockResolvedValueOnce(
        probeResult('{"data":[{"id":"gpt-5.2","object":"model"}]}', {
          model_count: 1,
          models: [{ id: 'gpt-5.2', name: 'GPT-5.2' }],
        }),
      )
      .mockResolvedValueOnce(probeResult('plain text response'));

    mountedComponent = mount(DebugView, { target: document.body });
    // Settings (and with them the probe Providers) resolve with the trace
    // list; the empty trace state marks that initial load as complete.
    await waitForCondition(() =>
      document.querySelector('.debug-view .empty-state'),
    );

    const providerTrigger = document.getElementById('debug-probe-provider');
    const connectionTrigger = document.getElementById('debug-probe-connection');
    expect(providerTrigger.getAttribute('aria-label')).toBe('Provider');
    expect(connectionTrigger.getAttribute('aria-label')).toBe('Connection');
    expect(dropdownTriggerLabel('debug-probe-provider')).toBe(
      t('debug.modelProbe.selectProvider'),
    );
    expect(connectionTrigger.disabled).toBe(true);

    chooseDropdownOption('debug-probe-provider', 'OpenAI');
    expect(dropdownTriggerLabel('debug-probe-provider')).toBe('OpenAI');
    expect(connectionTrigger.disabled).toBe(false);
    expect(dropdownTriggerLabel('debug-probe-connection')).toBe(
      t('debug.modelProbe.selectConnection'),
    );
    const probeButton = document.querySelector('.debug-view__probe-btn');
    expect(probeButton.disabled).toBe(true);

    chooseDropdownOption('debug-probe-connection', 'Default');
    expect(dropdownTriggerLabel('debug-probe-connection')).toBe('Default');
    expect(probeButton.disabled).toBe(false);

    // A JSON response is pretty-printed next to the model preview.
    probeButton.click();
    await waitForCondition(() =>
      document.querySelector('.debug-view__probe-model-chip'),
    );
    expect(debugModelProbeMock).toHaveBeenCalledWith('openai', 'default');
    expect(probeRawResponseBlock().textContent).toBe(
      JSON.stringify({ data: [{ id: 'gpt-5.2', object: 'model' }] }, null, 2),
    );
    expect(
      document.querySelector('.debug-view__probe-model-chip').textContent,
    ).toBe('GPT-5.2');
    expect(
      document
        .querySelector('.debug-view__probe-model-count')
        .textContent.trim(),
    ).toBe(t('debug.modelProbe.modelCount', { count: 1 }));

    // Any other response stays exactly as received.
    probeButton.click();
    await waitForCondition(
      () => probeRawResponseBlock()?.textContent === 'plain text response',
    );
    expect(document.querySelector('.debug-view__probe-model-chip')).toBeNull();
  });
});

function probeResult(
  rawResponse,
  modelPreview = { model_count: 0, models: [] },
) {
  return {
    raw_response: rawResponse,
    status_code: 200,
    duration_ms: 120,
    trace_id: 'probe-trace',
    model_preview: modelPreview,
  };
}

function probeRawResponseBlock() {
  return document.querySelector('.debug-view__probe-result-section pre');
}

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

function openDropdown(triggerId) {
  const trigger = document.getElementById(triggerId);
  if (!trigger) {
    throw new Error(`Dropdown trigger not found: ${triggerId}`);
  }
  if (trigger.getAttribute('aria-expanded') !== 'true') {
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
  }
  // The list is portaled to <body>; only the open dropdown renders one.
  return Array.from(
    document.querySelectorAll('.dropdown-primitive__list .dropdown-option'),
  );
}

function dropdownOptionLabels(triggerId) {
  const labels = openDropdown(triggerId).map((option) =>
    option.textContent.trim(),
  );
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  flushSync();
  return labels;
}

function chooseDropdownOption(triggerId, label) {
  const option = openDropdown(triggerId).find(
    (item) => item.textContent.trim() === label,
  );
  if (!option) {
    throw new Error(`Option "${label}" not found in ${triggerId}`);
  }
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function dropdownTriggerLabel(triggerId) {
  return (
    document
      .getElementById(triggerId)
      ?.querySelector('.dropdown-primitive__trigger-label')
      ?.textContent?.trim() ?? ''
  );
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

// Opens the `request` or `response` detail tab and its raw body view.
async function switchToDetailTabWhenReady(tab, attempts = 40) {
  const label = t(`debug.${tab}`);
  for (let i = 0; i < attempts; i += 1) {
    flushSync();
    const tab = Array.from(
      document.querySelectorAll('.debug-view__detail-tab-list .tab-list__tab'),
    ).find((button) => button.textContent?.trim() === label);
    if (tab) {
      tab.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      flushSync();
      const rawTab = document.querySelector(
        '.debug-view__body-tab-list [id$="-tab-raw"]',
      );
      rawTab?.click();
      flushSync();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`Detail tab not found: ${label}`);
}

function storageButton(label) {
  return [...document.querySelectorAll('.storage-actions button')].find(
    (button) => button.textContent.trim() === label,
  );
}

function clearAllTraces() {
  storageButton(t('debug.clearAll')).click();
  flushSync();
  storageButton(t('common.confirm')).click();
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

// Hovers `element` and returns the shared quick tooltip's text once it shows.
async function hoveredTooltipText(element) {
  element.dispatchEvent(new Event('pointerenter'));
  await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
  flushSync();
  const text = document.getElementById('app-tooltip')?.textContent ?? null;
  element.dispatchEvent(new Event('pointerleave'));
  return text;
}
