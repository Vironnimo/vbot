<script>
  import { onMount } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import DesktopConnectionSettings from './settings/DesktopConnectionSettings.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsCompactionPanel from './settings/SettingsCompactionPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsDefaultsPanel from './settings/SettingsDefaultsPanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsProvidersPanel from './settings/SettingsProvidersPanel.svelte';
  import SettingsRecallPanel from './settings/SettingsRecallPanel.svelte';
  import SettingsReflectionPanel from './settings/SettingsReflectionPanel.svelte';
  import SettingsSessionTitlesPanel from './settings/SettingsSessionTitlesPanel.svelte';
  import SettingsSpecializedModelsPanel from './settings/SettingsSpecializedModelsPanel.svelte';
  import SettingsSubAgentsPanel from './settings/SettingsSubAgentsPanel.svelte';
  import SettingsWebSearchPanel from './settings/SettingsWebSearchPanel.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Dropdown from './Dropdown.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { getSettings } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import { SETTINGS_LAYOUT_CLASS } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onToast = noop,
    agents = [],
    desktopCapabilities = null,
    targetPanelId = '',
    targetPanelRequestId = 0,
    onDebugEnabledChange = noop,
    onOpenSetupGuide = noop,
    modelsRefreshToken = 0,
    clientsRefreshToken = 0,
    channelsRefreshToken = 0,
    initialScrollPosition = null,
    onScrollPositionChange = noop,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    providersPanel?.handleProviderAuthCompleted(event);
  }

  export function getScrollPosition() {
    return captureScrollPosition();
  }

  // The whole Settings surface is one scrolling document. The index on the
  // left is a table of contents (click scrolls, scrolling highlights), not a
  // panel switcher — every section below is always mounted and rendered.
  // Filtering hides sections/rows via the DOM `hidden` attribute so the
  // panels' own component state is never disturbed by a search.

  // Elements the search treats as one show/hide unit inside a section. Nested
  // matches collapse into their outermost unit (a row inside a provider card
  // is covered by the card).
  const SEARCH_UNIT_SELECTOR = [
    '.s-row',
    '.s-provider-card',
    '.s-channel-card',
    '.s-ext-card',
    '.s-client-row',
    '.s-skill-directory-item',
  ].join(', ');
  // The section under the upper-third reading line is the one the user is
  // most likely reading. Click navigation and scrollspy share this exact
  // anchor so the index cannot disagree with the document position.
  const READING_LINE_RATIO = 0.32;
  // Layout can land a target a sub-pixel below the computed line. Treat that
  // browser rounding as aligned instead of selecting the previous section.
  const READING_LINE_TOLERANCE_PX = 1;
  // Ignore scroll-driven highlight updates while a click-triggered smooth
  // scroll is in flight, so intermediate sections do not flicker in the index.
  const SCROLLSPY_SUPPRESS_MS = 700;

  let groups = $derived([
    {
      id: 'connect',
      label: () => t('settings.groups.connect', 'Connect'),
      sections: [
        {
          id: 'providers',
          labelKey: 'settings.providers.title',
          labelFallback: 'Providers',
          label: () => t('settings.providers.title', 'Providers'),
          subtitle: () =>
            t(
              'settings.providers.subtitle',
              'Connected providers and their credentials.',
            ),
        },
        {
          id: 'channels',
          labelKey: 'settings.channels.title',
          labelFallback: 'Channels',
          label: () => t('settings.channels.title', 'Channels'),
          subtitle: () =>
            t(
              'settings.channels.subtitle',
              'Manage channel routing and runtime status.',
            ),
        },
        {
          id: 'extensions',
          labelKey: 'settings.extensions.title',
          labelFallback: 'Extensions',
          label: () => t('settings.extensions.title', 'Extensions'),
          subtitle: () =>
            t(
              'settings.extensions.subtitle',
              'Loaded extensions and their capabilities. Toggles take effect immediately.',
            ),
        },
      ],
    },
    {
      id: 'models',
      label: () => t('settings.groups.models', 'Models'),
      sections: [
        {
          id: 'defaults',
          labelKey: 'settings.defaults.title',
          labelFallback: 'Agent defaults',
          label: () => t('settings.defaults.title', 'Agent defaults'),
          subtitle: () =>
            t(
              'settings.defaults.subtitle',
              'Model, temperature, and thinking effort used when an agent or project leaves them unset — shown there as "Inherited: … (global default)".',
            ),
        },
        {
          id: 'specialized_models',
          labelKey: 'settings.specializedModels.title',
          labelFallback: 'Specialized Models',
          label: () =>
            t('settings.specializedModels.title', 'Specialized Models'),
          subtitle: () =>
            t(
              'settings.specializedModels.subtitle',
              'Task-specific model bindings for speech, image, and embedding tools. These bindings are independent of agent and project defaults.',
            ),
        },
      ],
    },
    {
      id: 'conversation',
      label: () => t('settings.groups.conversation', 'Conversation'),
      sections: [
        {
          id: 'compaction',
          labelKey: 'settings.compaction.title',
          labelFallback: 'Compaction',
          label: () => t('settings.compaction.title', 'Compaction'),
          subtitle: () =>
            t(
              'settings.compaction.subtitle',
              'Automatic summarizing when a conversation nears the model context limit.',
            ),
        },
        {
          id: 'session_titles',
          labelKey: 'settings.sessionTitles.title',
          labelFallback: 'Session titles',
          label: () => t('settings.sessionTitles.title', 'Session titles'),
          subtitle: () =>
            t(
              'settings.sessionTitles.subtitle',
              'Immediate local names and optional Model-generated titles for new Sessions.',
            ),
        },
        {
          id: 'recall',
          labelKey: 'settings.recall.title',
          labelFallback: 'Recall',
          label: () => t('settings.recall.title', 'Recall'),
          subtitle: () =>
            t(
              'settings.recall.subtitle',
              'How agents search past conversations.',
            ),
        },
      ],
    },
    {
      id: 'behavior',
      label: () => t('settings.groups.behavior', 'Behavior'),
      sections: [
        {
          id: 'voice',
          labelKey: 'settings.voice.title',
          labelFallback: 'Voice',
          label: () => t('settings.voice.title', 'Voice'),
          subtitle: () =>
            t(
              'settings.voice.subtitle',
              'Transcription audio and wakeword command settings.',
            ),
        },
        {
          id: 'web_search',
          labelKey: 'settings.webSearch.title',
          labelFallback: 'Web Search',
          label: () => t('settings.webSearch.title', 'Web Search'),
          subtitle: () =>
            t(
              'settings.webSearch.subtitle',
              'Provider used by the web_search tool.',
            ),
        },
        {
          id: 'subagents',
          labelKey: 'settings.subagents.title',
          labelFallback: 'Sub-Agents',
          label: () => t('settings.subagents.title', 'Sub-Agents'),
          subtitle: () =>
            t(
              'settings.subagents.subtitle',
              'Depth, fan-out, and timeout limits for spawned agent sessions.',
            ),
        },
        {
          id: 'reflection',
          labelKey: 'settings.reflection.title',
          labelFallback: 'Reflection',
          label: () => t('settings.reflection.title', 'Reflection'),
          subtitle: () =>
            t(
              'settings.reflection.subtitle',
              'Automatic background self-review that saves durable memory and skill updates from finished conversations.',
            ),
        },
      ],
    },
    {
      id: 'system',
      label: () => t('settings.groups.system', 'System'),
      sections: [
        {
          id: 'appearance',
          labelKey: 'settings.appearance.title',
          labelFallback: 'Appearance',
          label: () => t('settings.appearance.title', 'Appearance'),
          subtitle: () =>
            t(
              'settings.appearance.subtitle',
              'Language and chat reading width.',
            ),
        },
        {
          id: 'debug',
          labelKey: 'debug.settings',
          labelFallback: 'Debug',
          label: () => t('debug.settings', 'Debug'),
          subtitle: () =>
            t(
              'debug.settingsSubtitle',
              'Control debug tracing of provider requests and responses.',
            ),
        },
        {
          id: 'general',
          labelKey: 'settings.general.title',
          labelFallback: 'Server info',
          label: () => t('settings.general.title', 'Server info'),
          subtitle: () =>
            t(
              'settings.general.subtitle',
              'Server address, data directory, and connected clients.',
            ),
        },
        ...(desktopCapabilities?.serverSelection
          ? [
              {
                id: 'desktop_connection',
                labelKey: 'settings.desktop.connection.title',
                labelFallback: 'Connection',
                label: () =>
                  t('settings.desktop.connection.title', 'Connection'),
                subtitle: () =>
                  t(
                    'settings.desktop.connection.subtitle',
                    'Choose which vBot server this Desktop app connects to.',
                  ),
              },
            ]
          : []),
      ],
    },
  ]);

  let panels = $derived(groups.flatMap((group) => group.sections));
  let panelById = $derived(new Map(panels.map((panel) => [panel.id, panel])));
  let groupById = $derived(new Map(groups.map((group) => [group.id, group])));
  let mobileSectionOptions = $derived(
    groups.flatMap((group) =>
      group.sections.map((panel) => ({
        value: panel.id,
        label: panel.label(),
        secondaryLabel: group.label(),
      })),
    ),
  );

  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let providersPanel = $state(null);
  let handledTargetPanelRequestId = -1;

  let scrollContainer = $state(null);
  let documentRoot = $state(null);
  let activeSectionId = $state('providers');
  let searchQuery = $state('');
  let matchCount = $state(0);
  const hiddenSectionIds = new SvelteSet();
  let scrollSpyFrame = null;
  let scrollRestoreFrame = null;
  let scrollSpySuppressedUntil = 0;
  let searchFilterQueued = false;
  let scrollTailHeight = $state(0);
  let pinnedScrollRestore = null;

  let searchActive = $derived(searchQuery.trim().length > 0);

  onMount(() => {
    pinnedScrollRestore = normalizeScrollPosition(initialScrollPosition);
    loadSettings();

    return () => {
      if (scrollSpyFrame !== null) {
        cancelAnimationFrame(scrollSpyFrame);
        scrollSpyFrame = null;
      }
      if (scrollRestoreFrame !== null) {
        cancelAnimationFrame(scrollRestoreFrame);
        scrollRestoreFrame = null;
      }
    };
  });

  $effect(() => {
    if (!loading && !panelById.has(activeSectionId)) {
      activeSectionId = panels[0]?.id ?? 'providers';
    }
  });

  // Deep links (e.g. the agent editor's "Edit global defaults") scroll to the
  // requested section once the document is rendered.
  $effect(() => {
    if (loading) {
      return;
    }
    if (
      targetPanelId &&
      targetPanelRequestId !== handledTargetPanelRequestId &&
      panelById.has(targetPanelId)
    ) {
      handledTargetPanelRequestId = targetPanelRequestId;
      // An existing restore belongs to a normal tab return. App clears it for
      // a fresh deep link, which then deliberately wins here.
      if (!pinnedScrollRestore) {
        scrollToSection(targetPanelId);
      }
    }
  });

  // A restored position stays pinned while the independently loading panels
  // settle. Real user input releases the pin; programmatic scroll events do
  // not, so late content above the reading point cannot steal the position.
  $effect(() => {
    if (!loading && scrollContainer && documentRoot && pinnedScrollRestore) {
      queuePinnedScrollRestore();
    }
  });

  $effect(() => {
    const container = scrollContainer;
    if (!container) {
      return undefined;
    }

    const handleResize = () => {
      syncReadingGeometry();
      queuePinnedScrollRestore();
    };
    const releaseRestore = () => releasePinnedScrollRestore();
    const observer =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(handleResize)
        : null;

    syncReadingGeometry();
    observer?.observe(container);
    window.addEventListener('resize', handleResize);
    container.addEventListener('wheel', releaseRestore, { passive: true });
    container.addEventListener('touchstart', releaseRestore, {
      passive: true,
    });
    container.addEventListener('pointerdown', releaseRestore);
    container.addEventListener('keydown', releaseRestore);

    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', handleResize);
      container.removeEventListener('wheel', releaseRestore);
      container.removeEventListener('touchstart', releaseRestore);
      container.removeEventListener('pointerdown', releaseRestore);
      container.removeEventListener('keydown', releaseRestore);
    };
  });

  // Re-apply the search filter whenever the query changes or the document is
  // (re)built after loading. Runs post-flush, so the DOM is current.
  $effect(() => {
    void searchQuery;
    void loading;
    if (documentRoot) {
      applySearchFilter();
    }
  });

  // Panels load their own data after mount and re-render rows over time; a
  // childList observer re-applies an active search filter to nodes Svelte
  // recreated. Setting `hidden` is an attribute mutation, so the observer
  // never re-fires on the filter's own work.
  $effect(() => {
    if (!documentRoot) {
      return;
    }
    const observer = new MutationObserver(() => {
      if (searchQuery.trim().length > 0) {
        queueSearchFilter();
      }
      queuePinnedScrollRestore();
    });
    const layoutObserver =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(() => queuePinnedScrollRestore())
        : null;
    observer.observe(documentRoot, { childList: true, subtree: true });
    layoutObserver?.observe(documentRoot);
    return () => {
      observer.disconnect();
      layoutObserver?.disconnect();
    };
  });

  function queueSearchFilter() {
    if (searchFilterQueued) {
      return;
    }
    searchFilterQueued = true;
    queueMicrotask(() => {
      searchFilterQueued = false;
      applySearchFilter();
    });
  }

  function collectSearchUnits(sectionElement) {
    return Array.from(
      sectionElement.querySelectorAll(SEARCH_UNIT_SELECTOR),
    ).filter(
      (element) =>
        element.parentElement?.closest(SEARCH_UNIT_SELECTOR) === null,
    );
  }

  function applySearchFilter() {
    if (!documentRoot) {
      return;
    }
    const query = searchQuery.trim().toLowerCase();
    const nextHiddenIds = [];
    let visibleUnits = 0;

    for (const sectionElement of documentRoot.querySelectorAll(
      '[data-settings-section]',
    )) {
      const units = collectSearchUnits(sectionElement);
      if (!query) {
        sectionElement.hidden = false;
        for (const unit of units) {
          unit.hidden = false;
        }
        continue;
      }

      const headerText =
        sectionElement
          .querySelector('.s-section-header')
          ?.textContent.toLowerCase() ?? '';
      let sectionVisible;
      if (headerText.includes(query)) {
        // The section itself is the match — keep all of its content.
        sectionVisible = true;
        for (const unit of units) {
          unit.hidden = false;
        }
        visibleUnits += Math.max(units.length, 1);
      } else {
        let anyUnitVisible = false;
        for (const unit of units) {
          const matches = unit.textContent.toLowerCase().includes(query);
          unit.hidden = !matches;
          if (matches) {
            anyUnitVisible = true;
            visibleUnits += 1;
          }
        }
        if (!anyUnitVisible && units.length === 0) {
          // Sections without row-shaped content fall back to full-text match.
          anyUnitVisible = sectionElement.textContent
            .toLowerCase()
            .includes(query);
          if (anyUnitVisible) {
            visibleUnits += 1;
          }
        }
        sectionVisible = anyUnitVisible;
      }

      sectionElement.hidden = !sectionVisible;
      if (!sectionVisible) {
        nextHiddenIds.push(sectionElement.dataset.settingsSection);
      }
    }

    for (const labelElement of documentRoot.querySelectorAll(
      '[data-settings-group]',
    )) {
      const group = groups.find(
        (candidate) => candidate.id === labelElement.dataset.settingsGroup,
      );
      const groupHidden =
        Boolean(query) &&
        (group?.sections ?? []).every((section) =>
          nextHiddenIds.includes(section.id),
        );
      labelElement.hidden = groupHidden;
    }

    hiddenSectionIds.clear();
    for (const sectionId of nextHiddenIds) {
      hiddenSectionIds.add(sectionId);
    }
    matchCount = visibleUnits;
  }

  function handleContentScroll() {
    if (scrollSpyFrame !== null) {
      return;
    }
    scrollSpyFrame = requestAnimationFrame(() => {
      scrollSpyFrame = null;
      if (!pinnedScrollRestore) {
        rememberScrollPosition();
      }
      updateActiveSectionFromScroll();
    });
  }

  function updateActiveSectionFromScroll() {
    if (!scrollContainer || !documentRoot) {
      return;
    }
    if (Date.now() < scrollSpySuppressedUntil) {
      return;
    }
    // Fully scrolled down, the last section's top may never cross the spy
    // offset (short sections) — treat bottom-of-scroll as "last section".
    const scrolledToBottom =
      scrollContainer.scrollTop + scrollContainer.clientHeight >=
      scrollContainer.scrollHeight - 2;
    const containerTop = scrollContainer.getBoundingClientRect().top;
    const readingLine = readingLineOffset();
    let currentId = '';
    for (const sectionElement of documentRoot.querySelectorAll(
      '[data-settings-section]',
    )) {
      if (sectionElement.hidden) {
        continue;
      }
      const sectionTop =
        sectionElement.getBoundingClientRect().top - containerTop;
      if (
        !currentId ||
        scrolledToBottom ||
        sectionTop <= readingLine + READING_LINE_TOLERANCE_PX
      ) {
        currentId = sectionElement.dataset.settingsSection;
      }
    }
    if (currentId) {
      activeSectionId = currentId;
    }
  }

  function scrollToSection(panelId) {
    releasePinnedScrollRestore();
    activeSectionId = panelId;
    const sectionElement = documentRoot?.querySelector(
      `[data-settings-section="${panelId}"]`,
    );
    if (!sectionElement || !scrollContainer) {
      return;
    }

    const behavior = preferredScrollBehavior();
    scrollSpySuppressedUntil =
      Date.now() + (behavior === 'smooth' ? SCROLLSPY_SUPPRESS_MS : 0);
    const containerTop = scrollContainer.getBoundingClientRect().top;
    const sectionTop = sectionElement.getBoundingClientRect().top;
    const targetTop = Math.max(
      0,
      scrollContainer.scrollTop +
        sectionTop -
        containerTop -
        readingLineOffset(),
    );
    if (typeof scrollContainer.scrollTo === 'function') {
      scrollContainer.scrollTo({ top: targetTop, behavior });
    } else {
      scrollContainer.scrollTop = targetTop;
    }
  }

  function handleSearchInput(event) {
    releasePinnedScrollRestore();
    searchQuery = event.currentTarget.value;
  }

  function readingLineOffset() {
    return Math.max(0, scrollContainer?.clientHeight ?? 0) * READING_LINE_RATIO;
  }

  function syncReadingGeometry() {
    const height = Math.max(0, scrollContainer?.clientHeight ?? 0);
    scrollTailHeight = Math.ceil(height * (1 - READING_LINE_RATIO));
  }

  function preferredScrollBehavior() {
    if (
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ) {
      return 'auto';
    }
    return 'smooth';
  }

  function normalizeScrollPosition(position) {
    if (!position || !Number.isFinite(position.top)) {
      return null;
    }
    return {
      top: Math.max(0, position.top),
      sectionId:
        typeof position.sectionId === 'string' ? position.sectionId : '',
      sectionOffset: Number.isFinite(position.sectionOffset)
        ? position.sectionOffset
        : 0,
    };
  }

  function captureScrollPosition() {
    if (!scrollContainer) {
      return null;
    }

    const position = {
      top: Math.max(0, scrollContainer.scrollTop),
      sectionId: '',
      sectionOffset: 0,
    };
    if (!documentRoot || scrollContainer.clientHeight <= 0) {
      return position;
    }

    const containerTop = scrollContainer.getBoundingClientRect().top;
    const readingLine = readingLineOffset();
    let anchor = null;
    for (const sectionElement of documentRoot.querySelectorAll(
      '[data-settings-section]',
    )) {
      if (sectionElement.hidden) {
        continue;
      }
      const sectionTop =
        sectionElement.getBoundingClientRect().top - containerTop;
      if (!anchor || sectionTop <= readingLine + READING_LINE_TOLERANCE_PX) {
        anchor = { element: sectionElement, top: sectionTop };
      }
    }
    if (anchor) {
      position.sectionId = anchor.element.dataset.settingsSection ?? '';
      position.sectionOffset = readingLine - anchor.top;
    }
    return position;
  }

  function rememberScrollPosition() {
    const position = captureScrollPosition();
    if (position) {
      onScrollPositionChange(position);
    }
  }

  function releasePinnedScrollRestore() {
    pinnedScrollRestore = null;
    scrollSpySuppressedUntil = 0;
    if (scrollRestoreFrame !== null) {
      cancelAnimationFrame(scrollRestoreFrame);
      scrollRestoreFrame = null;
    }
  }

  function queuePinnedScrollRestore() {
    if (
      !pinnedScrollRestore ||
      loading ||
      !scrollContainer ||
      !documentRoot ||
      scrollRestoreFrame !== null
    ) {
      return;
    }
    scrollRestoreFrame = requestAnimationFrame(() => {
      scrollRestoreFrame = null;
      restorePinnedScrollPosition();
    });
  }

  function restorePinnedScrollPosition() {
    if (!pinnedScrollRestore || !scrollContainer || !documentRoot) {
      return;
    }

    let targetTop = pinnedScrollRestore.top;
    const anchor = pinnedScrollRestore.sectionId
      ? documentRoot.querySelector(
          `[data-settings-section="${pinnedScrollRestore.sectionId}"]`,
        )
      : null;
    if (anchor && scrollContainer.clientHeight > 0) {
      const containerTop = scrollContainer.getBoundingClientRect().top;
      const sectionDocumentTop =
        scrollContainer.scrollTop +
        anchor.getBoundingClientRect().top -
        containerTop;
      targetTop =
        sectionDocumentTop -
        readingLineOffset() +
        pinnedScrollRestore.sectionOffset;
      activeSectionId = pinnedScrollRestore.sectionId;
    }
    scrollContainer.scrollTop = Math.max(0, targetTop);
  }

  // The single settings error seam: a panel's `onError` funnels here. A
  // non-empty message becomes a sticky error toast (a transport/server failure
  // the user must acknowledge); the empty-string "clear" calls panels make on a
  // fresh attempt are simply ignored, since a toast is dismissed by the user,
  // not by the next keystroke. Panels already build a full sentence, so it is
  // the toast body under a generic error title.
  function reportSettingsError(message) {
    if (!message) {
      return;
    }
    onToast({
      title: t('errors.appError', 'Error'),
      message,
      variant: 'error',
    });
  }

  function applySettings(nextSettings) {
    settings = nextSettings;

    const language = nextSettings?.appearance?.language ?? 'en';
    init(language);
  }

  function commitSettings(nextSettings) {
    settings = nextSettings;
  }

  async function loadSettings() {
    loading = true;
    loadError = '';

    try {
      const nextSettings = await getSettings();
      applySettings(nextSettings);
    } catch (error) {
      loadError = `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`;
    } finally {
      loading = false;
    }
  }
