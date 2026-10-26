<script>
  // Launcher of the Terminals view: a small command palette that starts a
  // terminal. It opens from the start button's chevron or the empty state,
  // is portaled to <body> and placed by the shared panel positioning
  // (`computePanelPosition`), and becomes a bottom-sheet dialog on phones.
  //
  // The command line is a combobox over the recent setups (the launch
  // history): typing filters them, the arrow keys move between the typed
  // command and the setups, and Enter (or Start) starts the active one - the
  // typed command line, empty for the default shell, or a whole recent setup
  // with its own working directory. Tab on a setup copies it into the
  // launcher to edit it. A quiet options row sets the working directory (the
  // last used one by default) and the view the terminal shows in (the shown
  // view by default); the footer offers a new group and Start.
  //
  // The caller renders the launcher while it is open and clears it in
  // `onClose`. `onStarted(terminalId)` follows a successful start;
  // `onNewGroup()` asks for the New group dialog once the launcher closed.
  import { onMount, tick, untrack } from 'svelte';

  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import { trimTrailingSeparator } from '$lib/pathPicker.js';
  import {
    formatTerminalCommandLine,
    parseTerminalCommandLine,
    pinTargetOf,
  } from '$lib/terminalsView.js';
  import { tooltip } from '$lib/tooltip.js';
  import { computerOptions, useComputers } from '$lib/computers.svelte.js';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import PathField from '../ui/PathField.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    groupKindLabel,
    launchHistoryDetails,
    launchHistoryLabel,
    terminalError,
    viewLabel,
  } from './terminalLabels.js';

  const noop = () => {};
  const PANEL_WIDTH = 480;
  const LIST_MAX_HEIGHT = 288;
  const PHONE_QUERY = '(max-width: 640px)';
  const FOCUSABLE =
    'button:not([disabled]):not([tabindex="-1"]), input:not([disabled]), [tabindex="0"]';
  // Layers opened from inside the launcher live at <body> too: the view
  // picker, path suggestions, the folder browser, hints. Pressing in them
  // keeps the launcher open.
  const NESTED_LAYERS =
    '.s-dropdown-panel, .dropdown-list, .modal-overlay, .floating-tooltip, .floating-card';

  let {
    anchor,
    // 'end' aligns the panel's end with the anchor (the bar's start
    // button), 'center' centers it under the anchor (the empty state).
    align = 'end',
    viewState,
    controller,
    onClose = noop,
    onStarted = noop,
    onNewGroup = noop,
  } = $props();

  const componentId = $props.id();
  const inputId = `${componentId}-command`;
  const errorId = `${componentId}-error`;
  const listId = `${componentId}-setups`;
  const listLabelId = `${componentId}-setups-label`;
  const workdirFieldId = `${componentId}-workdir`;
  const optionId = (index) => `${componentId}-setup-${index}`;
  const navigation = useNavigation();
  const computers = useComputers();
  let computerId = $state('server');
  const computerChoices = $derived(
    computerOptions(computers?.computers ?? [], [computerId], 'terminal'),
  );
  const selectedComputer = $derived(
    computers?.computers.find((computer) => computer.id === computerId),
  );
  const computerUnavailable = $derived(
    selectedComputer?.known
      ? !selectedComputer.online ||
          !selectedComputer.capabilities.includes('terminal')
      : computerId !== 'server',
  );
  const launchHistory = $derived(
    viewState.launchHistory.filter(
      (entry) => (entry.computer_id ?? 'server') === computerId,
    ),
  );

  let query = $state('');
  let commandError = $state('');
  // The last used working directory and the shown view.
  let workdir = $state(
    untrack(
      () =>
        viewState.launchHistory.find(
          (entry) => (entry.computer_id ?? 'server') === 'server',
        )?.workdir ?? '',
    ),
  );
  let groupId = $state(untrack(() => pinTargetOf(shownView()) ?? ''));
  // -1 is the typed command line; 0.. a recent setup.
  let activeIndex = $state(-1);
  // A setup made active by the pointer stops being active when it leaves.
  let activeByPointer = false;
  let editingWorkdir = $state(false);
  let workdirBeforeEdit = '';
  let sheet = $state(phoneQuery()?.matches === true);
  let panelElement = $state();
  let listElement = $state();
  let inputElement = $state();
  let workdirChip = $state();
  let panelStyle = $state('');
  let placed = $state(false);

  const rows = $derived(setupRows(launchHistory, query, workdir));
  const errorText = $derived(
    commandError ||
      (viewState.startError ? terminalError(viewState.startError) : ''),
  );
  // Where a terminal can show: the user's groups, the conversations, and
  // the user's own terminals (no pin, value '').
  const viewOptions = $derived([
    {
      value: '',
      label: viewLabel({ kind: 'manual' }),
      secondaryLabel: groupKindLabel('manual'),
    },
    ...viewState.groups
      .filter((group) => pinTargetOf(group) !== null)
      .map((group) => ({
        value: group.group_id,
        label: viewLabel(group),
        secondaryLabel: groupKindLabel(group.kind),
      })),
  ]);
  const viewName = $derived(
    (viewOptions.find((option) => option.value === groupId) ?? viewOptions[0])
      .label,
  );
  const workdirPath = $derived(workdir.trim());
  const workdirHint = $derived({
    title: t('terminals.workdirLabel'),
    text: workdirPath || t('terminals.workdirHelp'),
    mono: Boolean(workdirPath),
    selectable: Boolean(workdirPath),
    placement: 'bottom',
  });

  function shownView() {
    return (
      viewState.groups.find(
        (group) => group.group_id === viewState.selectedGroupId,
      ) ?? null
    );
  }

  function phoneQuery() {
    return typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function'
      ? window.matchMedia(PHONE_QUERY)
      : null;
  }

  function samePath(left, right) {
    return trimTrailingSeparator(left.trim()) === trimTrailingSeparator(right);
  }

  // The recent setups the launcher offers for `typed`: each distinct one
  // once, without the default shell in the chosen folder (the empty command
  // line starts that), matching every typed word in what the row shows. A
  // setup names its folder only where it differs from the chosen one.
  function setupRows(history, typed, chosenFolder) {
    const words = typed.toLowerCase().split(/\s+/).filter(Boolean);
    const folder = chosenFolder.trim();
    const seen = {};
    const result = [];
    for (const entry of history) {
      const commandLine = formatTerminalCommandLine(entry.command, entry.args);
      const entryFolder = String(entry.workdir ?? '').trim();
      const key = `${commandLine}\u0000${trimTrailingSeparator(entryFolder)}`;
      if (seen[key]) continue;
      seen[key] = true;
      const sameFolder = samePath(entryFolder, folder);
      if (!commandLine && sameFolder) continue;
      const label = launchHistoryLabel(entry);
      const shownFolder = sameFolder
        ? ''
        : entryFolder || t('terminals.workdirPlaceholder');
      const searchable = `${label} ${shownFolder}`.toLowerCase();
      if (!words.every((word) => searchable.includes(word))) continue;
      result.push({
        entry,
        label,
        code: Boolean(commandLine),
        folder: shownFolder,
        folderCode: Boolean(entryFolder),
      });
    }
    return result;
  }

  function folderName(path) {
    const parts = path.split(/[\\/]+/).filter(Boolean);
    if (parts.length === 0) return path;
    const last = parts.at(-1);
    return parts.length === 1 && /^[A-Za-z]:$/.test(last) ? `${last}\\` : last;
  }

  // The caller unmounts the launcher in `onClose`, so everything needed
  // afterwards is read before.
  function close({ restoreFocus = false } = {}) {
    const opener = restoreFocus && !sheet ? anchor : null;
    onClose();
    if (opener?.isConnected) {
      opener.focus({ preventScroll: true });
    }
  }

  function clearErrors() {
    commandError = '';
    viewState.startError = '';
  }

  function handleInput(event) {
    query = event.currentTarget.value;
    activeIndex = -1;
    activeByPointer = false;
    clearErrors();
  }

  async function setActive(index) {
    activeIndex = index;
    activeByPointer = false;
    await tick();
    if (index >= 0) {
      document
        .getElementById(optionId(index))
        ?.scrollIntoView?.({ block: 'nearest' });
    }
  }

  // The arrows cycle through the typed command line and the setups.
  function move(step) {
    const count = rows.length + 1;
    void setActive(((activeIndex + 1 + step + count) % count) - 1);
  }

  function handleInputKeydown(event) {
    if (isImeComposing(event)) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      move(event.key === 'ArrowDown' ? 1 : -1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      void startActive();
    } else if (
      event.key === 'Tab' &&
      !event.shiftKey &&
      activeIndex >= 0 &&
      rows[activeIndex]
    ) {
      // Like a completion: the setup moves into the launcher to edit.
      event.preventDefault();
      editSetup(rows[activeIndex].entry);
    }
  }

  function editSetup(entry) {
    query = formatTerminalCommandLine(entry.command, entry.args);
    workdir = entry.workdir ?? '';
    activeIndex = -1;
    activeByPointer = false;
    clearErrors();
  }

  function startActive() {
    const row = activeIndex >= 0 ? rows[activeIndex] : null;
    return row ? startSetup(row.entry) : startTyped();
  }

  async function startTyped() {
    const parsed = parseTerminalCommandLine(query);
    if (parsed.error) {
      commandError = t(`terminals.commandError.${parsed.error}`);
      inputElement?.focus();
      return;
    }
    const params = {};
    if (parsed.command) params.command = parsed.command;
    if (parsed.args.length) params.args = parsed.args;
    if (workdirPath) params.workdir = workdirPath;
    await start(params);
  }

  async function startSetup(entry) {
    const params = {};
    if (entry.command) params.command = entry.command;
    if (entry.args?.length) params.args = [...entry.args];
    if (entry.workdir) params.workdir = entry.workdir;
    await start(params);
  }

  async function start(params) {
    if (viewState.startingTerminal || computerUnavailable) return;
    commandError = '';
    const started = await controller.startManualTerminal(
      {
        ...params,
        ...(computerId !== 'server' ? { computer_id: computerId } : {}),
      },
      {
        groupId: groupId || null,
      },
    );
    if (!started) {
      inputElement?.focus();
      return;
    }
    const showStarted = onStarted;
    close();
    await showStarted(started.terminal_id);
  }

  function newGroup() {
    const openNewGroup = onNewGroup;
    close({ restoreFocus: true });
    openNewGroup();
  }

  function selectComputer(id) {
    computerId = id;
    workdir =
      viewState.launchHistory.find(
        (entry) => (entry.computer_id ?? 'server') === id,
      )?.workdir ?? '';
    activeIndex = -1;
    clearErrors();
  }

  async function editWorkdir() {
    workdirBeforeEdit = workdir;
    editingWorkdir = true;
    await tick();
    document.getElementById(workdirFieldId)?.focus();
  }

  async function finishWorkdir({ keep }) {
    workdir = keep ? trimTrailingSeparator(workdir.trim()) : workdirBeforeEdit;
    editingWorkdir = false;
    await tick();
    (keep ? inputElement : workdirChip)?.focus();
  }

  // Keys the path field leaves over (it completes with Enter and Tab while
  // it offers folders, and closes its list with Escape).
  function handleWorkdirKeydown(event) {
    if (isImeComposing(event)) return;
    if (event.key === 'Enter' || event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      void finishWorkdir({ keep: event.key === 'Enter' });
    }
  }

  function handleRowPointerMove(index) {
    if (activeIndex === index && activeByPointer) return;
    activeIndex = index;
    activeByPointer = true;
  }

  function handleListPointerLeave() {
    if (activeByPointer) {
      activeIndex = -1;
      activeByPointer = false;
    }
  }

  // Tab stays inside the open launcher.
  function keepTabInside(event) {
    const focusable = Array.from(
      panelElement?.querySelectorAll(FOCUSABLE) ?? [],
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    const current = document.activeElement;
    if (event.shiftKey && (current === first || !focusable.includes(current))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && current === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handlePanelKeydown(event) {
    if (event.defaultPrevented || isImeComposing(event)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close({ restoreFocus: true });
    } else if (event.key === 'Tab') {
      keepTabInside(event);
    }
  }

  // An anchor object `computePanelPosition` can center the panel under.
  function centeredAnchor() {
    return {
      getBoundingClientRect() {
        const rect = anchor.getBoundingClientRect();
        const center = rect.left + rect.width / 2;
        return {
          top: rect.top,
          bottom: rect.bottom,
          height: rect.height,
          left: center - PANEL_WIDTH / 2,
          right: center + PANEL_WIDTH / 2,
          width: PANEL_WIDTH,
        };
      },
    };
  }

  function place() {
    if (sheet || !panelElement || !anchor?.isConnected) return;
    const listHeight = listElement?.offsetHeight ?? 0;
    const { left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(align === 'center' ? centeredAnchor() : anchor, {
        panelWidth: PANEL_WIDTH,
        horizontalAlign: align === 'center' ? 'start' : 'end',
        reservedHeight: panelElement.offsetHeight - listHeight,
        contentHeight: listElement?.scrollHeight ?? 0,
        maxHeight: LIST_MAX_HEIGHT,
      });
    panelStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `--terminal-launcher-list-max: ${optionsMaxHeight}px`,
    ].join('; ');
    placed = true;
  }

  onMount(() => {
    void computers?.refresh();
    viewState.startError = '';
    place();
    void tick().then(() => inputElement?.focus());
  });

  // The panel grows or shrinks with its content; keep it inside the window.
  $effect(() => {
    void rows.length;
    void errorText;
    void editingWorkdir;
    void sheet;
    void tick().then(place);
  });

  // A view that disappeared while open falls back to the user's terminals.
  $effect(() => {
    if (groupId && !viewOptions.some((option) => option.value === groupId)) {
      groupId = '';
    }
  });

  $effect(() => {
    const media = phoneQuery();
    if (!media) return undefined;
    const update = () => (sheet = media.matches);
    media.addEventListener?.('change', update);
    return () => media.removeEventListener?.('change', update);
  });

  // Back closes the popover (the sheet is a Modal, which registers itself).
  $effect(() => {
    if (sheet) return undefined;
    return navigation?.registerLayer({
      close: () => close({ restoreFocus: true }),
    });
  });

  // A press outside closes the popover; the anchor toggles it itself.
  $effect(() => {
    if (sheet) return undefined;
    const handlePointerDown = (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (panelElement?.contains(target) || anchor?.contains(target)) return;
      if (target.closest(NESTED_LAYERS)) return;
      close();
    };
    window.addEventListener('pointerdown', handlePointerDown, true);
    window.addEventListener('resize', place);
    return () => {
      window.removeEventListener('pointerdown', handlePointerDown, true);
      window.removeEventListener('resize', place);
    };
  });
</script>

{#snippet commandRow()}
  <div class="terminal-launcher__command">
    <svg
      class="terminal-launcher__prompt"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      aria-hidden="true"
    >
      <path d="m3.5 4.5 3.5 3.5-3.5 3.5M8.5 12h4" />
    </svg>
    <input
      bind:this={inputElement}
      id={inputId}
      class="terminal-launcher__input"
      type="text"
      value={query}
      placeholder={t('terminals.commandPlaceholder')}
      aria-label={t('terminals.commandLabel')}
      role="combobox"
      aria-autocomplete="list"
      aria-expanded={rows.length > 0}
      aria-controls={rows.length > 0 ? listId : undefined}
      aria-activedescendant={activeIndex >= 0
        ? optionId(activeIndex)
        : undefined}
      aria-invalid={errorText ? 'true' : undefined}
      aria-describedby={errorText ? errorId : undefined}
      autocomplete="off"
      autocapitalize="off"
      spellcheck="false"
      oninput={handleInput}
      onkeydown={handleInputKeydown}
    />
    <InfoHint
      class="terminal-launcher__help"
      text={t('terminals.commandHelp')}
      ariaLabel={t('terminals.commandLabel')}
    />
  </div>
  {#if errorText}
    <p id={errorId} class="terminal-launcher__error" role="alert">
      {errorText}
    </p>
  {/if}
{/snippet}

{#snippet setupList()}
  {#if rows.length > 0}
    <div class="terminal-launcher__setups">
      <div id={listLabelId} class="terminal-launcher__section">
        {t('terminals.historyLabel')}
      </div>
      <div
        bind:this={listElement}
        id={listId}
        class="terminal-launcher__list"
        role="listbox"
        tabindex="-1"
        aria-labelledby={listLabelId}
        onpointerleave={handleListPointerLeave}
      >
        {#each rows as row, index (row.entry.id)}
          <button
            id={optionId(index)}
            type="button"
            tabindex="-1"
            class="terminal-launcher__setup"
            class:active={index === activeIndex}
            role="option"
            aria-selected={index === activeIndex}
            use:tooltip={{
              ...launchHistoryDetails(row.entry),
              placement: 'left',
            }}
            onmousedown={(event) => event.preventDefault()}
            onpointermove={() => handleRowPointerMove(index)}
            onclick={() => void startSetup(row.entry)}
          >
            <span
              class="terminal-launcher__setup-command"
              class:terminal-launcher__setup-command--code={row.code}
              >{row.label}</span
            >
            {#if row.folder}
              <span
                class="terminal-launcher__setup-folder"
                class:terminal-launcher__setup-folder--code={row.folderCode}
                ><bdi>{row.folder}</bdi></span
              >
            {/if}
          </button>
        {/each}
      </div>
    </div>
  {/if}
{/snippet}

{#snippet optionsRow()}
  <div class="terminal-launcher__options">
    <SearchableDropdown
      value={computerId}
      options={computerChoices}
      ariaLabel={t('computers.label')}
      panelMinWidth={280}
      onValueChange={selectComputer}
      onOpenChange={(open) => {
        if (open) void computers?.refresh();
      }}
    />
    {#if computerUnavailable}
      <span class="terminal-launcher__error" role="status"
        >{selectedComputer?.online === false
          ? t('computers.offline')
          : t('computers.terminalUnavailable')}</span
      >
    {/if}
    {#if computers?.error}
      <span class="terminal-launcher__error" role="alert"
        >{computers.error}</span
      >
    {/if}
  </div>
  <div class="terminal-launcher__options">
    {#if editingWorkdir}
      {#if computerId !== 'server'}
        <TextField
          id={workdirFieldId}
          class="terminal-launcher__path-input"
          code
          value={workdir}
          ariaLabel={t('computers.remoteWorkdir')}
          placeholder={t('terminals.workdirPlaceholder')}
          onInput={(next) => (workdir = next)}
          onkeydown={handleWorkdirKeydown}
        />
      {:else}
        <PathField
          id={workdirFieldId}
          class="terminal-launcher__path"
          inputClass="terminal-launcher__path-input"
          value={workdir}
          mode="directory"
          projectShortcuts
          ariaLabel={t('terminals.workdirLabel')}
          placeholder={t('terminals.workdirPlaceholder')}
          onInput={(next) => (workdir = next)}
          onkeydown={handleWorkdirKeydown}
        />
      {/if}
    {:else}
      <button
        bind:this={workdirChip}
        type="button"
        class="terminal-launcher__chip terminal-launcher__chip--folder"
        aria-label={t('terminals.workdirChip', {
          path: workdirPath || t('terminals.workdirPlaceholder'),
        })}
        use:tooltip={workdirHint}
        onclick={() => void editWorkdir()}
      >
        <svg
          class="terminal-launcher__chip-icon"
          viewBox="0 0 16 16"
          width="14"
          height="14"
          aria-hidden="true"
        >
          <path d="M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75z" />
        </svg>
        <span
          class="terminal-launcher__chip-value"
          class:terminal-launcher__chip-value--code={Boolean(workdirPath)}
          >{workdirPath
            ? folderName(workdirPath)
            : t('terminals.workdirPlaceholder')}</span
        >
      </button>
    {/if}
    <SearchableDropdown
      value={groupId}
      options={viewOptions}
      ariaLabel={t('terminals.startGroupChip', { view: viewName })}
      triggerClass="terminal-launcher__view-picker"
      triggerTooltip={{
        text: t('terminals.startGroupHelp'),
        placement: 'bottom',
      }}
      panelMinWidth={260}
      onValueChange={(next) => (groupId = next)}
    >
      {#snippet triggerContent()}
        <svg
          class="terminal-launcher__chip-icon"
          viewBox="0 0 16 16"
          width="14"
          height="14"
          aria-hidden="true"
        >
          <rect x="2" y="2.75" width="12" height="10.5" rx="1.5" />
          <path d="M2 6h12" />
        </svg>
        <span class="terminal-launcher__chip-label"
          >{t('terminals.startGroupLabel')}</span
        >
        <span class="terminal-launcher__chip-value">{viewName}</span>
        <svg
          class="dropdown-chevron"
          viewBox="0 0 12 12"
          width="10"
          height="10"
          aria-hidden="true"
        >
          <path d="M2 4l4 4 4-4" />
        </svg>
      {/snippet}
    </SearchableDropdown>
  </div>
{/snippet}

{#snippet footerContent()}
  <div class="modal-footer__start">
    <Button
      variant="tertiary"
      class="terminal-launcher__new-group"
      onClick={newGroup}
    >
      {t('terminals.newGroup')}
    </Button>
  </div>
  <Button
    variant="primary"
    class="terminal-launcher__start"
    loading={viewState.startingTerminal}
    disabled={computerUnavailable}
    aria-keyshortcuts="Enter"
    onClick={() => void startActive()}
  >
    {t('terminals.start')}
    <kbd class="terminal-launcher__key" aria-hidden="true"
      >{t('terminals.enterKey')}</kbd
    >
  </Button>
{/snippet}

{#if sheet}
  <Modal
    title={t('terminals.startTitle')}
    class="terminal-launcher-sheet"
    closeDisabled={viewState.startingTerminal}
    onClose={() => close()}
  >
    {#snippet body()}
      <div class="modal-body terminal-launcher-sheet__body">
        {@render commandRow()}
        {@render setupList()}
        {@render optionsRow()}
      </div>
    {/snippet}
    {#snippet footer()}
      {@render footerContent()}
    {/snippet}
  </Modal>
{:else}
  <div
    use:portal
    bind:this={panelElement}
    class="terminal-launcher"
    role="dialog"
    tabindex="-1"
    aria-label={t('terminals.startTitle')}
    style={`${panelStyle}; visibility: ${placed ? 'visible' : 'hidden'}`}
    onkeydown={handlePanelKeydown}
  >
    {@render commandRow()}
    {@render setupList()}
    {@render optionsRow()}
    <div class="terminal-launcher__footer">
      {@render footerContent()}
    </div>
  </div>
{/if}
