<script>
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import {
    terminalDetails,
    terminalError,
    terminalLabel,
    terminalTitle,
  } from './terminalLabels.js';
  import {
    TERMINAL_STREAM_CONNECTED,
    TERMINAL_STREAM_IDLE,
    terminalIsFinished,
  } from '$lib/terminalsView.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  let {
    item,
    span,
    viewState,
    controller,
    renderer,
    maximizedTerminalId,
    serverUnavailable,
    draggable,
    draggedId,
    dropTargetId,
    onActivate,
    onMaximize,
    onClose,
    onMenu,
    onDragStart,
    onDragOver,
    onDragEnd,
    onDrop,
  } = $props();

  const stream = $derived(
    viewState.streams[item.terminal_id] ?? {
      status: TERMINAL_STREAM_IDLE,
      error: '',
      errorCode: '',
    },
  );
  const isMaximized = $derived(maximizedTerminalId === item.terminal_id);
  const isFinished = $derived(terminalIsFinished(item));
  const isFocused = $derived(item.terminal_id === viewState.selectedTerminalId);
  const hasSpeech = $derived(item.terminal_id === viewState.speechTerminalId);
  const isRecording = $derived(
    hasSpeech && viewState.speechState === 'recording',
  );
  const speechBusy = $derived(hasSpeech && !isRecording);
  const speechLabel = $derived(
    isRecording ? t('terminals.voice.stop') : t('terminals.voice.start'),
  );
  const isDragged = $derived(draggedId === item.terminal_id);
  const isDropTarget = $derived(dropTargetId === item.terminal_id);
  function activateTerminalFromPointer(event, terminalId) {
    if (event.button !== 0 || !terminalId || serverUnavailable) {
      return;
    }
    onActivate(terminalId);
  }

  function activateTerminalFromKeyboard(event, terminalId) {
    if (
      !terminalId ||
      serverUnavailable ||
      event.target !== event.currentTarget
    ) {
      return;
    }
    if (event.key !== 'Enter' && event.key !== ' ') {
      return;
    }
    event.preventDefault();
    onActivate(terminalId);
  }
</script>

<div
  class="terminals-view__tile"
  class:terminals-view__tile--hidden={maximizedTerminalId && !isMaximized}
  class:terminals-view__tile--maximized={isMaximized}
  class:terminals-view__tile--focused={isFocused && !maximizedTerminalId}
  class:terminals-view__tile--finished={isFinished}
  class:terminals-view__tile--dragging={isDragged}
  class:terminals-view__tile--drop-target={isDropTarget}
  data-terminal-id={item.terminal_id}
  style="grid-row: {span.row +
    1} / span {span.rowSpan}; grid-column: {span.column +
    1} / span {span.columnSpan};"
