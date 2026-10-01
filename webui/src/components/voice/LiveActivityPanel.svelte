<script>
  // What was said in the Live call and what the operator did, with links to
  // the Sessions and Terminals it named. It stays readable after the call
  // ends, until the next call starts.
  import { onMount, tick } from 'svelte';
  import { t, tOr } from '$lib/i18n.js';

  // Long result texts fold behind a toggle; the first line always shows.
  const RESULT_PREVIEW_CHARS = 160;
  const PANEL_WIDTH = 380;
  const PANEL_GAP = 8;

  let {
    // The Live voice state (`createLiveVoiceState`).
    voice,
    // The button that opened the panel; the panel sits next to it.
    anchor = null,
    // App UI actions: `open(target, guard)` and `terminalView(target, guard)`.
    uiActions = {},
    onClose = () => {},
    // Called when a link could not be shown.
    onLinkFailed = () => {},
  } = $props();

  let panel = $state();
  let feed = $state();
  let placement = $state('');

  const entries = $derived(
    [
      ...voice.captions.map((caption) => ({ kind: 'caption', ...caption })),
      ...voice.actions.map((action) => ({ kind: 'action', ...action })),
    ].sort((a, b) => a.seq - b.seq),
  );

  // The values the Model passed, as one short line.
  function argumentLine(args) {
    const parts = Object.values(args ?? {}).filter(
      (value) =>
        (typeof value === 'string' && value.trim()) ||
        (typeof value === 'number' && Number.isFinite(value)),
    );
    return parts.map(String).join(' · ');
  }

  function resultPreview(text) {
    const firstLine = text.split('\n', 1)[0];
    return firstLine.length > RESULT_PREVIEW_CHARS
      ? `${firstLine.slice(0, RESULT_PREVIEW_CHARS - 1)}…`
      : firstLine;
  }

  async function show(link) {
    const guard = { isCurrent: () => true };
    try {
      const outcome =
        link.kind === 'session'
          ? await uiActions.open?.(
              {
                view: 'chat',
                agent_id: link.agent_id,
                session_id: link.session_id,
              },
              guard,
            )
          : await uiActions.terminalView?.(
              { op: 'show', terminal_id: link.terminal_id },
              guard,
            );
      if (outcome === false) onLinkFailed();
    } catch {
      onLinkFailed();
    }
  }

  // Next to the sidebar when there is room, else above the opening button.
  function place() {
    const rect = anchor?.getBoundingClientRect?.();
    if (!rect) return;
    const width = Math.min(PANEL_WIDTH, window.innerWidth - 2 * PANEL_GAP);
    const besideFits =
      rect.right + PANEL_GAP + width <= window.innerWidth - PANEL_GAP;
    const left = besideFits
      ? rect.right + PANEL_GAP
      : Math.max(PANEL_GAP, Math.min(rect.left, window.innerWidth - width));
    const bottom = besideFits
      ? Math.max(PANEL_GAP, window.innerHeight - rect.bottom)
      : window.innerHeight - rect.top + PANEL_GAP;
    placement = `left: ${left}px; bottom: ${bottom}px; width: ${width}px; max-height: ${Math.max(
      160,
      Math.min(560, window.innerHeight - bottom - 2 * PANEL_GAP),
    )}px;`;
  }

  function handleKeydown(event) {
    if (event.key === 'Escape' && !event.defaultPrevented) {
      event.preventDefault();
      onClose();
    }
  }

  function handlePointerDown(event) {
    if (panel?.contains(event.target) || anchor?.contains?.(event.target))
      return;
    onClose();
  }

  // Follow new entries while the reader is at the end of the feed.
  let following = true;
  function handleScroll() {
    if (!feed) return;
    following = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 24;
  }
  $effect(() => {
    void entries.length;
    void entries.at(-1)?.text;
    if (!following || !feed) return;
    void tick().then(() => {
      if (feed) feed.scrollTop = feed.scrollHeight;
    });
  });

  onMount(() => {
    place();
    window.addEventListener('resize', place);
    document.addEventListener('keydown', handleKeydown);
    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => {
      window.removeEventListener('resize', place);
      document.removeEventListener('keydown', handleKeydown);
      document.removeEventListener('pointerdown', handlePointerDown, true);
    };
  });
</script>

<div
  class="live-activity"
  bind:this={panel}
  style={placement}
  role="dialog"
  aria-label={t('live.activity.title')}
