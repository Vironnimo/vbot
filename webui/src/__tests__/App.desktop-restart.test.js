// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { AUTOSAVE_STILL_SAVING_MS } from '../lib/autosave.js';
import { t } from '../lib/i18n.js';
import { flushComposerMemory } from '../lib/composerMemory.js';
import {
  App,
  buttonWithText,
  cleanupAppHarness,
  createAppRpcMock,
  createSettingsRpcMock,
  getServedWebuiBuildMock,
  resetAppHarness,
  rpcMock,
  settingsPanelButton,
  sidebarNavButton,
  subscribeServerEventsMock,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/composerMemory.js', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, flushComposerMemory: vi.fn(actual.flushComposerMemory) };
});

const PENDING = { pending: true, restarting: false, failed: false };
const CONFIRM_DIALOG =
  '[role="dialog"][aria-labelledby="confirm-dialog-title"]';

const restartBanner = () => document.querySelector('.app-desktop-restart');
const restartButton = () => restartBanner()?.querySelector('button') ?? null;
const reloadBanner = () => document.querySelector('.app-webui-outdated');
const errorToasts = () =>
  [...document.querySelectorAll('.toast.error .toast-msg')].map(
    (message) => message.textContent,
  );
const depthInput = () =>
  document.querySelector(
    `input[aria-label="${t('settings.subagents.maxDepth')}"]`,
  );
const cronPrompt = () => document.getElementById('cron-job-prompt');
const invocation = (mock, index = 0) => mock.mock.invocationCallOrder[index];
const rpcInvocation = (method) =>
  rpcMock.mock.invocationCallOrder[
    rpcMock.mock.calls.findIndex(([name]) => name === method)
  ];

