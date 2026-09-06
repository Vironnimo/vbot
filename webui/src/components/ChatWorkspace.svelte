<script>
  import { onMount, tick, untrack } from 'svelte';
  import { t } from '$lib/i18n.js';
  import ChatView from './ChatView.svelte';
  import HtmlPreview from './chat/HtmlPreview.svelte';
  import Button from './ui/Button.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';

  let { active = true, ...chatProps } = $props();
  const id = $props.id();
  const paneIds = [0, 1];
  let root = $state(null);
  let split = $state(false);
  let secondCreated = $state(false);
  let secondChatCreated = $state(false);
  let singlePane = $state(0);
  let focusedPane = $state(0);
  let kinds = $state(['chat', 'chat']);
  let previews = $state([null, null]);
  let sessions = $state([null, null]);
  let sameSession = $derived(
    Boolean(sessions[0]?.sessionId) &&
      sessions[0]?.agentId === sessions[1]?.agentId &&
      sessions[0]?.sessionId === sessions[1]?.sessionId,
  );
  let editorPane = $derived.by(() => {
    if (!split) return singlePane;
    return kinds[0] === 'chat' ? 0 : 1;
  });
  let secondAgent = $state('');
  let secondProject = $state('');
  let secondProjectAgent = $state(null);
  let secondNavigation = $state(null);
  let navigationSequence = 0;
  let ratio = $state(50);
  let width = $state(0);
  let dragging = $state(false);
  let stacked = $derived(width > 0 && width < 760);
  let minimum = $derived(
    stacked || !width ? 25 : Math.min(45, (340 / (width - 9)) * 100),
  );
  let effectiveRatio = $derived(
    Math.max(minimum, Math.min(100 - minimum, ratio)),
  );
  let lastNavigation = null;
  let menuPane = $state(null);
  let menuTrigger = null;
  let menu = $state(null);
  let menuStyle = $state('visibility: hidden');

  function closeMenu(restoreFocus = false) {
    menuPane = null;
    if (restoreFocus) menuTrigger?.focus();
  }

  async function toggleMenu(index, trigger) {
    if (menuPane === index) {
      closeMenu(true);
      return;
    }
    menuTrigger = trigger;
    menuPane = index;
    menuStyle = 'visibility: hidden';
    await tick();
    if (menuPane !== index || !menu) return;
    const position = computePanelPosition(trigger, {
      panelWidth: 200,
      contentHeight: menu.scrollHeight,
    });
    menuStyle = `left: ${position.left}px; ${position.verticalRule}; width: ${position.width}px; max-height: ${position.optionsMaxHeight}px`;
    menu.querySelector('button')?.focus();
  }

  function menuKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key === 'Tab') {
      closeMenu(true);
      return;
    }
    const items = [...menu.querySelectorAll('button')];
    const current = items.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (current + 1) % items.length;
    else if (event.key === 'ArrowUp')
      next = (current - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else return;
    event.preventDefault();
    items[next]?.focus();
  }

  function changeContent(index, kind) {
    closeMenu();
    kinds[index] = kind;
    if (index === 1 && kind === 'chat') secondChatCreated = true;
    focusArea(index);
  }

  $effect(() => {
    if (!active) closeMenu();
  });

  onMount(() => {
    try {
      const stored = Number(localStorage.getItem('vbot.chat.splitRatio') || 50);
      if (Number.isFinite(stored)) ratio = Math.max(25, Math.min(75, stored));
    } catch {
      /* Storage is optional; the live layout remains usable. */
    }
    if (!root || typeof ResizeObserver !== 'function') return;
    width = root.clientWidth;
    const observer = new ResizeObserver(() => {
      width = root.clientWidth;
    });
    observer.observe(root);
    return () => observer.disconnect();
  });

  function saveRatio() {
    try {
      localStorage.setItem('vbot.chat.splitRatio', String(ratio));
    } catch {
      /* Storage is optional. */
    }
  }

  function createSecond() {
    if (secondCreated) return;
    secondAgent = chatProps.sharedSelectedAgentId || '';
    secondProject = chatProps.selectedProjectId || '';
    secondProjectAgent = chatProps.sharedSelectedProjectAgentId ?? null;
    secondCreated = true;
  }

  function openSplit() {
    closeMenu();
    createSecond();
    if (kinds[1] === 'chat') secondChatCreated = true;
    split = true;
    focusedPane = 1 - singlePane;
    focusArea(focusedPane);
  }

  function closePane(index) {
    closeMenu();
    split = false;
    dragging = false;
    singlePane = 1 - index;
    focusedPane = singlePane;
    focusArea(singlePane);
    // Owners stay mounted: closing the area must not discard a composer or Run.
  }

  function focusArea(index) {
    void tick().then(() => {
      const area = root?.querySelectorAll('.chat-workspace__pane')[index];
      const surface = area?.querySelector(
        '.chat-workspace__body:not([hidden])',
      );
      surface?.querySelector('[data-workspace-action]')?.focus();
    });
  }

  function openPreview(index, source) {
    const target = 1 - index;
    createSecond();
    split = true;
    kinds[target] = 'preview';
    previews[target] = { source };
  }

  function interceptFiles(node, index) {
    const handleClick = (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const anchor = event.target.closest?.('.msg-markdown a');
      if (!anchor || !/\.html?$/i.test(anchor.textContent.trim())) return;
      const href = anchor.getAttribute('href') || '';
      if (!/^\/api\/files\/[A-Za-z0-9_.-]+$/.test(href)) return;
      event.preventDefault();
      openPreview(index, href);
    };
    node.addEventListener('click', handleClick);
    return { destroy: () => node.removeEventListener('click', handleClick) };
  }

  function moveDivider(event) {
    if (!dragging || !root) return;
    const bounds = root.getBoundingClientRect();
    const position = stacked
      ? event.clientY - bounds.top
      : event.clientX - bounds.left;
    const length = stacked ? bounds.height : bounds.width;
    if (length > 0)
      ratio = Math.max(
        minimum,
        Math.min(100 - minimum, (position / length) * 100),
      );
  }

  function startDrag(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragging = true;
    moveDivider(event);
  }

  function stopDrag() {
    dragging = false;
    saveRatio();
  }

  function resizeWithKeyboard(event) {
    const delta = event.shiftKey ? 10 : 2;
    let next;
    if (event.key === (stacked ? 'ArrowUp' : 'ArrowLeft'))
      next = effectiveRatio - delta;
    else if (event.key === (stacked ? 'ArrowDown' : 'ArrowRight'))
      next = effectiveRatio + delta;
    else if (event.key === 'Home') next = minimum;
    else if (event.key === 'End') next = 100 - minimum;
    else if (event.key === 'Enter') next = 50;
    else return;
    event.preventDefault();
    ratio = Math.max(minimum, Math.min(100 - minimum, next));
    saveRatio();
  }

  $effect(() => {
    const navigation = chatProps.pendingSessionNavigation;
    if (!navigation || navigation === lastNavigation) return;
    lastNavigation = navigation;
    untrack(() => {
      singlePane = 0;
      kinds[0] = 'chat';
      focusedPane = 0;
    });
  });

  function navigateSecond(agentId, sessionId) {
    secondNavigation = {
      agentId,
      sessionId,
      subAgent: true,
      requestId: ++navigationSequence,
    };
  }
