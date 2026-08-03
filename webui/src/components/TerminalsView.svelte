<script>
  import { onMount } from 'svelte';
  import '@xterm/xterm/css/xterm.css';

  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextArea from './ui/TextArea.svelte';
  import Toggle from './ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
  import {
    TERMINAL_STREAM_CONNECTED,
    TERMINAL_STREAM_CONNECTING,
    TERMINAL_STREAM_ERROR,
    TERMINAL_STREAM_RECONNECTING,
    createTerminalsController,
    createTerminalsViewState,
    selectedTerminal,
  } from '$lib/terminalsView.js';
  import { tooltip } from '$lib/tooltip.js';

  let {
    terminalsRefreshToken = 0,
    serverUnavailable = false,
    onToast = () => {},
  } = $props();

  let viewState = $state(createTerminalsViewState());
  let terminalHost = $state(null);
  let controlEnabled = $state(false);
  let controlledTerminalId = $state('');
  let contextText = $state('');
  let stopDialogOpen = $state(false);
  let mounted = false;
  let rendererPromise = null;
  let pendingSnapshot = '';
  let pendingOutput = [];
  let xterm = null;
  let fitAddon = null;
  let resizeObserver = null;
  let inputDisposable = null;

  let terminal = $derived(selectedTerminal(viewState));
  let streamStatusLabel = $derived(streamLabel(viewState.streamStatus));
  let streamStatusVariant = $derived(streamVariant(viewState.streamStatus));

  const controller = createTerminalsController({
    state: viewState,
    onSnapshot: (ansi) => {
      pendingSnapshot = ansi;
      pendingOutput = [];
      if (!xterm) {
        return;
      }
      xterm.reset();
      xterm.write(ansi, scheduleFit);
    },
    onOutput: (data) => {
      if (xterm) {
        xterm.write(data);
      } else {
        pendingOutput = [...pendingOutput, data].slice(-256);
      }
    },
    onClear: () => {
      pendingSnapshot = '';
      pendingOutput = [];
      xterm?.reset();
    },
  });

  $effect(() => {
    void terminalsRefreshToken;
    if (mounted && !serverUnavailable) {
      void controller.loadTerminals({ silent: true });
    }
  });

  $effect(() => {
    controller.setServerUnavailable(serverUnavailable);
  });

  $effect(() => {
    const selectedId = viewState.selectedTerminalId;
    if (selectedId !== controlledTerminalId) {
      controlledTerminalId = selectedId;
      setControlEnabled(false);
      contextText = '';
      stopDialogOpen = false;
    }
  });

  onMount(() => {
    mounted = true;
    if (!serverUnavailable) {
      void controller.start();
    }

    return () => {
      mounted = false;
      controller.destroy();
      disposeRenderer();
      pendingSnapshot = '';
      pendingOutput = [];
    };
  });

  function mountTerminal(node) {
    terminalHost = node;
    ensureRenderer();
    return {
      destroy() {
        if (terminalHost === node) {
          terminalHost = null;
          disposeRenderer();
        }
      },
    };
  }

  function ensureRenderer() {
    if (!mounted || !terminalHost || xterm || rendererPromise) {
      return;
    }
    const loading = initializeTerminal()
      .catch(() => {
        if (mounted) {
          viewState.streamError = t(
            'terminals.rendererError',
            'The browser terminal renderer could not be loaded.',
          );
          viewState.streamStatus = TERMINAL_STREAM_ERROR;
        }
      })
      .finally(() => {
        if (rendererPromise === loading && !xterm) {
          rendererPromise = null;
        }
      });
    rendererPromise = loading;
  }

  function disposeRenderer() {
    resizeObserver?.disconnect();
    inputDisposable?.dispose();
    xterm?.dispose();
    resizeObserver = null;
    inputDisposable = null;
    fitAddon = null;
    xterm = null;
    rendererPromise = null;
  }

  async function initializeTerminal() {
    const [{ Terminal }, { FitAddon }] = await Promise.all([
      import('@xterm/xterm'),
      import('@xterm/addon-fit'),
    ]);
    if (!mounted || !terminalHost) {
      return;
    }
    xterm = new Terminal({
      allowTransparency: false,
      convertEol: false,
      cursorBlink: false,
      cursorInactiveStyle: 'outline',
      disableStdin: true,
      fontFamily: cssToken('--font-mono', 'IBM Plex Mono, monospace'),
      fontSize: 12,
      lineHeight: 1.12,
      minimumContrastRatio: 4.5,
      rightClickSelectsWord: true,
      scrollback: 2_000,
      scrollOnUserInput: true,
      smoothScrollDuration: 100,
      theme: terminalTheme(),
    });
    fitAddon = new FitAddon();
    xterm.loadAddon(fitAddon);
    xterm.open(terminalHost);
    inputDisposable = xterm.onData((data) => {
      if (controlEnabled) {
        controller.queueInput(data);
      }
    });
    if (typeof globalThis.ResizeObserver === 'function') {
      resizeObserver = new ResizeObserver(scheduleFit);
      resizeObserver.observe(terminalHost);
    }
    if (pendingSnapshot) {
      xterm.reset();
      xterm.write(pendingSnapshot);
    }
    for (const data of pendingOutput) {
      xterm.write(data);
    }
    pendingOutput = [];
    scheduleFit();
  }

  function scheduleFit() {
    queueMicrotask(fitTerminal);
  }

  function fitTerminal() {
    if (
      !xterm ||
      !fitAddon ||
      !terminalHost ||
      terminalHost.clientWidth <= 0 ||
      terminalHost.clientHeight <= 0
    ) {
      return;
    }
    try {
      fitAddon.fit();
      controller.resize(xterm.cols, xterm.rows);
    } catch {
      // The host may be between layout states while the view is mounting.
    }
  }

  function setControlEnabled(enabled) {
    controlEnabled = enabled === true && Boolean(viewState.selectedTerminalId);
    if (!xterm) {
      return;
    }
    xterm.options.disableStdin = !controlEnabled;
    xterm.options.cursorBlink = controlEnabled;
    if (controlEnabled) {
      xterm.focus();
    }
  }

  function sendContext() {
    if (!contextText.trim() || !terminal) {
      return;
    }
    controller.queueInput(`${contextText}\r`, { immediate: true });
    contextText = '';
  }

  function handleContextKeydown(event) {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      sendContext();
    }
  }

  async function confirmStop() {
    const stopped = await controller.killSelected();
    stopDialogOpen = false;
    if (stopped) {
      onToast({
        title: t('terminals.stoppedTitle', 'Terminal stopped'),
        message: t(
          'terminals.stoppedMessage',
          'The selected Terminal Session and its process tree were stopped.',
        ),
        variant: 'success',
      });
    }
  }

  function terminalTarget(item) {
    const agentId = item?.owner?.agent_id || '—';
    const projectId = item?.owner?.project_id;
    return projectId ? `${agentId}@${projectId}` : agentId;
  }

  function terminalSession(item) {
    return item?.owner?.session_id || '—';
  }

  function shortSession(item) {
    const sessionId = terminalSession(item);
    return sessionId.length > 14 ? `${sessionId.slice(0, 12)}…` : sessionId;
  }

  function stateLabel(state) {
    return t(`terminals.state.${state}`, state || 'Unknown');
  }

  function stateVariant(state) {
    if (state === 'working' || state === 'ready') {
      return 'success';
    }
    if (state === 'starting' || state === 'needs_input') {
      return 'warn';
    }
    if (state === 'turn_complete') {
      return 'info';
    }
    if (state === 'error') {
      return 'error';
    }
    return 'neutral';
  }

  function streamLabel(status) {
    if (status === TERMINAL_STREAM_CONNECTED) {
      return t('terminals.stream.live', 'Live');
    }
    if (status === TERMINAL_STREAM_CONNECTING) {
      return t('terminals.stream.connecting', 'Connecting…');
    }
    if (status === TERMINAL_STREAM_RECONNECTING) {
      return t('terminals.stream.reconnecting', 'Reconnecting…');
    }
    if (status === TERMINAL_STREAM_ERROR) {
      return t('terminals.stream.error', 'Stream error');
    }
    return t('terminals.stream.idle', 'Idle');
  }

  function streamVariant(status) {
    if (status === TERMINAL_STREAM_CONNECTED) {
      return 'success';
    }
    if (status === TERMINAL_STREAM_RECONNECTING) {
      return 'warn';
    }
    if (status === TERMINAL_STREAM_ERROR) {
      return 'error';
    }
    return 'neutral';
  }

  function startedAt(item) {
    const date = new Date(item?.started_at || '');
    if (Number.isNaN(date.getTime())) {
      return '—';
    }
    return new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(date);
  }

  function cssToken(name, fallback) {
    if (typeof document === 'undefined') {
      return fallback;
    }
    return (
      getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim() || fallback
    );
  }

  function terminalTheme() {
    return {
      background: cssToken('--bg', '#221A12'),
      foreground: cssToken('--text-hi', '#EEE7DC'),
      cursor: cssToken('--accent', '#E8870A'),
      cursorAccent: cssToken('--bg', '#221A12'),
      selectionBackground: cssToken('--border-2', '#5D4A35'),
      black: '#221A12',
      red: cssToken('--red', '#FC8181'),
      green: cssToken('--green', '#4ADE80'),
      yellow: cssToken('--amber', '#F59E0B'),
      blue: cssToken('--blue', '#60A5FA'),
      magenta: '#D8A4E2',
      cyan: '#67D4C1',
      white: cssToken('--text-hi', '#EEE7DC'),
      brightBlack: cssToken('--text-lo', '#5E4C38'),
      brightRed: '#FFA0A0',
      brightGreen: '#7AE7A4',
      brightYellow: '#FBC45B',
      brightBlue: '#8EC0FF',
      brightMagenta: '#E7BDEE',
      brightCyan: '#8DE1D2',
      brightWhite: '#FFF9F0',
    };
  }

  function terminalError(message) {
    return message || t('terminals.unknownError', 'Unknown terminal error');
  }