function typeInto(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function pushUpdate(detail) {
  window.dispatchEvent(new CustomEvent('vbot-desktop-update', { detail }));
  flushSync();
}

// True when the page took the Desktop's restart request over.
function requestUpdateRestart() {
  const handled = !window.dispatchEvent(
    new CustomEvent('vbot-desktop-restart', {
      cancelable: true,
      detail: { reason: 'update' },
    }),
  );
  flushSync();
  return handled;
}

async function settle() {
  for (let index = 0; index < 10; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

describe('App Desktop restart', () => {
  let mountedComponent;
  let api;

  function installDesktop({
    capabilities = { restart: true },
    update = PENDING,
  } = {}) {
    window.history.replaceState({}, '', '/?accessor=desktop');
    api = {
      getDesktopCapabilities: vi.fn().mockResolvedValue(capabilities),
      getDesktopUpdate: vi.fn().mockResolvedValue(update),
      restartDesktop: vi.fn().mockResolvedValue({ accepted: true }),
    };
    window.pywebview = { api };
    return api;
  }

  async function mountApp() {
    mountedComponent = mount(App, { target: document.body });
    flushSync();
    await settle();
  }

  async function mountWithRestartOffered() {
    await mountApp();
    await waitForCondition(() => expect(restartButton()).toBeTruthy());
  }

  // Settings whose sub-agent depth edit is saved by `settings.update`, which
  // answers through `answer` (the settled value or a rejection).
  // `beforeEdit` runs once the editor shows, before the edit.
  async function editSettings(answer = (save) => save(), beforeEdit = null) {
    const settingsRpc = createSettingsRpcMock();
    rpcMock.mockImplementation((method, params) =>
      method === 'settings.update'
        ? answer(() => settingsRpc(method, params))
        : settingsRpc(method, params),
    );
    await mountWithRestartOffered();
    sidebarNavButton('settings').click();
    await waitForCondition(() =>
      expect(settingsPanelButton('tools')).toBeTruthy(),
    );
    settingsPanelButton('tools').click();
    await waitForCondition(() => expect(depthInput()).toBeTruthy());
    beforeEdit?.();
    typeInto(depthInput(), '5');
  }

  // An unsaved new schedule, kept by App while Cron shows or not.
  async function createCronDraft() {
    rpcMock.mockImplementation(
      createAppRpcMock({
        agents: [{ id: 'alpha', name: 'Alpha' }],
        methods: {
          'cron.list': () => ({ jobs: [] }),
          'project.list': () => ({ projects: [] }),
        },
      }),
    );
    await mountWithRestartOffered();
    sidebarNavButton('cron').click();
    const create = () =>
      document.querySelector(
        `button[aria-label="${t('cron.detail.createTitle')}"]`,
      );
    await waitForCondition(() => expect(create()?.disabled).toBe(false));
    create().click();
    await waitForCondition(() => expect(cronPrompt()).toBeTruthy());
    typeInto(cronPrompt(), 'Unfinished prompt');
  }

  // A Live voice call that keeps connecting.
  async function startLiveCall() {
    localStorage.setItem('vbot.onboardingDismissed', '1');
    rpcMock.mockImplementation(
      createAppRpcMock({
        methods: {
          'settings.get': () => ({
            appearance: { language: 'en', available_languages: ['en'] },
            model_tasks: { live_voice: { target: 'openai/realtime' } },
          }),
          'live.status': () => new Promise(() => {}),
        },
      }),
    );
    await mountWithRestartOffered();
    const start = () =>
      document.querySelector(`button[aria-label="${t('live.startButton')}"]`);
    await waitForCondition(() => expect(start()).toBeTruthy());
    start().click();
    await waitForCondition(() =>
      expect(
        document.querySelector(`button[aria-label="${t('live.stopButton')}"]`),
      ).toBeTruthy(),
    );
  }

  beforeEach(() => {
    resetAppHarness();
    vi.mocked(flushComposerMemory).mockClear();
    mountedComponent = null;
  });

  afterEach(async () => {
    vi.useRealTimers();
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  it.each([
    ['a Desktop that can restart', { restart: true }, true],
    ['an older Desktop', {}, false],
    ['a browser', null, false],
  ])(
    'with %s replaces the reload banner by a restart: %s',
    async (_accessor, capabilities, offered) => {
      if (capabilities) installDesktop({ capabilities });
      const meta = document.createElement('meta');
      meta.name = 'vbot-webui-build';
      meta.content = 'build-loaded';
      document.head.append(meta);
      try {
        getServedWebuiBuildMock.mockResolvedValue('build-served');
        await mountApp();
        subscribeServerEventsMock.mock.calls[0][0].onEvent({
          type: 'connection_ready',
          active_runs: [],
        });
        pushUpdate(PENDING);

        await waitForCondition(() => {
          expect(Boolean(restartBanner())).toBe(offered);
          expect(Boolean(reloadBanner())).toBe(!offered);
        });
        if (offered) {
          expect(restartButton().textContent.trim()).toBe(
            t('app.desktopRestart.restart'),
          );
        } else if (capabilities) {
          expect(api.getDesktopUpdate).not.toHaveBeenCalled();
        } else {
          // A browser leaves the Desktop's restart request unanswered.
          expect(requestUpdateRestart()).toBe(false);
        }
      } finally {
        meta.remove();
      }
    },
  );

  it('follows the restart through progress, failure and a retry', async () => {
    installDesktop();
    rpcMock.mockImplementation(createAppRpcMock());
    await mountWithRestartOffered();
    expect(restartBanner().textContent).toContain(
      t('app.desktopRestart.pending'),
    );

    restartButton().click();
    await waitForCondition(() => {
      expect(api.restartDesktop).toHaveBeenCalledOnce();
      expect(restartButton().disabled).toBe(true);
      expect(restartButton().textContent.trim()).toBe(
        t('app.desktopRestart.restarting'),
      );
    });

    pushUpdate({ pending: true, restarting: false, failed: true });
    expect(restartBanner().textContent).toContain(
      t('app.desktopRestart.failed'),
    );
    expect(restartButton().disabled).toBe(false);
    expect(restartButton().textContent.trim()).toBe(t('common.retry'));
    restartButton().click();
    await waitForCondition(() =>
      expect(api.restartDesktop).toHaveBeenCalledTimes(2),
    );

    pushUpdate({ pending: false, restarting: false, failed: false });
    expect(restartBanner()).toBeNull();
  });

  it('saves pending edits and composer drafts before the user’s restart', async () => {
    installDesktop();
    let finishSave;
    await editSettings(
      (save) =>
        new Promise((resolve) => {
          finishSave = () => resolve(save());
        }),
    );

    restartButton().click();
    await waitForCondition(() => expect(finishSave).toBeTypeOf('function'));
    await settle();
    expect(api.restartDesktop).not.toHaveBeenCalled();

    finishSave();
    await waitForCondition(() =>
      expect(api.restartDesktop).toHaveBeenCalledOnce(),
    );
    expect(rpcInvocation('settings.update')).toBeLessThan(
      invocation(flushComposerMemory),
    );
    expect(invocation(flushComposerMemory)).toBeLessThan(
      invocation(api.restartDesktop),
    );
  });

  it('asks before the user’s restart discards an unsaved new schedule', async () => {
    installDesktop();
    await createCronDraft();

    restartButton().click();
    await waitForCondition(() =>
      expect(document.querySelector(CONFIRM_DIALOG)).toBeTruthy(),
    );
    buttonWithText(`${CONFIRM_DIALOG} button`, t('common.cancel')).click();
    flushSync();
    expect(document.querySelector(CONFIRM_DIALOG)).toBeNull();
    expect(cronPrompt().value).toBe('Unfinished prompt');
    expect(api.restartDesktop).not.toHaveBeenCalled();

    restartButton().click();
    await waitForCondition(() =>
      expect(document.querySelector(CONFIRM_DIALOG)).toBeTruthy(),
    );
    buttonWithText(
      `${CONFIRM_DIALOG} button`,
      t('app.desktopRestart.cronDraftConfirm'),
    ).click();
    await waitForCondition(() => {
      expect(api.restartDesktop).toHaveBeenCalledOnce();
      expect(restartButton().disabled).toBe(true);
    });
    // The handoff does not ask about the schedule a second time.
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
  });

  it.each([
    ['edits that save', () => editSettings(), true],
    [
      'edits that fail to save',
      () => editSettings(() => Promise.reject(new Error('save unavailable'))),
      true,
    ],
    ['a running Live voice call', startLiveCall, false],
    ['an unsaved new schedule', createCronDraft, false],
  ])(
    'takes the Desktop’s restart request over with %s and restarts without asking',
    async (_situation, arrange, edits) => {
      installDesktop();
      await arrange();

      expect(requestUpdateRestart()).toBe(true);
      await waitForCondition(() =>
        expect(api.restartDesktop).toHaveBeenCalledOnce(),
      );
      if (edits) {
        expect(rpcInvocation('settings.update')).toBeLessThan(
          invocation(api.restartDesktop),
        );
      }
      expect(invocation(flushComposerMemory)).toBeLessThan(
        invocation(api.restartDesktop),
      );
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    },
  );

  it.each([
    ['within', AUTOSAVE_STILL_SAVING_MS - 1, 0],
    ['after', AUTOSAVE_STILL_SAVING_MS, 1],
  ])(
    'restarts on the Desktop’s request once pending edits save, waiting no longer than a navigation: edits saved %s that time',
    async (_when, saveMs, restartedBeforeSave) => {
      installDesktop();
      let finishSave;
      await editSettings(
        (save) =>
          new Promise((resolve) => {
            finishSave = () => resolve(save());
          }),
        () => vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }),
      );

      expect(requestUpdateRestart()).toBe(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(finishSave).toBeTypeOf('function');
      await vi.advanceTimersByTimeAsync(saveMs);
      expect(api.restartDesktop).toHaveBeenCalledTimes(restartedBeforeSave);
      finishSave();
      await vi.advanceTimersByTimeAsync(0);
      flushSync();

      expect(api.restartDesktop).toHaveBeenCalledOnce();
      // The save the restart stopped waiting for still lands.
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(
        rpcMock.mock.calls.filter(([method]) => method === 'settings.update'),
      ).toHaveLength(1);
    },
  );

  it.each([
    ['the user’s restart', () => restartButton().click(), true],
    ['the Desktop’s restart request', requestUpdateRestart, false],
  ])(
    'reports a refused restart only for %s',
    async (_request, request, reported) => {
      installDesktop();
      api.restartDesktop.mockRejectedValue(new Error('restart_unavailable'));
      rpcMock.mockImplementation(createAppRpcMock());
      await mountWithRestartOffered();

      request();
      await waitForCondition(() =>
        expect(api.restartDesktop).toHaveBeenCalledOnce(),
      );
      await settle();
      expect(errorToasts()).toEqual(
        reported ? [t('app.desktopRestart.error.unavailable')] : [],
      );
      expect(restartButton().disabled).toBe(false);
    },
  );
});
