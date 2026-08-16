<script>
  import { onMount, tick } from 'svelte';
  import '@xterm/xterm/css/xterm.css';

  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import Dropdown from './Dropdown.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextArea from './ui/TextArea.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
  import {
    TERMINAL_STREAM_CONNECTED,
    TERMINAL_STREAM_CONNECTING,
    TERMINAL_STREAM_ERROR,
    TERMINAL_STREAM_RECONNECTING,
    TERMINAL_STREAM_SNAPSHOT,
    createTerminalsController,
    createTerminalsViewState,
    selectedTerminal,
    terminalIsFinished,
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
  let terminalScrolledBack = $state(false);
  let stopDialogOpen = $state(false);
  let dismissDialogOpen = $state(false);
  let startDialogOpen = $state(false);
  let selectedLaunchHistoryId = $state('');
  let startCommand = $state('');
  let startArguments = $state('');
  let startWorkdir = $state('');
  let mounted = false;
  let rendererPromise = null;
  let pendingSnapshot = '';
  let pendingSnapshotTerminal = null;
  let pendingOutput = [];
  let xterm = null;
  let fitAddon = null;
  let resizeObserver = null;
  let inputDisposable = null;
  let scrollDisposable = null;

  let terminal = $derived(selectedTerminal(viewState));
  let terminalFinished = $derived(terminalIsFinished(terminal));
  let streamStatusLabel = $derived(streamLabel(viewState.streamStatus));
  let streamStatusVariant = $derived(streamVariant(viewState.streamStatus));
  let launchHistoryOptions = $derived(
    viewState.launchHistory.map((entry) => ({
      value: entry.id,
      label: launchHistoryLabel(entry),
      secondaryLabel: launchHistoryWorkdir(entry),
    })),
  );

  const controller = createTerminalsController({
    state: viewState,
    onSnapshot: (ansi, snapshotTerminal) => {
      pendingSnapshot = ansi;
      pendingSnapshotTerminal = snapshotTerminal;
      pendingOutput = [];
      if (!xterm) {
        return;
      }
      writeSnapshot(ansi, snapshotTerminal);
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
      pendingSnapshotTerminal = null;
      pendingOutput = [];
      terminalScrolledBack = false;
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
      terminalScrolledBack = false;
      stopDialogOpen = false;
    }
  });

  $effect(() => {
    if (terminalFinished && controlEnabled) {
      setControlEnabled(false);
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
      pendingSnapshotTerminal = null;
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
    scrollDisposable?.dispose();
    xterm?.dispose();
    resizeObserver = null;
    inputDisposable = null;
    scrollDisposable = null;
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
      cursorBlink: controlEnabled,
      cursorInactiveStyle: 'outline',
      disableStdin: !controlEnabled,
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
    scrollDisposable = xterm.onScroll(() => {
      terminalScrolledBack =
        xterm.buffer.active.viewportY < xterm.buffer.active.baseY;
    });
    if (typeof globalThis.ResizeObserver === 'function') {
      resizeObserver = new ResizeObserver(scheduleFit);
      resizeObserver.observe(terminalHost);
    }
    if (pendingSnapshot) {
      writeSnapshot(pendingSnapshot, pendingSnapshotTerminal);
    }
    for (const data of pendingOutput) {
      xterm.write(data);
    }
    pendingOutput = [];
    scheduleFit();
    if (controlEnabled) {
      xterm.focus();
    }
  }

  function writeSnapshot(ansi, snapshotTerminal) {
    if (!xterm) {
      return;
    }
    const columns = snapshotTerminal?.columns;
    const rows = snapshotTerminal?.rows;
    if (
      Number.isInteger(columns) &&
      Number.isInteger(rows) &&
      columns > 0 &&
      rows > 0 &&
      (xterm.cols !== columns || xterm.rows !== rows)
    ) {
      xterm.resize(columns, rows);
    }
    xterm.reset();
    terminalScrolledBack = false;
    xterm.write(ansi, () => {
      xterm?.refresh(0, xterm.rows - 1);
      scheduleFit();
    });
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
    controlEnabled =
      enabled === true &&
      Boolean(viewState.selectedTerminalId) &&
      !terminalFinished;
    if (!xterm) {
      return;
    }
    xterm.options.disableStdin = !controlEnabled;
    xterm.options.cursorBlink = controlEnabled;
    if (controlEnabled) {
      xterm.focus();
    }
  }

  function scrollToLatest() {
    xterm?.scrollToBottom();
    terminalScrolledBack = false;
  }

  function takeTerminalControl(event) {
    if (
      event.button !== 0 ||
      !terminal ||
      terminalFinished ||
      serverUnavailable
    ) {
      return;
    }
    setControlEnabled(true);
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

  async function confirmDismiss() {
    const dismissed = await controller.forgetSelected();
    dismissDialogOpen = false;
    if (dismissed) {
      onToast({
        title: t('terminals.dismissedTitle', 'Terminal dismissed'),
        message: t(
          'terminals.dismissedMessage',
          'The retained Terminal Session was removed from the list.',
        ),
        variant: 'success',
      });
    }
  }

  function openStartDialog() {
    applyLaunchHistory(viewState.launchHistory[0] ?? null);
    viewState.startError = '';
    startDialogOpen = true;
  }

  function applyLaunchHistory(entry) {
    selectedLaunchHistoryId = entry?.id ?? '';
    startCommand = entry?.command ?? '';
    startArguments = Array.isArray(entry?.args) ? entry.args.join('\n') : '';
    startWorkdir = entry?.workdir ?? '';
    viewState.startError = '';
  }

  function selectLaunchHistory(entryId) {
    const entry = viewState.launchHistory.find((item) => item.id === entryId);
    if (entry) {
      applyLaunchHistory(entry);
    }
  }

  function markLaunchHistoryEdited() {
    selectedLaunchHistoryId = '';
    viewState.startError = '';
  }

  function launchHistoryLabel(entry) {
    const command =
      String(entry?.command || '').trim() ||
      t('terminals.commandPlaceholder', 'Default shell');
    const argumentsList = Array.isArray(entry?.args) ? entry.args : [];
    return [command, ...argumentsList].join(' ');
  }

  function launchHistoryWorkdir(entry) {
    return (
      String(entry?.workdir || '').trim() ||
      t('terminals.workdirPlaceholder', 'User home directory')
    );
  }

  function closeStartDialog() {
    if (viewState.startingTerminal) {
      return;
    }
    startDialogOpen = false;
    viewState.startError = '';
  }

  async function submitStartTerminal(event) {
    event.preventDefault();
    const command = startCommand.trim();
    const workdir = startWorkdir.trim();
    const args = startArguments
      .split(/\r?\n/)
      .filter((argument) => argument.length > 0);
    const params = {};
    if (command) {
      params.command = command;
    }
    if (args.length) {
      params.args = args;
    }
    if (workdir) {
      params.workdir = workdir;
    }

    const started = await controller.startManualTerminal(params);
    if (!started) {
      return;
    }
    startDialogOpen = false;
    await tick();
    setControlEnabled(true);
    onToast({
      title: t('terminals.startedTitle', 'Terminal started'),
      message: t(
        'terminals.startedMessage',
        'The manual Terminal Session is live and ready for input.',
      ),
      variant: 'success',
    });
  }

  function terminalTarget(item) {
    if (!item?.owner) {
      return t('terminals.manualOwner', 'Manual');
    }
    const agentId = item?.owner?.agent_id || '—';
    const projectId = item?.owner?.project_id;
    return projectId ? `${agentId}@${projectId}` : agentId;
  }

  function terminalTitle(item) {
    const announcedTitle =
      typeof item?.title === 'string' ? item.title.trim() : '';
    if (announcedTitle) {
      return announcedTitle;
    }
    const command = String(item?.command || '').trim();
    const executable = command.split(/[\\/]/).pop()?.toLowerCase() || '';
    const labels = {
      'pwsh.exe': 'PowerShell',
      pwsh: 'PowerShell',
      'powershell.exe': 'Windows PowerShell',
      powershell: 'Windows PowerShell',
      'cmd.exe': 'Command Prompt',
      cmd: 'Command Prompt',
      'bash.exe': 'Bash',
      bash: 'Bash',
      zsh: 'Zsh',
      fish: 'Fish',
    };
    return labels[executable] || command || 'Terminal';
  }

  function terminalFullCommand(item) {
    const command = String(item?.command || '').trim();
    const args = Array.isArray(item?.arguments) ? item.arguments : [];
    if (!command && args.length === 0) {
      return '';
    }
    return [command, ...args].join(' ');
  }

  function terminalSession(item) {
    return (
      item?.owner?.session_id || t('terminals.localOperator', 'Local operator')
    );
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
    if (state === 'starting') {
      return 'warn';
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
    if (status === TERMINAL_STREAM_SNAPSHOT) {
      return t('terminals.stream.snapshot', 'History loaded');
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
    if (status === TERMINAL_STREAM_SNAPSHOT) {
      return 'neutral';
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
  <aside
    class="terminals-view__list-pane secondary-pane"
    aria-labelledby="terminals-list-title"
  >
    <div class="secondary-pane__header">
      <span id="terminals-list-title" class="secondary-pane__title">
        {t('terminals.title', 'Terminals')}
      </span>
      <Button
        variant="primary"
        disabled={serverUnavailable}
        onClick={openStartDialog}
      >
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {t('common.add', 'Add')}
      </Button>
    </div>

    <div class="secondary-pane__scroll secondary-list terminals-view__list">
      {#if viewState.loading && viewState.terminals.length === 0}
        <Banner variant="neutral">
          {t('terminals.loading', 'Loading terminal sessions…')}
        </Banner>
      {:else if viewState.listError && !serverUnavailable}
        <Banner variant="error">
          <span
            >{t(
              'terminals.listError',
              'Terminal sessions could not be loaded.',
            )}</span
          >
          <Button
            variant="secondary"
            onClick={() => controller.loadTerminals()}
          >
            {t('common.retry', 'Retry')}
          </Button>
        </Banner>
      {:else if viewState.terminals.length === 0}
        <EmptyState
          title={t('terminals.emptyTitle', 'No terminal sessions')}
          description={t(
            'terminals.emptyDescription',
            'Open a manual terminal here, or monitor Terminal Sessions started by an agent.',
          )}
        />
      {:else}
        {#each viewState.terminals as item (item.terminal_id)}
          <button
            type="button"
            class="secondary-list__item terminals-view__list-item"
            class:active={item.terminal_id === viewState.selectedTerminalId}
            aria-current={item.terminal_id === viewState.selectedTerminalId
              ? 'true'
              : undefined}
            onclick={() => controller.selectTerminal(item.terminal_id)}
          >
            <span class="terminals-view__item-topline">
              <span
                class="terminals-view__item-title"
                use:tooltip={terminalTitle(item)}>{terminalTitle(item)}</span
              >
              <span
                class={`terminals-view__state-dot terminals-view__state-dot--${item.state}`}
                role="img"
                aria-label={stateLabel(item.state)}
                use:tooltip={stateLabel(item.state)}
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
      {/if}
    </div>
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
            'Open and use your own terminals, or watch and control the same interactive terminal an agent is using.',
          )}
        </p>
      </div>
      <div class="view-header__actions terminals-view__header-status">
        {#if terminal}
          <StatusChip variant={stateVariant(terminal.state)}>
            {stateLabel(terminal.state)}
          </StatusChip>
          <StatusChip variant={streamStatusVariant}>
            {streamStatusLabel}
          </StatusChip>
        {/if}
      </div>
    </header>

    {#if terminal}
      <div class="terminals-view__toolbar view-toolbar view-toolbar--split">
        <div class="terminals-view__identity">
          <span class="terminals-view__identity-primary">
            <span use:tooltip={terminalTitle(terminal)}
              >{terminalTitle(terminal)}</span
            >
            <span class="terminals-view__target-marker"
              >{terminalTarget(terminal)}</span
            >
          </span>
          <span class="terminals-view__identity-meta">
            <span use:tooltip={terminalFullCommand(terminal)}
              >{terminal.command}</span
            >
            {#if Array.isArray(terminal.arguments) && terminal.arguments.length > 0}
              <span
                use:tooltip={terminal.arguments.join(' ')}
                class="terminals-view__identity-args"
              >
                {terminal.arguments.length}
                {terminal.arguments.length === 1 ? 'arg' : 'args'}
              </span>
            {/if}
            <span>PID {terminal.pid}</span>
            <span>{terminal.columns}×{terminal.rows}</span>
            {#if terminalFinished && terminal.exit_code != null}
              <span
                class="terminals-view__exit-code"
                class:terminals-view__exit-code--nonzero={terminal.exit_code !==
                  0}
              >
                {t('terminals.exitCode', 'Exit code')}
                {terminal.exit_code}
              </span>
            {/if}
            <span use:tooltip={terminal.workdir}>{terminal.workdir}</span>
          </span>
        </div>
        {#if !terminalFinished}
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
        {:else}
          <div class="terminals-view__controls">
            <Button
              variant="secondary"
              loading={viewState.forgetting}
              onClick={() => (dismissDialogOpen = true)}
            >
              {t('terminals.dismiss', 'Dismiss')}
            </Button>
          </div>
        {/if}
      </div>

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
            {terminalFinished
              ? t(
                  'terminals.mode.history',
                  'Read-only history — retained temporarily after exit',
                )
              : controlEnabled
                ? t(
                    'terminals.mode.control',
                    'Control enabled — keystrokes go to the process',
                  )
                : t(
                    'terminals.mode.observe',
                    'Observe mode — click terminal to take control',
                  )}
          </span>
          <div class="terminals-view__terminal-bar-actions">
            {#if terminalScrolledBack}
              <button
                type="button"
                class="terminals-view__latest-action"
                onclick={scrollToLatest}
              >
                {t('terminals.scrollLatest', 'Jump to latest')}
              </button>
            {/if}
            <span>PTY</span>
          </div>
        </div>
        <div
          use:mountTerminal
          class="terminals-view__terminal-host"
          role="group"
          aria-label={t(
            terminalFinished
              ? 'terminals.historyTerminalLabel'
              : 'terminals.liveTerminalLabel',
            terminalFinished
              ? 'Retained terminal history.'
              : 'Live terminal. Click to take control.',
          )}
          onpointerdown={takeTerminalControl}
        ></div>
      </div>
    {:else}
      <EmptyState
        fill
        title={t('terminals.detailEmptyTitle', 'Open a terminal')}
        description={t(
          'terminals.detailEmptyDescription',
          'Start the local default shell or choose a command such as codex. Agent terminals will appear here too.',
        )}
      >
        {#snippet actions()}
          <Button
            variant="primary"
            disabled={serverUnavailable}
            onClick={openStartDialog}
          >
            {t('terminals.new', 'New terminal')}
          </Button>
        {/snippet}
      </EmptyState>
    {/if}
  </div>
</section>

{#if startDialogOpen}
  <Modal
    title={t('terminals.startTitle', 'New terminal')}
    labelledById="terminal-start-modal-title"
    class="terminals-view__start-modal"
    closeDisabled={viewState.startingTerminal}
    onClose={closeStartDialog}
  >
    {#snippet body()}
      <form id="terminal-start-form" onsubmit={submitStartTerminal}>
        <div class="modal-body terminals-view__start-form">
          <p class="terminals-view__start-intro">
            {t(
              'terminals.startIntro',
              'Leave Command empty to open the server user’s default shell. Every command uses the same real PTY / ConPTY.',
            )}
          </p>

          {#if viewState.startError && !serverUnavailable}
            <Banner variant="error" role="alert">
              {terminalError(viewState.startError)}
            </Banner>
          {/if}

          {#if viewState.launchHistory.length > 0}
            <FormField
              controlId="terminal-start-history"
              label={t('terminals.historyLabel', 'Recent setup')}
              help={t(
                'terminals.historyHelp',
                'Saved on this vBot server. Choosing a setup fills Command, Arguments, and Working directory.',
              )}
            >
              {#snippet children(field)}
                <Dropdown
                  id={field.controlId}
                  value={selectedLaunchHistoryId}
                  options={launchHistoryOptions}
                  ariaLabel={t('terminals.historyLabel', 'Recent setup')}
                  ariaDescribedby={field.describedBy}
                  disabled={viewState.startingTerminal}
                  triggerClass="terminals-view__history-dropdown"
                  listClass="terminals-view__history-dropdown-list"
                  onValueChange={selectLaunchHistory}
                />
              {/snippet}
            </FormField>
          {/if}

          <FormField
            controlId="terminal-start-command"
            label={t('terminals.commandLabel', 'Command')}
            help={t(
              'terminals.commandHelp',
              'Optional. For example: codex, powershell, bash, or python.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={startCommand}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.commandPlaceholder', 'Default shell')}
                onInput={(next) => {
                  startCommand = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField
            controlId="terminal-start-arguments"
            label={t('terminals.argumentsLabel', 'Arguments')}
            help={t(
              'terminals.argumentsHelp',
              'Optional. Enter one exact argument per line; spaces within a line are preserved.',
            )}
          >
            {#snippet children(field)}
              <TextArea
                id={field.controlId}
                code
                rows={4}
                aria-describedby={field.describedBy}
                value={startArguments}
                disabled={viewState.startingTerminal}
                placeholder={t(
                  'terminals.argumentsPlaceholder',
                  '--profile\nwork',
                )}
                onInput={(next) => {
                  startArguments = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField
            controlId="terminal-start-workdir"
            label={t('terminals.workdirLabel', 'Working directory')}
            help={t(
              'terminals.workdirHelp',
              'Optional. Defaults to the server user’s home directory.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={startWorkdir}
                disabled={viewState.startingTerminal}
                placeholder={t(
                  'terminals.workdirPlaceholder',
                  'User home directory',
                )}
                onInput={(next) => {
                  startWorkdir = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={viewState.startingTerminal}
        onClick={closeStartDialog}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-start-form"
        variant="primary"
        loading={viewState.startingTerminal}
      >
        {t('terminals.start', 'Start terminal')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if stopDialogOpen && terminal && !terminalFinished}
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

{#if dismissDialogOpen && terminal && terminalFinished}
  <ConfirmDialog
    title={t('terminals.dismissConfirmTitle', 'Dismiss this Terminal Session?')}
    body={t(
      'terminals.dismissConfirmBody',
      'The retained history will be removed from this list immediately. The Terminal Session cannot be resumed afterward.',
    )}
    confirmLabel={t('terminals.dismiss', 'Dismiss')}
    onConfirm={confirmDismiss}
    onCancel={() => (dismissDialogOpen = false)}
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

  .terminals-view__item-title,
  .terminals-view__target,
  .terminals-view__item-meta {
    overflow: hidden;
    font-family: var(--font-mono);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminals-view__item-title {
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

  .terminals-view__state-dot--starting {
    background: var(--amber);
  }

  .terminals-view__state-dot--error {
    background: var(--red);
  }

  .terminals-view__detail {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    height: 100%;
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

  .terminals-view__identity-args {
    color: var(--text-lo);
  }

  .terminals-view__exit-code {
    font-weight: 500;
    color: var(--text-med);
  }

  .terminals-view__exit-code--nonzero {
    color: var(--red);
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

  :global(.terminals-view__feedback) {
    margin-bottom: 8px;
  }

  :global(.modal.terminals-view__start-modal) {
    width: 520px;
  }

  .terminals-view__start-form {
    max-height: min(640px, 70vh);
    overflow-y: auto;
  }

  .terminals-view__start-intro {
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }

  :global(.terminals-view__history-dropdown) {
    width: 100%;
  }

  :global(.terminals-view__history-dropdown .dropdown-primitive__trigger) {
    width: 100%;
  }

  :global(.terminals-view__history-dropdown-list) {
    font-family: var(--font-mono);
  }

  :global(.terminals-view__attention strong) {
    margin-right: 8px;
    color: var(--text-hi);
  }

  .terminals-view__terminal-shell {
    display: flex;
    min-height: 240px;
    flex: 1 1 0;
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

  .terminals-view__terminal-bar-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .terminals-view__latest-action {
    padding: 0;
    border: 0;
    color: var(--accent);
    background: transparent;
    font: inherit;
  }

  .terminals-view__latest-action:hover,
  .terminals-view__latest-action:focus-visible {
    color: var(--text-hi);
    outline: none;
    text-decoration: underline;
    text-underline-offset: 3px;
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

  .terminals-view__terminal-host
    :global(.xterm-scrollable-element > .scrollbar.vertical) {
    opacity: 1 !important;
    pointer-events: auto !important;
    background: var(--surface-2);
  }

  .terminals-view__terminal-host
    :global(.xterm-scrollable-element > .scrollbar.vertical > .slider) {
    border: 2px solid var(--surface-2);
    border-radius: var(--r-sm);
    background: var(--text-lo);
  }

  @media (max-width: 960px) {
    .terminals-view__detail {
      padding: 16px;
    }

    .terminals-view__toolbar {
      align-items: flex-start;
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
  }

  @media (prefers-reduced-motion: reduce) {
    .terminals-view__terminal-host :global(.xterm-viewport) {
      scroll-behavior: auto;
    }
  }
</style>