</script>

<section class="terminals-view" aria-labelledby="terminals-title">
  <aside class="terminals-view__list-pane secondary-pane">
    <div class="terminals-view__list-heading">
      <span class="secondary-pane__title">
        {t('terminals.activeLabel', 'Active terminals')}
      </span>
      <span
        class="terminals-view__count"
        aria-label={t('terminals.activeCount', '{count} active', {
          count: viewState.terminals.length,
        })}
      >
        {viewState.terminals.length}
      </span>
    </div>

    {#if viewState.loading && viewState.terminals.length === 0}
      <Banner variant="neutral" class="terminals-view__list-feedback">
        {t('terminals.loading', 'Loading active terminals…')}
      </Banner>
    {:else if viewState.listError && !serverUnavailable}
      <Banner variant="error" class="terminals-view__list-feedback">
        <span
          >{t(
            'terminals.listError',
            'Active terminals could not be loaded.',
          )}</span
        >
        <Button variant="secondary" onClick={() => controller.loadTerminals()}>
          {t('common.retry', 'Retry')}
        </Button>
      </Banner>
    {:else if viewState.terminals.length === 0}
      <EmptyState
        density="compact"
        title={t('terminals.emptyTitle', 'No active terminals')}
        description={t(
          'terminals.emptyDescription',
          'Terminal Sessions started by an agent will appear here and keep running across Runs.',
        )}
      />
    {:else}
      <div class="secondary-list terminals-view__list">
        {#each viewState.terminals as item (item.terminal_id)}
          <button
            type="button"
            class="secondary-list__item terminals-view__list-item"
            class:secondary-list__item--active={item.terminal_id ===
              viewState.selectedTerminalId}
            aria-current={item.terminal_id === viewState.selectedTerminalId
              ? 'true'
              : undefined}
            onclick={() => controller.selectTerminal(item.terminal_id)}
          >
            <span class="terminals-view__item-topline">
              <span class="terminals-view__command">{item.command}</span>
              <span
                class={`terminals-view__state-dot terminals-view__state-dot--${item.state}`}
                aria-hidden="true"
              ></span>
            </span>
            <span class="terminals-view__target">{terminalTarget(item)}</span>
            <span class="terminals-view__item-meta">
              <span use:tooltip={terminalSession(item)}
                >{shortSession(item)}</span
              >
              <span>{startedAt(item)}</span>
            </span>
          </button>
        {/each}
      </div>
    {/if}
  </aside>

  <div class="terminals-view__detail">
    <header class="terminals-view__header view-header">
      <div class="view-header__intro">
        <p class="view-header__eyebrow">
          {t('terminals.eyebrow', 'Live PTY / ConPTY')}
        </p>
        <h2 id="terminals-title" class="view-header__title">
          {t('terminals.title', 'Terminals')}
        </h2>
        <p class="view-header__subtitle">
          {t(
            'terminals.subtitle',
            'Watch the same interactive terminal an agent is using, take control when needed, or stop it explicitly.',
          )}
        </p>
      </div>
      {#if terminal}
        <div class="view-header__actions terminals-view__header-status">
          <StatusChip variant={stateVariant(terminal.state)}>
            {stateLabel(terminal.state)}
          </StatusChip>
          <StatusChip variant={streamStatusVariant}>
            {streamStatusLabel}
          </StatusChip>
        </div>
      {/if}
    </header>

    {#if terminal}
      <div class="terminals-view__toolbar view-toolbar view-toolbar--split">
        <div class="terminals-view__identity">
          <span class="terminals-view__identity-primary">
            <span>{terminal.command}</span>
            <span class="terminals-view__target-marker"
              >{terminalTarget(terminal)}</span
            >
          </span>
          <span class="terminals-view__identity-meta">
            <span>PID {terminal.pid}</span>
            <span>{terminal.columns}×{terminal.rows}</span>
            <span use:tooltip={terminal.workdir}>{terminal.workdir}</span>
          </span>
        </div>
        <div class="terminals-view__controls">
          <label class="terminals-view__control-toggle">
            <span>{t('terminals.controlLabel', 'Take control')}</span>
            <Toggle
              size="sm"
              checked={controlEnabled}
              onChange={setControlEnabled}
              ariaLabel={t('terminals.controlLabel', 'Take control')}
            />
          </label>
          <Button
            variant="danger"
            loading={viewState.killing}
            onClick={() => (stopDialogOpen = true)}
          >
            {t('terminals.stop', 'Stop terminal')}
          </Button>
        </div>
      </div>

      {#if terminal.attention}
        <Banner
          variant={terminal.attention.kind === 'error' ? 'error' : 'warn'}
          class="terminals-view__attention"
        >
          <span>
            <strong>{t('terminals.attentionLabel', 'Attention')}</strong>
            {terminal.attention.summary}
          </span>
        </Banner>
      {/if}
      {#if viewState.streamError && !serverUnavailable}
        <Banner variant="warn" class="terminals-view__feedback">
          <span>{terminalError(viewState.streamError)}</span>
        </Banner>
      {/if}
      {#if viewState.streamErrorCode === 'gap' && !serverUnavailable}
        <Banner variant="warn" class="terminals-view__feedback">
          <span>
            {t(
              'terminals.streamGap',
              'Terminal output continuity was lost; rebuilding the live screen.',
            )}
          </span>
        </Banner>
      {/if}
      {#if viewState.actionError && !serverUnavailable}
        <Banner variant="error" class="terminals-view__feedback">
          <span>{terminalError(viewState.actionError)}</span>
        </Banner>
      {/if}

      <div
        class="terminals-view__terminal-shell"
        data-control={controlEnabled ? 'enabled' : 'observe'}
      >
        <div class="terminals-view__terminal-bar">
          <span class="terminals-view__terminal-mode">
            {controlEnabled
              ? t(
                  'terminals.mode.control',
                  'Control enabled — keystrokes go to the process',
                )
              : t(
                  'terminals.mode.observe',
                  'Observe mode — keyboard input is locked',
                )}
          </span>
          <span>{terminal.integration === 'codex' ? 'CODEX' : 'PTY'}</span>
        </div>
        <div
          use:mountTerminal
          class="terminals-view__terminal-host"
          aria-label={t('terminals.liveTerminalLabel', 'Live terminal output')}
        ></div>
      </div>

      <div class="terminals-view__composer">
        <div class="terminals-view__composer-copy">
          <span class="terminals-view__composer-label">
            {t('terminals.sendLabel', 'Send to terminal')}
          </span>
          <span class="terminals-view__composer-hint">
            {t(
              'terminals.sendHint',
              'Ctrl+Enter sends the text followed by Enter.',
            )}
          </span>
        </div>
        <TextArea
          class="terminals-view__context-input"
          value={contextText}
          rows={2}
          placeholder={t(
            'terminals.sendPlaceholder',
            'Add context, answer a question, or give the next instruction…',
          )}
          ariaLabel={t('terminals.sendLabel', 'Send to terminal')}
          onInput={(next) => (contextText = next)}
          onkeydown={handleContextKeydown}
        />
        <Button
          variant="primary"
          disabled={!contextText.trim() || serverUnavailable}
          onClick={sendContext}
        >
          {t('terminals.send', 'Send + Enter')}
        </Button>
      </div>
    {:else}
      <EmptyState
        fill
        title={t('terminals.detailEmptyTitle', 'Nothing to monitor yet')}
        description={t(
          'terminals.detailEmptyDescription',
          'When an agent starts terminal_beta, its live TUI appears here without taking ownership away from the agent.',
        )}
      />
    {/if}
  </div>
</section>

{#if stopDialogOpen && terminal}
  <ConfirmDialog
    title={t('terminals.stopConfirmTitle', 'Stop this Terminal Session?')}
    body={t(
      'terminals.stopConfirmBody',
      'This terminates the selected process tree. The Terminal Session cannot be resumed afterward.',
    )}
    confirmLabel={t('terminals.stop', 'Stop terminal')}
    onConfirm={confirmStop}
    onCancel={() => (stopDialogOpen = false)}
  />
{/if}

<style>
  .terminals-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    background: var(--bg);
  }

  .terminals-view__list-pane {
    gap: 0;
  }

  .terminals-view__list-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0 12px 10px;
  }

  .terminals-view__count {
    min-width: 22px;
    padding: 2px 6px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    text-align: center;
  }

  :global(.terminals-view__list-feedback) {
    margin: 0 12px;
  }

  .terminals-view__list {
    overflow: auto;
  }

  .terminals-view__list-item {
    display: flex;
    width: 100%;
    flex-direction: column;
    gap: 6px;
    text-align: left;
  }

  .terminals-view__item-topline,
  .terminals-view__item-meta,
  .terminals-view__identity-primary,
  .terminals-view__identity-meta,
  .terminals-view__controls,
  .terminals-view__control-toggle {
    display: flex;
    align-items: center;
  }

  .terminals-view__item-topline,
  .terminals-view__item-meta {
    width: 100%;
    justify-content: space-between;
    gap: 8px;
  }

  .terminals-view__command,
  .terminals-view__target,
  .terminals-view__item-meta {
    overflow: hidden;
    font-family: var(--font-mono);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminals-view__command {
    color: var(--text-hi);
    font-size: var(--fs-mono-body);
    font-weight: 500;
  }

  .terminals-view__target {
    width: 100%;
    color: var(--text-med);
    font-size: var(--fs-mono-sm);
  }

  .terminals-view__item-meta {
    color: var(--text-lo);
    font-size: var(--fs-mono-xs);
  }

  .terminals-view__state-dot {
    width: 7px;
    height: 7px;
    flex: 0 0 auto;
    border-radius: 50%;
    background: var(--text-lo);
  }

  .terminals-view__state-dot--working,
  .terminals-view__state-dot--ready {
    background: var(--green);
  }

  .terminals-view__state-dot--starting,
  .terminals-view__state-dot--needs_input {
    background: var(--amber);
  }

  .terminals-view__state-dot--turn_complete {
    background: var(--blue);
  }

  .terminals-view__detail {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    padding: 20px;
  }

  .terminals-view__header {
    padding: 0 0 14px;
  }

  .terminals-view__header-status {
    align-items: center;
  }

  .terminals-view__toolbar {
    margin-bottom: 14px;
  }

  .terminals-view__identity {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 6px;
  }

  .terminals-view__identity-primary {
    min-width: 0;
    gap: 8px;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
  }

  .terminals-view__target-marker {
    color: var(--accent);
  }

  .terminals-view__identity-meta {
    min-width: 0;
    gap: 12px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .terminals-view__identity-meta span:last-child {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminals-view__controls {
    flex: 0 0 auto;
    gap: 12px;
  }

  .terminals-view__control-toggle {
    gap: 8px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }

  :global(.terminals-view__attention),
  :global(.terminals-view__feedback) {
    margin-bottom: 8px;
  }

  :global(.terminals-view__attention strong) {
    margin-right: 8px;
    color: var(--text-hi);
  }

  .terminals-view__terminal-shell {
    display: flex;
    min-height: 240px;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--bg);
  }

  .terminals-view__terminal-shell[data-control='enabled'] {
    border-color: var(--accent-40);
    box-shadow: var(--focus-ring);
  }

  .terminals-view__terminal-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.04em;
  }

  .terminals-view__terminal-shell[data-control='enabled']
    .terminals-view__terminal-mode {
    color: var(--accent);
  }

  .terminals-view__terminal-host {
    min-width: 0;
    min-height: 0;
    flex: 1;
    padding: 10px 12px;
    overflow: hidden;
  }

  .terminals-view__terminal-host :global(.xterm) {
    height: 100%;
  }

  .terminals-view__terminal-host :global(.xterm-viewport),
  .terminals-view__terminal-host :global(.xterm-screen) {
    border-radius: var(--r-sm);
  }

  .terminals-view__composer {
    display: grid;
    grid-template-columns: minmax(180px, 240px) minmax(0, 1fr) auto;
    gap: 12px;
    align-items: end;
    margin-top: 14px;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .terminals-view__composer-copy {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-self: center;
  }

  .terminals-view__composer-label {
    color: var(--text-hi);
    font-size: var(--fs-label-sm);
    font-weight: 600;
  }

  .terminals-view__composer-hint {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  :global(.terminals-view__context-input) {
    min-height: 54px;
    max-height: 112px;
    resize: vertical;
    font-family: var(--font-mono);
  }

  @media (max-width: 960px) {
    .terminals-view__detail {
      padding: 16px;
    }

    .terminals-view__toolbar {
      align-items: flex-start;
    }

    .terminals-view__composer {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .terminals-view__composer-copy {
      grid-column: 1 / -1;
    }
  }

  @media (max-width: 640px) {
    .terminals-view {
      overflow: auto;
      flex-direction: column;
    }

    .terminals-view__list-pane {
      max-height: 220px;
      flex: 0 0 auto;
    }

    .terminals-view__detail {
      min-height: 620px;
      overflow: visible;
    }

    .terminals-view__controls {
      width: 100%;
      justify-content: space-between;
    }

    .terminals-view__composer {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .terminals-view__terminal-host :global(.xterm-viewport) {
      scroll-behavior: auto;
    }
  }
</style>