>
  <div
    class="terminals-view__tile-bar"
    class:terminals-view__tile-bar--busy={hasSpeech}
    role="button"
    tabindex="0"
    aria-label={terminalTitle(item)}
    {draggable}
    ondragstart={(event) => onDragStart(event, item.terminal_id)}
    ondragover={(event) => onDragOver(event, item.terminal_id)}
    use:tooltip={() => terminalDetails(item)}
    ondragend={onDragEnd}
    ondrop={(event) => onDrop(event, item.terminal_id)}
    onclick={() => onActivate(item.terminal_id)}
    ondblclick={() => onMaximize(item.terminal_id)}
    oncontextmenu={(event) => onMenu(event, item)}
    onkeydown={(event) => {
      if (isContextMenuKey(event)) {
        onMenu(event, item);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onActivate(item.terminal_id);
      } else if (event.key === 'F2') {
        event.preventDefault();
        onMaximize(item.terminal_id);
      }
    }}
  >
    <span
      class="terminals-view__tile-dot tab-indicator"
      class:tab-indicator--running={item.state === 'working'}
      aria-hidden="true"
    ></span>
    {#if item.call_name}
      <span class="terminals-view__tile-call">{item.call_name}</span>
    {/if}
    <span class="terminals-view__tile-label">{terminalLabel(item)}</span>
    {#if item.pinned}
      <span
        class="terminals-view__tile-pin"
        use:tooltip={t('terminals.pinnedHint')}
      >
        <svg
          viewBox="0 0 16 16"
          width="12"
          height="12"
          aria-label={t('terminals.pinnedHint')}
        >
          <path
            d="M6 2.5h4l-.6 4 2.1 2H4.5l2.1-2zM8 8.5v5"
            class="terminals-view__stroke"
          />
        </svg>
      </span>
    {/if}
    {#if isFinished}
      <span class="terminals-view__tile-state">
        {Number.isInteger(item.exit_code)
          ? t('terminals.state.exitedWithCode', {
              code: item.exit_code,
            })
          : t('terminals.state.exited')}
      </span>
    {/if}
    {#if renderer.scrolledBack(item.terminal_id)}
      <button
        type="button"
        class="terminals-view__latest-action"
        onclick={(event) => {
          event.stopPropagation();
          renderer.scrollToLatest(item.terminal_id);
        }}
      >
        {t('terminals.scrollLatest')}
      </button>
    {/if}
    <span
      class="terminals-view__tile-actions"
      role="presentation"
      onclick={(event) => event.stopPropagation()}
      ondblclick={(event) => event.stopPropagation()}
      onkeydown={(event) => event.stopPropagation()}
    >
      {#if !isFinished}
        {#if hasSpeech}
          <span class="terminals-view__speech-status" role="status">
            {viewState.speechState === 'requesting'
              ? t('terminals.voice.requesting')
              : isRecording
                ? t('voice.state.recording')
                : t('voice.state.transcribing')}
          </span>
        {/if}
        <Button
          variant="tertiary"
          icon
          class={`terminals-view__tile-action ${isRecording ? 'btn-icon--active' : ''}`}
          ariaLabel={speechLabel}
          tooltip={speechLabel}
          aria-pressed={isRecording}
          loading={speechBusy}
          disabled={serverUnavailable ||
            stream.status !== TERMINAL_STREAM_CONNECTED ||
            (!!viewState.speechTerminalId && !hasSpeech) ||
            !renderer.hasRenderer(item.terminal_id)}
          onClick={() => void controller.toggleSpeech(item.terminal_id)}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="M8 2a2 2 0 0 1 2 2v4a2 2 0 1 1-4 0V4a2 2 0 0 1 2-2z" />
            <path d="M4 7v1a4 4 0 0 0 8 0V7M8 12v2M6 14h4" />
          </svg>
        </Button>
        {#if hasSpeech}
          <Button
            variant="tertiary"
            icon
            class="terminals-view__tile-action"
            ariaLabel={t('terminals.voice.cancel')}
            tooltip={t('terminals.voice.cancel')}
            onClick={() => controller.cancelSpeech(item.terminal_id)}
          >
            <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
              <path d="m4 4 6 6m0-6-6 6" />
            </svg>
          </Button>
        {/if}
      {/if}
      <Button
        variant="tertiary"
        icon
        class="terminals-view__tile-action"
        ariaLabel={isMaximized
          ? t('terminals.restore')
          : t('terminals.maximize')}
        tooltip={isMaximized
          ? t('terminals.restoreHint')
          : t('terminals.maximizeHint')}
        onClick={() => onMaximize(item.terminal_id)}
      >
        {#if isMaximized}
          <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
            <path d="M5.5 5.5V2.5h6v6h-3M2.5 5.5h6v6h-6z" />
          </svg>
        {:else}
          <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
            <path d="M3 3h8v8H3z" />
          </svg>
        {/if}
      </Button>
      <Button
        variant="danger"
        icon
        class="terminals-view__tile-action"
        ariaLabel={t('terminals.close')}
        tooltip={t('terminals.close')}
        onClick={() => void onClose(item.terminal_id)}
      >
        <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
          <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" />
        </svg>
      </Button>
    </span>
  </div>
  <div class="terminals-view__tile-chrome" role="presentation">
    {#if stream.errorCode === 'gap' && !serverUnavailable}
      <Banner variant="warn" class="terminals-view__tile-feedback">
        <span>
          {t('terminals.streamGap')}
        </span>
      </Banner>
    {/if}
    {#if stream.error && !serverUnavailable}
      <Banner variant="warn" class="terminals-view__tile-feedback">
        <span>{terminalError(stream.error)}</span>
      </Banner>
    {/if}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
    <div
      use:renderer.mountTile={item.terminal_id}
      class="terminals-view__tile-host"
      role="group"
      tabindex={isFinished ? -1 : 0}
      aria-label={isFinished
        ? t('terminals.historyTerminalLabel')
        : t('terminals.liveTerminalLabel')}
      onpointerdown={(event) =>
        activateTerminalFromPointer(event, item.terminal_id)}
      onkeydown={(event) =>
        activateTerminalFromKeyboard(event, item.terminal_id)}
    ></div>
  </div>
</div>
