// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { createAutosaveParticipant } from '../autosave.js';
import { createExtensionPageClient } from '../extensionPageClient.js';
import { t } from '../i18n.js';
import { listingErrorReason } from '../pathPicker.js';

const descriptor = { owner: 'alpha', page: 'main' };
let client = null;

function parent() {
  return { postMessage: vi.fn() };
}

function dispatchFrom(source, data) {
  const event = new MessageEvent('message', { data });
  Object.defineProperty(event, 'source', { value: source });
  window.dispatchEvent(event);
}

function initialize(target, nonce = 'nonce-a', epoch = 'epoch-a', fields = {}) {
  dispatchFrom(target, {
    type: 'vbot.extension.init',
    version: 1,
    nonce,
    epoch,
    descriptor,
    route: '/initial',
    theme: { mode: 'dark' },
    locale: 'de',
    timezone: 'Europe/Berlin',
    ...fields,
  });
}

afterEach(() => {
  client?.dispose();
  client = null;
  vi.useRealTimers();
});

describe('extension page client', () => {
  it('exposes catalog services, local commands, external links and searchable page entries through the bound bridge', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target, 'nonce-a', 'epoch-a', {
      api: { version: 30, features: ['ui.contributions'] },
      ui: { commands: ['composer.insert'], context: { session_id: 'one' } },
    });
    expect(client.context.api).toEqual({
      version: 30,
      features: ['ui.contributions'],
    });
    const entries = [{ id: 'one', label: 'One', terms: 'connection' }];
    for (const [invoke, method, params] of [
      [() => client.services.describe(), 'service.describe', {}],
      [() => client.openAppChat(), 'chat.open_app', {}],
      [
        () => client.services.query('agent.list'),
        'service.query',
        { name: 'agent.list', params: {} },
      ],
      [
        () => client.services.execute('agent.update', { id: 'a' }),
        'service.execute',
        { name: 'agent.update', params: { id: 'a' } },
      ],
      [
        () => client.command('composer.insert', { text: 'hello' }),
        'ui.command',
        { name: 'composer.insert', params: { text: 'hello' } },
      ],
      [
        () => client.openExternalUrl('https://example.test'),
        'external.open',
        { url: 'https://example.test' },
      ],
      [() => client.setSearchIndex(entries), 'search.index', { entries }],
    ]) {
      const result = invoke();
      const request = target.postMessage.mock.calls.at(-1)[0];
      expect(request).toMatchObject({
        method,
        params,
        epoch: 'epoch-a',
        nonce: 'nonce-a',
      });
      dispatchFrom(target, {
        ...request,
        type: 'vbot.extension.result',
        result: { ok: true },
      });
      await expect(result).resolves.toEqual({ ok: true });
    }
    const reveal = vi.fn();
    const remove = client.onSearch(reveal);
    dispatchFrom(target, {
      type: 'vbot.extension.search.reveal',
      version: 1,
      descriptor,
      epoch: 'epoch-a',
      nonce: 'wrong',
      id: 'one',
    });
    expect(reveal).not.toHaveBeenCalled();
    dispatchFrom(target, {
      type: 'vbot.extension.search.reveal',
      version: 1,
      descriptor,
      epoch: 'epoch-a',
      nonce: 'nonce-a',
      id: 'one',
    });
    expect(reveal).toHaveBeenCalledExactlyOnceWith('one');
    remove();
  });

  it('asks the shell to open one Session by canonical address', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const address = {
      project_id: 'project',
      agent_id: 'tmp_worker',
      session_id: 'ses_one',
    };
    const opened = client.openSession(address);
    const request = target.postMessage.mock.calls.at(-1)[0];
    expect(request).toMatchObject({
      method: 'session.open',
      params: { address },
    });
    dispatchFrom(target, {
      ...request,
      type: 'vbot.extension.result',
      result: {},
    });
    await expect(opened).resolves.toEqual({});
  });

  it('attaches a participant slot after handshake and releases it from the current registration', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    const errors = vi.fn();
    const scroller = document.createElement('div');
    const slot = document.createElement('div');
    scroller.style.overflowY = 'auto';
    scroller.append(slot);
    Object.defineProperty(scroller, 'clientHeight', { value: 100 });
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue({ top: 30 });
    vi.spyOn(slot, 'getBoundingClientRect').mockReturnValue({
      x: 10,
      left: 10,
      right: 210,
      width: 200,
      y: -10,
      top: -10,
      bottom: 200,
      height: 210,
    });
    const viewport = document.documentElement;
    const width = vi.spyOn(viewport, 'clientWidth', 'get').mockReturnValue(400);
    const height = vi
      .spyOn(viewport, 'clientHeight', 'get')
      .mockReturnValue(400);
    const opened = vi.fn();
    const rules = [
      { prefix: '#', fragment: '#post/', label: 'Board', first: 0, last: 2 },
    ];
    const release = client.mountSession(
      slot,
      'group',
      'worker',
      errors,
      'compact',
      opened,
    );
    release.setReferences(rules);
    expect(target.postMessage).not.toHaveBeenCalled();
    initialize(target);
    const header = document.createElement('header');
    header.id = 'participant-toolbar';
    slot.dataset.sessionClipBelow = header.id;
    scroller.prepend(header);
    document.body.append(scroller);
    vi.spyOn(header, 'getBoundingClientRect').mockReturnValue({
      left: 10,
      right: 210,
      top: 30,
      bottom: 75,
    });
    initialize(target, 'next');
    const registrations = target.postMessage.mock.calls
      .map(([data]) => data)
      .filter((request) => request.method === 'session.embed');
    const notifyReference = (registration, slot) =>
      dispatchFrom(target, {
        ...registration,
        type: 'vbot.extension.session.reference',
        slot,
        href: '#post/0',
      });
    notifyReference(registrations[0], registrations[0].params.slot);
    notifyReference(registrations[1], registrations[0].params.slot);
    expect(opened).not.toHaveBeenCalled();
    notifyReference(registrations[1], registrations[1].params.slot);
    expect(opened).toHaveBeenCalledExactlyOnceWith('#post/0');
    release.setReferences([]);
    release();
    notifyReference(registrations[1], registrations[1].params.slot);
    expect(opened).toHaveBeenCalledTimes(1);
    const sent = target.postMessage.mock.calls.map(([data]) => data);
    const mounts = sent.filter((m) => m.method === 'session.embed');
    expect(mounts.map((m) => m.nonce)).toEqual(['nonce-a', 'next']);
    expect(
      sent.findLast((m) => m.type === 'vbot.extension.session.references'),
    ).toMatchObject({
      nonce: 'next',
      slot: mounts[1].params.slot,
      references: [],
    });
    expect(
      sent
        .filter((m) => m.type === 'vbot.extension.session.references')
        .map((m) => m.references),
    ).toEqual([rules, rules, []]);
    expect(mounts[1].params).toMatchObject({
      group_id: 'group',
      participant_id: 'worker',
      presentation: 'compact',
    });
    expect(
      sent.find((m) => m.type === 'vbot.extension.session.bounds')?.bounds,
    ).toEqual({
      x: 10,
      y: -10,
      width: 200,
      height: 210,
      clip: [40, 0, 70, 0],
    });
    expect(
      sent.findLast((m) => m.type === 'vbot.extension.session.bounds')?.bounds
        .clip,
    ).toEqual([85, 0, 70, 0]);
    scroller.remove();
    expect(sent.at(-1)).toMatchObject({
      type: 'vbot.extension.session.release',
      nonce: 'next',
      slot: mounts[1].params.slot,
    });
    expect(errors).not.toHaveBeenCalled();
    width.mockRestore();
    height.mockRestore();
  });

  it('flushes a registered editor and rejects obsolete completion after reinitialization', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    let complete;
    const participant = {
      hasPending: () => true,
      flush: vi.fn(() => new Promise((resolve) => (complete = resolve))),
    };
    client.registerAutosave(participant);
    expect(target.postMessage.mock.calls.at(-1)[0]).toMatchObject({
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
    const request = {
      type: 'vbot.extension.autosave.flush',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
      id: 'flush-a',
    };
    dispatchFrom(target, request);
    await Promise.resolve();
    initialize(target, 'nonce-b', 'epoch-b');
    complete(true);
    await Promise.resolve();
    await Promise.resolve();
    expect(
      target.postMessage.mock.calls.some(
        ([data]) => data.type === 'vbot.extension.autosave.result',
      ),
    ).toBe(false);
    participant.flush.mockResolvedValue(false);
    dispatchFrom(target, {
      ...request,
      id: 'flush-b',
      nonce: 'nonce-b',
      epoch: 'epoch-b',
    });
    await vi.waitFor(() =>
      expect(target.postMessage.mock.calls.at(-1)[0]).toMatchObject({
        type: 'vbot.extension.autosave.result',
        id: 'flush-b',
        saved: false,
      }),
    );
  });

  it('releases the running write of the registered editor and reports edits made after it', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    let draft = 'first';
    const participant = createAutosaveParticipant({
      getSnapshot: () => draft,
      hasChanges: () => draft !== '',
      save: () => new Promise(() => {}),
    });
    client.registerAutosave(participant);
    void participant.runSave();
    const lastMessage = () => target.postMessage.mock.calls.at(-1)[0];

    dispatchFrom(target, {
      type: 'vbot.extension.autosave.release',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
    });
    expect(lastMessage()).toMatchObject({
      type: 'vbot.extension.autosave.state',
      pending: false,
    });

    draft = 'second';
    client.notifyAutosave();
    expect(lastMessage()).toMatchObject({
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
  });

  it('accepts catalog replies larger than the command limit', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const pending = client.operation('catalog');
    const call = target.postMessage.mock.calls.at(-1)[0];
    const result = { catalog: { description: 'x'.repeat(128 * 1024) } };
    dispatchFrom(target, { ...call, type: 'vbot.extension.result', result });
    await expect(pending).resolves.toEqual(result);
  });

  it('rejects a missing reply and allows a fresh request without replaying it', async () => {
    vi.useFakeTimers();
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const failed = expect(client.operation('catalog')).rejects.toThrow(
      t('extensions.page.requestTimedOut'),
    );
    await vi.advanceTimersByTimeAsync(30_000);
    await failed;
    const pending = client.operation('catalog');
    const call = target.postMessage.mock.calls.at(-1)[0];
    dispatchFrom(target, {
      ...call,
      type: 'vbot.extension.result',
      result: {},
    });
    await expect(pending).resolves.toEqual({});
    expect(vi.getTimerCount()).toBe(0);
    expect(
      target.postMessage.mock.calls.filter(
        ([data]) => data.type === 'vbot.extension.call',
      ),
    ).toHaveLength(2);
  });

  it('accepts only a versioned matching init and publishes its display context', () => {
    const target = parent();
    const contexts = vi.fn();
    client = createExtensionPageClient({ target });
    client.onContext(contexts);

    dispatchFrom(target, { type: 'vbot.extension.init', version: 2 });
    expect(target.postMessage).not.toHaveBeenCalled();

    initialize(target);

    expect(target.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'vbot.extension.ready',
        version: 1,
        nonce: 'nonce-a',
        epoch: 'epoch-a',
        descriptor,
      }),
      '*',
    );
    expect(client.context).toMatchObject({
      route: '/initial',
      theme: { mode: 'dark' },
      locale: 'de',
      timezone: 'Europe/Berlin',
    });
    expect(contexts).toHaveBeenCalledOnce();
  });

  it.each([
    ['pushRoute', 'route.push'],
    ['replaceRoute', 'route.replace'],
  ])('sends %s to the host as a %s call', (name, method) => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    void client[name]('/swarms/swr-a').catch(() => {});
    expect(target.postMessage.mock.calls.at(-1)[0]).toMatchObject({
      type: 'vbot.extension.call',
      method,
      params: { route: '/swarms/swr-a' },
    });
  });

  it('lists server folders through the host and rejects a failed listing with its reason', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const lastCall = () => target.postMessage.mock.calls.at(-1)[0];
    const reply = (fields) =>
      dispatchFrom(target, { ...lastCall(), ...fields });

    const places = client.listDirectory();
    expect(lastCall()).toMatchObject({
      type: 'vbot.extension.call',
      method: 'directory.list',
      params: { path: null },
    });
    const listing = { path: '', entries: [], truncated: false, home: '/home' };
    reply({ type: 'vbot.extension.result', result: listing });
    await expect(places).resolves.toEqual(listing);

    const failures = [];
    for (const [fields, params] of [
      [
        { code: 'domain_error', reason: 'unreadable' },
        { path: 'C:/locked', include_files: true, prefix: 'Re' },
      ],
      [{ code: 'invalid_request' }, { path: '../out', root: 'C:/work' }],
    ]) {
      const failed = client.listDirectory(params);
      expect(lastCall().params).toEqual(params);
      reply({ type: 'vbot.extension.error', error: 'Not listed', ...fields });
      failures.push(await failed.catch((error) => error));
    }
    // The shared path field reads the failure as it reads the app's.
    expect(failures.map(listingErrorReason)).toEqual(['unreadable', 'invalid']);

    // A failure without a message still says that the request failed.
    const unnamed = client.listDirectory();
    reply({ type: 'vbot.extension.error' });
    await expect(unnamed).rejects.toThrow(t('extensions.page.requestFailed'));
  });

  it("asks the host for a Run's step window and for the steps before its gap", async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const lastCall = () => target.postMessage.mock.calls.at(-1)[0];

    const subscribed = client.subscribeRun('group', 'run', 3, {
      timeline: true,
      childLimit: 100,
    });
    expect(lastCall()).toMatchObject({
      method: 'run.subscribe',
      params: {
        group_id: 'group',
        run_id: 'run',
        after_sequence: 3,
        timeline: true,
        child_limit: 100,
      },
    });
    const subscription = { id: 'subscription', replay_through_sequence: 3 };
    dispatchFrom(target, {
      ...lastCall(),
      type: 'vbot.extension.result',
      result: subscription,
    });
    await expect(subscribed).resolves.toEqual(subscription);

    const children = client.readRunChildren('group', 'participant', {
      runId: 'run',
      before: 'step-3',
      historyGeneration: 'generation',
      limit: 100,
    });
    expect(lastCall()).toMatchObject({
      method: 'history.children',
      params: {
        group_id: 'group',
        participant_id: 'participant',
        run_id: 'run',
        before: 'step-3',
        history_generation: 'generation',
        limit: 100,
      },
    });
    const page = { children: [], has_more: false };
    dispatchFrom(target, {
      ...lastCall(),
      type: 'vbot.extension.result',
      result: page,
    });
    await expect(children).resolves.toEqual(page);

    const raw = client.readToolCall('group', 'participant', {
      runId: 'run',
      toolId: 'tool',
      historyGeneration: 'generation',
    });
    expect(lastCall()).toMatchObject({
      method: 'history.tool_call',
      params: {
        group_id: 'group',
        participant_id: 'participant',
        run_id: 'run',
        tool_id: 'tool',
        history_generation: 'generation',
      },
    });
    const call = { arguments: {}, result: null };
    dispatchFrom(target, {
      ...lastCall(),
      type: 'vbot.extension.result',
      result: call,
    });
    await expect(raw).resolves.toEqual(call);
  });

  it('reports whether layers are open and closes the topmost one when the host asks', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    const states = () =>
      target.postMessage.mock.calls
        .map(([data]) => data)
        .filter((data) => data.type === 'vbot.extension.layers.state')
        .map((data) => data.open);
    const dialog = { close: vi.fn() };
    const menu = { close: vi.fn() };
    const releaseDialog = client.registerLayer(dialog);
    // A layer opened before the host is known is reported with Ready.
    initialize(target);
    const releaseMenu = client.registerLayer(menu);
    dispatchFrom(target, {
      type: 'vbot.extension.layers.close',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
    });
    expect(menu.close).toHaveBeenCalledOnce();
    expect(dialog.close).not.toHaveBeenCalled();
    releaseMenu();
    releaseDialog();
    expect(states()).toEqual([true, false]);
  });

  it('forwards Back/Forward keys and side buttons to the host only when it asks', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    const moves = () =>
      target.postMessage.mock.calls
        .map(([data]) => data)
        .filter((data) => data.type === 'vbot.extension.history.move')
        .map((data) => [data.nonce, data.direction]);
    // Each returns whether the page's native handling was cancelled.
    const press = (key, modifiers = {}) =>
      !window.dispatchEvent(
        new KeyboardEvent('keydown', { key, cancelable: true, ...modifiers }),
      );
    const click = (type, button) =>
      !window.dispatchEvent(new MouseEvent(type, { button, cancelable: true }));
    initialize(target);
    expect([press('ArrowLeft', { altKey: true }), click('mouseup', 3)]).toEqual(
      [false, false],
    );
    initialize(target, 'nonce-b', 'epoch-b', { forwardHistoryInput: true });
    expect([
      press('ArrowLeft', { altKey: true }),
      press('ArrowRight', { altKey: true, shiftKey: true }),
      press('BrowserForward'),
      click('mousedown', 3),
      click('mouseup', 3),
      click('mouseup', 4),
    ]).toEqual([true, false, true, true, true, true]);
    // A key the page handled itself stays with the page.
    const handled = new KeyboardEvent('keydown', {
      key: 'ArrowLeft',
      altKey: true,
      cancelable: true,
    });
    handled.preventDefault();
    window.dispatchEvent(handled);
    expect(moves()).toEqual([
      ['nonce-b', 'back'],
      ['nonce-b', 'forward'],
      ['nonce-b', 'back'],
      ['nonce-b', 'forward'],
    ]);
    client.dispose();
    expect(press('ArrowLeft', { altKey: true })).toBe(false);
  });

  it('rejects pending calls on reload and ignores stale results', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const pending = client.operation('read', {});
    const oldCall = target.postMessage.mock.calls.at(-1)[0];

    initialize(target, 'nonce-b', 'epoch-b');
    await expect(pending).rejects.toThrow(t('extensions.page.reloaded'));

    dispatchFrom(target, {
      ...oldCall,
      type: 'vbot.extension.result',
      result: { stale: true },
    });
    const current = client.operation('read', {});
    const currentCall = target.postMessage.mock.calls.at(-1)[0];
    dispatchFrom(target, {
      type: 'vbot.extension.result',
      version: 1,
      nonce: currentCall.nonce,
      epoch: currentCall.epoch,
      descriptor,
      id: currentCall.id,
      result: { current: true },
    });

    await expect(current).resolves.toEqual({ current: true });
  });

  it('passes the reason and the changed records of each invalidation to listeners', () => {
    const target = parent();
    const invalidations = vi.fn();
    client = createExtensionPageClient({ target });
    client.onInvalidation(invalidations);
    initialize(target);
    const invalidate = (fields) =>
      dispatchFrom(target, {
        type: 'vbot.extension.invalidate',
        version: 1,
        nonce: 'nonce-a',
        epoch: 'epoch-a',
        descriptor,
        ...fields,
      });
    const change = { resource: 'swarms', ids: ['swarm-one'], revision: 3 };
    invalidate({ revision: 4, change });
    invalidate({ reason: 'run_stream_recovered' });
    // A malformed change is dropped, so the page refreshes everything.
    invalidate({ change: { ...change, ids: 'swarm-one' } });
    expect(invalidations.mock.calls.map(([value]) => value)).toEqual([
      { reason: null, change },
      { reason: 'run_stream_recovered', change: null },
      { reason: null, change: null },
    ]);
  });

  it('cleans up pending work and listeners when the page is disposed', async () => {
    const target = parent();
    const invalidations = vi.fn();
    client = createExtensionPageClient({ target });
    client.onInvalidation(invalidations);
    initialize(target);
    const pending = client.replaceRoute('/next');

    client.dispose();
    await expect(pending).rejects.toThrow(t('extensions.page.disposed'));
    dispatchFrom(target, {
      type: 'vbot.extension.invalidate',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
    });
    expect(invalidations).not.toHaveBeenCalled();
  });

  it('does not accept messages from a different window or oversized calls', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(parent());
    expect(client.context).toBeNull();
    await expect(client.operation('read')).rejects.toThrow(
      t('extensions.page.hostUnavailable'),
    );
    initialize(target);
    await expect(
      client.operation('read', { text: 'x'.repeat(64 * 1024) }),
    ).rejects.toThrow(t('extensions.page.requestTooLarge'));
  });
});