>
  <header class="live-activity__header">
    <h2 class="live-activity__title">
      {voice.phase === 'off'
        ? t('live.activity.lastCall')
        : t('live.activity.title')}
    </h2>
    <button
      type="button"
      class="live-activity__close"
      aria-label={t('common.close')}
      onclick={onClose}
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M4 4l8 8M12 4l-8 8" />
      </svg>
    </button>
  </header>
  <ol class="live-activity__feed" bind:this={feed} onscroll={handleScroll}>
    {#each entries as entry (`${entry.kind}-${entry.seq}`)}
      {#if entry.kind === 'caption'}
        <li
          class="live-activity__caption"
          data-role={entry.role}
          data-final={entry.final}
        >
          <span class="live-activity__speaker"
            >{entry.role === 'user'
              ? t('live.activity.you')
              : t('live.activity.assistant')}</span
          >
          <span class="live-activity__text">{entry.text}</span>
        </li>
      {:else}
        <li class="live-activity__action" data-ok={entry.ok}>
          <div class="live-activity__action-head">
            <span class="live-activity__tool"
              >{tOr(`live.tool.${entry.tool}`, entry.tool)}</span
            >
            {#if !entry.ok}
              <span class="live-activity__failed"
                >{t('live.activity.failed')}</span
              >
            {/if}
          </div>
          {#if argumentLine(entry.arguments)}
            <div class="live-activity__arguments">
              {argumentLine(entry.arguments)}
            </div>
          {/if}
          {#if entry.result}
            {#if entry.result.length > RESULT_PREVIEW_CHARS || entry.result.includes('\n')}
              <details class="live-activity__result">
                <summary>{resultPreview(entry.result)}</summary>
                <pre>{entry.result}</pre>
              </details>
            {:else}
              <div class="live-activity__result">{entry.result}</div>
            {/if}
          {/if}
          {#if entry.links.length}
            <div class="live-activity__links">
              {#each entry.links as link (link.ref)}
                <button
                  type="button"
                  class="live-activity__link"
                  onclick={() => show(link)}
                >
                  <span class="live-activity__ref">{link.ref}</span>
                  {link.label}
                </button>
              {/each}
            </div>
          {/if}
        </li>
      {/if}
    {:else}
      <li class="live-activity__empty">{t('live.activity.empty')}</li>
    {/each}
  </ol>
</div>

<style>
  .live-activity {
    position: fixed;
    z-index: var(--z-floating);
    display: flex;
    flex-direction: column;
    min-height: 0;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: var(--dropdown-elevation);
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .live-activity__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 8px 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  .live-activity__title {
    margin: 0;
    color: var(--text-hi);
    font-size: var(--fs-label-md);
    font-weight: 600;
  }
  .live-activity__close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-lo);
    cursor: pointer;
  }
  .live-activity__close svg {
    width: 12px;
    height: 12px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.5;
  }
  .live-activity__close:hover {
    color: var(--text-hi);
  }
  .live-activity__feed {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 0;
    padding: 10px 12px 12px;
    overflow-y: auto;
    list-style: none;
  }
  .live-activity__caption {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .live-activity__speaker {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
  .live-activity__caption[data-role='user'] .live-activity__text {
    color: var(--text-hi);
  }
  .live-activity__caption[data-final='false'] .live-activity__text {
    opacity: 0.7;
  }
  .live-activity__text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .live-activity__action {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px 10px;
    border-left: 2px solid var(--accent-38);
    border-radius: var(--r-sm);
    background: var(--surface-3);
  }
  .live-activity__action[data-ok='false'] {
    border-left-color: var(--red);
  }
  .live-activity__action-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .live-activity__tool {
    color: var(--text-hi);
    font-size: var(--fs-label-md);
    font-weight: 600;
  }
  .live-activity__failed {
    color: var(--red);
    font-size: var(--fs-label-sm);
  }
  .live-activity__arguments {
    display: -webkit-box;
    overflow: hidden;
    color: var(--text-med);
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }
  .live-activity__result {
    color: var(--text-lo);
    overflow-wrap: anywhere;
  }
  .live-activity__result summary {
    cursor: pointer;
  }
  .live-activity__result pre {
    max-height: 240px;
    margin: 6px 0 0;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    white-space: pre-wrap;
  }
  .live-activity__links {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 2px;
  }
  .live-activity__link {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 100%;
    padding: 2px 8px;
    overflow: hidden;
    border: 1px solid var(--border-2);
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    font: inherit;
    font-size: var(--fs-label-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: pointer;
  }
  .live-activity__link:hover {
    border-color: var(--accent-40);
    color: var(--text-hi);
  }
  .live-activity__ref {
    color: var(--accent);
    font-family: var(--font-mono);
  }
  .live-activity__close:focus-visible,
  .live-activity__link:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 1px;
  }
  .live-activity__empty {
    color: var(--text-lo);
  }
</style>