</script>

{#snippet sectionHeader(panel)}
  <header class="s-section-header">
    <h2 class="s-section-title" id={`settings-section-${panel.id}`}>
      {panel.label()}
    </h2>
    <p class="s-section-sub">{panel.subtitle()}</p>
  </header>
{/snippet}

<section
  class={SETTINGS_LAYOUT_CLASS}
  aria-label={t('settings.title', 'Settings')}
>
  <nav
    class="settings-nav secondary-pane"
    aria-label={t('settings.sections', 'Settings sections')}
  >
    <div class="settings-nav-title secondary-pane__title">
      {t('settings.title', 'Settings')}
    </div>
    <div class="settings-search">
      <svg
        class="settings-search-icon"
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2.4"
        aria-hidden="true"
      >
        <circle cx="11" cy="11" r="7" />
        <path d="M20 20l-4-4" />
      </svg>
      <input
        class="settings-search-input"
        type="search"
        value={searchQuery}
        oninput={handleSearchInput}
        placeholder={t('settings.search.placeholder', 'Search settings…')}
        aria-label={t('settings.search.label', 'Search settings')}
      />
    </div>
    <div class="settings-search-count" role="status">
      {#if searchActive}
        {t('settings.search.matches', 'Matches: {count}', {
          count: matchCount,
        })}
      {/if}
    </div>

    <div class="settings-desktop-index">
      {#each groups as group (group.id)}
        <div class="settings-nav-group">{group.label()}</div>
        {#each group.sections as panel (panel.id)}
          <button
            class:snav-item--active={panel.id === activeSectionId}
            class:snav-item--dimmed={searchActive &&
              hiddenSectionIds.has(panel.id)}
            class="snav-item"
            type="button"
            aria-current={panel.id === activeSectionId ? 'true' : undefined}
            aria-label={t(panel.labelKey, panel.labelFallback)}
            onclick={() => scrollToSection(panel.id)}
          >
            {panel.label()}
          </button>
        {/each}
      {/each}
    </div>

    <div class="settings-mobile-section-picker">
      <span class="settings-mobile-section-label">
        {t('settings.sections', 'Settings sections')}
      </span>
      <Dropdown
        id="settings-mobile-section"
        value={activeSectionId}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections', 'Settings sections')}
        triggerClass="settings-mobile-section-dropdown"
        onValueChange={scrollToSection}
      />
    </div>
  </nav>

  <div
    class="settings-content"
    bind:this={scrollContainer}
    onscroll={handleContentScroll}
  >
    <div class="s-doc" bind:this={documentRoot}>
      {#if loading}
        <Banner variant="neutral">
          {t('settings.loading', 'Loading settings…')}
        </Banner>
      {:else if loadError}
        <Banner variant="error">
          <span>{loadError}</span>
          <Button variant="secondary" onClick={loadSettings}>
            {t('common.retry', 'Retry')}
          </Button>
        </Banner>
      {:else}
        {#if searchActive && matchCount === 0}
          <EmptyState
            density="compact"
            description={t(
              'settings.search.noMatches',
              'No settings match your search.',
            )}
          />
        {/if}

        <div class="s-doc-group" data-settings-group="connect">
          {groupById.get('connect').label()}
        </div>

        <section
          class="s-section"
          data-settings-section="providers"
          aria-labelledby="settings-section-providers"
        >
          {@render sectionHeader(panelById.get('providers'))}
          <SettingsProvidersPanel
            bind:this={providersPanel}
            {settings}
            visible={true}
            {providerAuthEvent}
            {connectProvider}
            {disconnectProvider}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            onReloadSettings={loadSettings}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="channels"
          aria-labelledby="settings-section-channels"
        >
          {@render sectionHeader(panelById.get('channels'))}
          <SettingsChannelsPanel
            {onToast}
            {channelsRefreshToken}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="extensions"
          aria-labelledby="settings-section-extensions"
        >
          {@render sectionHeader(panelById.get('extensions'))}
          <SettingsExtensionsPanel
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <div class="s-doc-group" data-settings-group="models">
          {groupById.get('models').label()}
        </div>

        <section
          class="s-section"
          data-settings-section="defaults"
          aria-labelledby="settings-section-defaults"
        >
          {@render sectionHeader(panelById.get('defaults'))}
          <SettingsDefaultsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="specialized_models"
          aria-labelledby="settings-section-specialized_models"
        >
          {@render sectionHeader(panelById.get('specialized_models'))}
          <SettingsSpecializedModelsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <div class="s-doc-group" data-settings-group="conversation">
          {groupById.get('conversation').label()}
        </div>

        <section
          class="s-section"
          data-settings-section="compaction"
          aria-labelledby="settings-section-compaction"
        >
          {@render sectionHeader(panelById.get('compaction'))}
          <SettingsCompactionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="session_titles"
          aria-labelledby="settings-section-session_titles"
        >
          {@render sectionHeader(panelById.get('session_titles'))}
          <SettingsSessionTitlesPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="recall"
          aria-labelledby="settings-section-recall"
        >
          {@render sectionHeader(panelById.get('recall'))}
          <SettingsRecallPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <div class="s-doc-group" data-settings-group="behavior">
          {groupById.get('behavior').label()}
        </div>

        <section
          class="s-section"
          data-settings-section="voice"
          aria-labelledby="settings-section-voice"
        >
          {@render sectionHeader(panelById.get('voice'))}
          <WakewordVoiceSettings
            {agents}
            {settings}
            wakewordAvailable={desktopCapabilities?.wakeword === true}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="web_search"
          aria-labelledby="settings-section-web_search"
        >
          {@render sectionHeader(panelById.get('web_search'))}
          <SettingsWebSearchPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="subagents"
          aria-labelledby="settings-section-subagents"
        >
          {@render sectionHeader(panelById.get('subagents'))}
          <SettingsSubAgentsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="reflection"
          aria-labelledby="settings-section-reflection"
        >
          {@render sectionHeader(panelById.get('reflection'))}
          <SettingsReflectionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <div class="s-doc-group" data-settings-group="system">
          {groupById.get('system').label()}
        </div>

        <section
          class="s-section"
          data-settings-section="appearance"
          aria-labelledby="settings-section-appearance"
        >
          {@render sectionHeader(panelById.get('appearance'))}
          <SettingsAppearancePanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="debug"
          aria-labelledby="settings-section-debug"
        >
          {@render sectionHeader(panelById.get('debug'))}
          <SettingsDebugPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {onDebugEnabledChange}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="general"
          aria-labelledby="settings-section-general"
        >
          {@render sectionHeader(panelById.get('general'))}
          <SettingsGeneralPanel
            {settings}
            {clientsRefreshToken}
            {onOpenSetupGuide}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        {#if desktopCapabilities?.serverSelection}
          <section
            class="s-section"
            data-settings-section="desktop_connection"
            aria-labelledby="settings-section-desktop_connection"
          >
            {@render sectionHeader(panelById.get('desktop_connection'))}
            <DesktopConnectionSettings {onToast} />
          </section>
        {/if}

        <div
          class="settings-scroll-tail"
          style={`height: ${scrollTailHeight}px`}
          aria-hidden="true"
        ></div>
      {/if}
    </div>
  </div>
</section>