</script>

<svelte:window onresize={() => closeMenu()} />
<svelte:document
  onpointerdown={(event) => {
    if (
      menuPane !== null &&
      !menu?.contains(event.target) &&
      !menuTrigger?.contains(event.target)
    )
      closeMenu();
  }}
/>

{#snippet areaActions(index, inChat = true)}
  <Button
    variant={inChat ? 'secondary' : 'tertiary'}
    icon
    class={inChat ? 'chat-view__workspace-action' : ''}
    data-workspace-action
    ariaLabel={t('split.actions', 'Area actions')}
    tooltip={t('split.actions', 'Area actions')}
    aria-haspopup="menu"
    aria-expanded={menuPane === index}
    aria-controls={menuPane === index ? `${id}-actions` : undefined}
    onClick={(event) => toggleMenu(index, event.currentTarget)}
    onkeydown={(event) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        void toggleMenu(index, event.currentTarget);
      }
    }}
  >
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M12 4v16" />
    </svg>
  </Button>
{/snippet}

{#if menuPane !== null}
  <div
    bind:this={menu}
    use:portal
    class="chat-workspace__menu"
    id={`${id}-actions`}
    role="menu"
    tabindex="-1"
    aria-label={t('split.actions', 'Area actions')}
    style={menuStyle}
    onkeydown={menuKeydown}
  >
    {#if !split}
      <Button variant="tertiary" role="menuitem" onClick={openSplit}
        >{t('split.open', 'Split view')}</Button
      >
    {/if}
    {#if kinds[menuPane] === 'preview' || previews[menuPane]}
      <Button
        variant="tertiary"
        role="menuitem"
        onClick={() =>
          changeContent(
            menuPane,
            kinds[menuPane] === 'chat' ? 'preview' : 'chat',
          )}
      >
        {kinds[menuPane] === 'chat'
          ? t('split.showPreview', 'Show preview')
          : t('split.backToChat', 'Back to chat')}
      </Button>
    {/if}
    {#if split}
      <Button
        variant="tertiary"
        role="menuitem"
        onClick={() => closePane(menuPane)}
        >{t('split.close', 'Close area')}</Button
      >
    {/if}
  </div>
{/if}

<div
  bind:this={root}
  class="chat-workspace"
  class:split
  class:stacked
  class:dragging
  hidden={!active}
  style:--pane-ratio={`${effectiveRatio}%`}
>
  {#each paneIds as index (index)}
    {#snippet chatActions()}{@render areaActions(index)}{/snippet}
    {#snippet previewActions()}{@render areaActions(index, false)}{/snippet}
    {#if index === 0 || secondCreated}
      {#if index === 1 && split}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
        <div
          class="chat-workspace__divider"
          role="separator"
          tabindex="0"
          aria-label={t('split.resize', 'Resize chat areas')}
          aria-orientation={stacked ? 'horizontal' : 'vertical'}
          aria-valuemin={Math.round(minimum)}
          aria-valuemax={Math.round(100 - minimum)}
          aria-valuenow={Math.round(effectiveRatio)}
          aria-controls={`${id}-pane-0 ${id}-pane-1`}
          use:tooltip={t(
            'split.resizeHint',
            'Drag to resize. Arrow keys adjust; Enter resets.',
          )}
          onpointerdown={startDrag}
          onpointermove={moveDivider}
          onpointerup={stopDrag}
          onpointercancel={stopDrag}
          onlostpointercapture={stopDrag}
          onkeydown={resizeWithKeyboard}
          ondblclick={() => {
            ratio = 50;
            saveRatio();
          }}
        >
          <span></span>
        </div>
      {/if}
      <section
        class="chat-workspace__pane"
        id={`${id}-pane-${index}`}
        hidden={!split && singlePane !== index}
        aria-label={index === 0
          ? t('split.firstArea', 'First area')
          : t('split.secondArea', 'Second area')}
        onfocusin={() => (focusedPane = index)}
        onpointerdowncapture={() => (focusedPane = index)}
        use:interceptFiles={index}
      >
        <div class="chat-workspace__body" hidden={kinds[index] !== 'chat'}>
          {#if index === 0}
            <ChatView
              {...chatProps}
              workspaceActions={chatActions}
              active={active &&
                kinds[index] === 'chat' &&
                (split || singlePane === index)}
              interactive={focusedPane === index}
              composerAvailable={!sameSession || editorPane === index}
              preserveSessionSelection
              onDisplayedSession={(session) => (sessions[index] = session)}
            />
          {:else if secondChatCreated}
            <ChatView
              {...chatProps}
              workspaceActions={chatActions}
              active={active &&
                kinds[index] === 'chat' &&
                (split || singlePane === index)}
              interactive={focusedPane === index}
              composerAvailable={!sameSession || editorPane === index}
              preserveSessionSelection
              initialSessionDrawer
              sharedSelectedAgentId={secondAgent}
              selectedProjectId={secondProject}
              sharedSelectedProjectAgentId={secondProjectAgent}
              onAgentSelected={(value) => (secondAgent = value)}
              onProjectSelected={(value) => (secondProject = value)}
              onProjectAgentSelected={(value) => (secondProjectAgent = value)}
              pendingSessionNavigation={secondNavigation}
              navigateToSubAgent={navigateSecond}
              onSessionNavigation={() => {}}
              onDisplayedSession={(session) => (sessions[index] = session)}
            />
          {/if}
        </div>
        <div class="chat-workspace__body" hidden={kinds[index] !== 'preview'}>
          <HtmlPreview
            active={active &&
              kinds[index] === 'preview' &&
              (split || singlePane === index)}
            workspaceActions={previewActions}
            request={previews[index]}
          />
        </div>
      </section>
    {/if}
  {/each}
</div>

<style>
  .chat-workspace {
    display: flex;
    flex: 1;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
    background: var(--bg);
  }
  .chat-workspace[hidden],
  .chat-workspace__pane[hidden],
  .chat-workspace__body[hidden] {
    display: none;
  }
  .chat-workspace__pane {
    display: flex;
    flex: 1;
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    overflow: hidden;
    container-type: inline-size;
  }
  .split .chat-workspace__pane:first-child {
    flex: 0 0 calc(var(--pane-ratio) - 4.5px);
  }
  .chat-workspace__menu {
    position: fixed;
    z-index: var(--z-floating);
    display: flex;
    flex-direction: column;
    padding: 4px;
    overflow-y: auto;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: var(--dropdown-elevation);
  }
  .chat-workspace__menu :global(button) {
    justify-content: flex-start;
    min-height: 36px;
    padding: 8px 12px;
    font-family: var(--font-ui);
    text-transform: none;
  }
  .chat-workspace__body {
    display: flex;
    flex: 1;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  .chat-workspace__body :global(.chat-view) {
    width: 100%;
    min-width: 0;
  }
  .chat-workspace__divider {
    position: relative;
    z-index: 5;
    flex: 0 0 9px;
    cursor: col-resize;
    touch-action: none;
    background: var(--secondary-surface);
    border-inline: 1px solid var(--border);
    outline-offset: -2px;
  }
  .chat-workspace__divider span {
    position: absolute;
    width: 3px;
    height: 32px;
    left: 2px;
    top: calc(50% - 16px);
    border-radius: 2px;
    background: var(--border-2);
  }
  .chat-workspace__divider:hover span,
  .chat-workspace__divider:focus-visible span,
  .dragging .chat-workspace__divider span {
    background: var(--accent);
  }
  .dragging {
    user-select: none;
  }
  .dragging :global(iframe) {
    pointer-events: none;
  }
  .stacked.split {
    flex-direction: column;
  }
  .stacked .chat-workspace__divider {
    cursor: row-resize;
    border-inline: 0;
    border-block: 1px solid var(--border);
  }
  .stacked .chat-workspace__divider span {
    height: 3px;
    width: 32px;
    top: 2px;
    left: calc(50% - 16px);
  }
  @container (max-width: 560px) {
    .chat-workspace__body :global(.chat-header) {
      padding-inline: 10px;
      gap: 4px;
    }
    .chat-workspace__body :global(.agent-tab) {
      padding-inline: 8px;
    }
    .chat-workspace__body :global(.chat-header__project-dropdown) {
      max-width: 145px;
    }
    .chat-workspace__body :global(.chat-activity-panel) {
      max-width: calc(100% - 24px);
    }
  }
</style>
