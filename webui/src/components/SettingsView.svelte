<script>
  import { onMount, tick, untrack } from 'svelte';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import TranscriptionAudioSettings from './voice/TranscriptionAudioSettings.svelte';
  import ArchiveEntriesPanel from './archive/ArchiveEntriesPanel.svelte';
  import DesktopConnectionSettings from './settings/DesktopConnectionSettings.svelte';
  import DesktopLiveVoiceShortcut from './settings/DesktopLiveVoiceShortcut.svelte';
  import SettingsActivityPanel from './settings/SettingsActivityPanel.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsArchivePanel from './settings/SettingsArchivePanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsLibrarianPanel from './settings/SettingsLibrarianPanel.svelte';
  import SettingsNotificationsPanel from './settings/SettingsNotificationsPanel.svelte';
  import SettingsProvidersPanel from './settings/SettingsProvidersPanel.svelte';
  import SettingsRecallPanel from './settings/SettingsRecallPanel.svelte';
  import SettingsReflectionPanel from './settings/SettingsReflectionPanel.svelte';
  import SettingsSessionTitlesPanel from './settings/SettingsSessionTitlesPanel.svelte';
  import SettingsSpecializedModelsPanel from './settings/SettingsSpecializedModelsPanel.svelte';
  import SettingsSubAgentsPanel from './settings/SettingsSubAgentsPanel.svelte';
  import SettingsWebFetchPanel from './settings/SettingsWebFetchPanel.svelte';
  import SettingsWebSearchPanel from './settings/SettingsWebSearchPanel.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Dropdown from './Dropdown.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { getSettings } from '$lib/api.js';
  import { setApplicationTimeZone } from '$lib/dateTimePrefs.svelte.js';
  import { applyAppearanceSettings } from '$lib/appearancePrefs.svelte.js';
  import { applyArchiveSettings } from '$lib/archiveRetention.svelte.js';
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import {
    expandSettingsEntry,
    focusSettingsControl,
    indexSettings,
    matchSettings,
    normalizeSearchText,
    searchTerms,
    settingsEntryAnchor,
  } from '$lib/settingsSearch.js';
  import { SETTINGS_LAYOUT_CLASS } from '$lib/settingsView.js';

  const noop = () => {};
  // Shared Agent defaults live in Agents; these words lead there.
  const AGENT_DEFAULTS_TERMS = normalizeSearchText(
    'Agent defaults global shared Model Thinking effort advanced sampling temperature top_p fallback compaction',
  );
  const AGENT_DEFAULTS_RESULT = { key: 'agent_defaults', kind: 'shortcut' };
  // How long an opened search result stays highlighted.
  const SEARCH_HIT_MS = 1600;

  let {
    // The place is `[page]` or `[page, section]`, or `[page, subPage]` on a
    // page with sub-pages (an Archive entry: `['archive', entryId]`); an App
    // deep link may name a page or a section id alone. An empty place shows
    // the General page.
    navigation = createStandaloneNavigation(),
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onToast = noop,
    onSettingsCommit = noop,
    onNavigateToAgentDefaults = noop,
    agents = [],
    projects = [],
    desktopCapabilities = null,
    // The app-level Desktop Voice owner (see app/desktop.svelte.js).
    desktopVoice = null,
    onDebugEnabledChange = noop,
    onOpenSetupGuide = noop,
    modelsRefreshToken = 0,
    // The latest pushed Recall index status, applied by the Conversation
    // search panel.
    recallIndexStatus = null,
    // The server's running downloads, installations and indexing passes,
    // kept current by its pushes; General shows them at the top.
    backgroundActivity = [],
    clientsRefreshToken = 0,
    channelsRefreshToken = 0,
    archiveRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
    sessionsRefreshToken = 0,
    // A Librarian pass starts and ends as a Skills change.
    skillsRefreshToken = 0,
    // Opens a Session in Chat (Skill maintenance opens the Librarian's).
    onOpenSession = noop,
    initialScrollPosition = null,
    onScrollPositionChange = noop,
    // The App's Extension invalidations (see app/extensions.svelte.js).
    subscribeExtensionInvalidations = null,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    providersPanel?.handleProviderAuthCompleted(event);
  }

  export function getScrollPosition() {
    return captureScrollPosition();
  }

  // Navigation follows user tasks; editor components do not define pages.
  const sections = [
    {
      id: 'appearance',
      label: () => t('settings.appearance.title'),
    },
    {
      id: 'session_titles',
      label: () => t('settings.sessionTitles.title'),
    },
    {
      id: 'preferences',
      label: () => t('settings.preferences.title'),
    },
    {
      id: 'notifications',
      label: () => t('settings.notifications.title'),
    },
    {
      id: 'providers',
      label: () => t('settings.providers.title'),
    },
    {
      id: 'voice_controls',
      label: () => t('settings.sections.wakeword'),
    },
    {
      id: 'transcription_audio',
      label: () => t('settings.sections.transcriptionAudio'),
    },
    {
      id: 'speech_models',
      label: () => t('settings.sections.speechModels'),
    },
    {
      id: 'live_voice_model',
      label: () => t('settings.sections.liveVoice'),
    },
    {
      id: 'live_voice_shortcut',
      label: () => t('settings.sections.liveVoiceShortcut'),
    },
    {
      id: 'recall',
      label: () => t('settings.sections.recall'),
    },
    {
      id: 'reflection',
      label: () => t('settings.reflection.title'),
    },
    {
      id: 'librarian',
      label: () => t('settings.librarian.title'),
    },
    {
      id: 'web_search',
      label: () => t('settings.webSearch.title'),
    },
    { id: 'web_fetch', label: () => t('settings.webFetch.title') },
    {
      id: 'media_models',
      label: () => t('settings.sections.mediaModels'),
    },
    {
      id: 'decision_model',
      label: () => t('settings.sections.evaluation'),
    },
    {
      id: 'subagents',
      label: () => t('settings.sections.delegation'),
    },
    { id: 'channels', label: () => t('settings.channels.title') },
    {
      id: 'extensions',
      label: () => t('settings.extensions.title'),
    },
    { id: 'server', label: () => t('settings.sections.server') },
    {
      id: 'desktop_connection',
      label: () => t('settings.desktop.connection.title'),
    },
    {
      id: 'archive_retention',
      label: () => t('settings.archive.retentionTitle'),
    },
    { id: 'archive_entries', label: () => t('archive.listLabel') },
    { id: 'debug', label: () => t('debug.settings') },
  ];
  const panelById = new Map(sections.map((section) => [section.id, section]));
  const modelTasksBySection = {
    speech_models: ['speech_to_text', 'text_to_speech'],
    live_voice_model: ['live_voice'],
    media_models: [
      'image_understanding',
      'image_generation',
      'video_generation',
      'music_generation',
    ],
    decision_model: ['decision'],
  };
  let pages = $derived([
    {
      id: 'general',
      label: () => t('settings.pages.general'),
      description: () => t('settings.pages.generalDescription'),
      // Everyday display first; the time zone and the setup guide are
      // rarely revisited.
      sections: [
        'appearance',
        'session_titles',
        'notifications',
        'preferences',
      ],
    },
    {
      id: 'providers',
      label: () => t('settings.providers.title'),
      description: () => t('settings.pages.providersDescription'),
      sections: ['providers'],
    },
    {
      id: 'voice',
      label: () => t('settings.voice.title'),
      description: () => t('settings.pages.voiceDescription'),
      // Models first, the Live voice shortcut right after its Model, the
      // Desktop wakeword next, and the rarely changed recording format last.
      sections: [
        'speech_models',
        'live_voice_model',
        ...(desktopCapabilities?.liveHotkey ? ['live_voice_shortcut'] : []),
        'voice_controls',
        'transcription_audio',
      ],
    },
    {
      id: 'memory',
      label: () => t('settings.pages.memory'),
      description: () => t('settings.pages.memoryDescription'),
      sections: ['reflection', 'librarian', 'recall'],
    },
    {
      id: 'tools',
      label: () => t('settings.pages.tools'),
      description: () => t('settings.pages.toolsDescription'),
      sections: [
        'web_search',
        'web_fetch',
        'media_models',
        'decision_model',
        'subagents',
      ],
    },
    {
      id: 'integrations',
      label: () => t('settings.pages.integrations'),
      description: () => t('settings.pages.integrationsDescription'),
      sections: ['channels', 'extensions'],
    },
    {
      id: 'archive',
      label: () => t('settings.archive.title'),
      description: () => t('settings.pages.archiveDescription'),
      // The retention period first, then the archived items; an item opens
      // as a sub-page of this page in place of everything else.
      sections: ['archive_retention', 'archive_entries'],
      subPages: 'archive_entries',
    },
    {
      id: 'system',
      label: () => t('settings.pages.system'),
      description: () => t('settings.pages.systemDescription'),
      sections: [
        'server',
        ...(desktopCapabilities?.serverSelection ? ['desktop_connection'] : []),
        'debug',
      ],
    },
  ]);
  let mobileSectionOptions = $derived(
    pages.map((page) => ({
      value: page.id,
      label: page.label(),
    })),
  );
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let providerRefreshGeneration = 0;
  let providerRefreshPending = false;
  let providersPanel = $state(null);
  let scrollContainer = $state(null);
  let documentRoot = $state(null);
  let shownPlace = $derived(resolvePlace(navigation.place));
  let activePageId = $derived(shownPlace.page.id);
  let subPageShown = $derived(shownPlace.subPlace.length > 0);
  let editorsElement = $state(null);
  let searchQuery = $state('');
  // Results hold DOM element references, which must not become proxies.
  let searchResults = $state.raw([]);
  let searchActive = $derived(searchQuery.trim().length > 0);
  // The search index of the mounted editors; null after they changed.
  let searchIndex = null;
  let indexedPages = null;
  let searchFrame = null;
  // A row result waiting for its place to be shown: {key, result}.
  let pendingReveal = null;
  // The highlighted anchor of the last opened result: {element, timer}.
  let searchHit = null;
  let restoreTop = 0;
  let restorePending = false;
  let restoreFrame = null;
  // The element a section or search target keeps in view while content
  // above it settles.
  let restoreAnchor = null;
  // The canonical place last shown; null until the loaded content shows one.
  let appliedPlaceKey = null;
  // The target of that place.
  let appliedTarget = null;
  // The scroll position of the last scroll event, and the one a page's
  // sub-pages return to.
  let lastScrollTop = 0;
  let subPageReturnTop = 0;
  // Invalidates an earlier target still waiting for the page to update.
  let showGeneration = 0;

  onMount(() => {
    loadSettings();
    return () => {
      providerRefreshGeneration += 1;
      providerRefreshPending = false;
      if (restoreFrame !== null) cancelAnimationFrame(restoreFrame);
      if (searchFrame !== null) cancelAnimationFrame(searchFrame);
      pendingReveal = null;
      clearSearchHit();
    };
  });

  // Place -> shown page and section. The page follows the place directly;
  // once the content has loaded, a section target scrolls to its heading and
  // any other page change starts at the page top. A place that is not
  // canonical (a bare page or section id from an App deep link, an empty or
  // unknown place) is then corrected without a step.
  $effect(() => {
    const place = navigation.place;
    if (loading) return;
    untrack(() => {
      const target = resolvePlace(place);
      const canonical = placeFor(target);
      const key = canonical.join('/');
      if (appliedPlaceKey === null) {
        appliedPlaceKey = key;
        appliedTarget = target;
        showFirstTarget(target, place);
      } else if (key !== appliedPlaceKey || !samePlace(place, canonical)) {
        const previous = appliedTarget;
        appliedPlaceKey = key;
        appliedTarget = target;
        if (isSubPageStep(previous, target)) showSubPageStep(previous, target);
        else void showTarget(target);
      }
      if (!samePlace(place, canonical)) navigation.replace(canonical);
    });
  });

  // Loading and page composition changes re-index the editors.
  $effect(() => {
    void pages;
    void loading;
    void documentRoot;
    untrack(() => {
      searchIndex = null;
      updateSearchResults();
    });
  });

  $effect(() => {
    if (!documentRoot) return;
    const observer = new MutationObserver((records) => {
      // Editor changes re-index the search, at most once per frame; the
      // rendered result list does not.
      if (records.some(changesEditors)) {
        searchIndex = null;
        if (searchQuery.trim()) scheduleSearch();
      }
      queueRestore();
    });
    observer.observe(documentRoot, {
      childList: true,
      subtree: true,
      characterData: true,
      attributeFilter: [
        'data-help-text',
        'data-search-label',
        'data-search-terms',
      ],
    });
    const resizeObserver =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(queueRestore)
        : null;
    resizeObserver?.observe(documentRoot);
    queueRestore();
    return () => {
      observer.disconnect();
      resizeObserver?.disconnect();
    };
  });

  function changesEditors(record) {
    const node =
      record.target.nodeType === Node.ELEMENT_NODE
        ? record.target
        : record.target.parentElement;
    return node === documentRoot || Boolean(editorsElement?.contains(node));
  }

  // The translated page and section labels the search index names.
  function searchPages() {
    return pages.map((page) => ({
      id: page.id,
      label: page.label(),
      description: page.description(),
      sections: page.sections.map((id) => ({
        id,
        label: panelById.get(id).label(),
      })),
    }));
  }

  function scheduleSearch() {
    if (searchFrame !== null) return;
    searchFrame = requestAnimationFrame(() => {
      searchFrame = null;
      updateSearchResults();
    });
  }

  function sameResults(left, right) {
    return (
      left.length === right.length &&
      left.every(
        (result, index) =>
          result.key === right[index].key &&
          result.element === right[index].element &&
          result.openDetails === right[index].openDetails,
      )
    );
  }

  function updateSearchResults() {
    if (searchFrame !== null) {
      cancelAnimationFrame(searchFrame);
      searchFrame = null;
    }
    const terms = searchTerms(searchQuery);
    let results = [];
    if (terms.length && documentRoot) {
      if (!searchIndex || indexedPages !== pages) {
        indexedPages = pages;
        searchIndex = indexSettings(documentRoot, searchPages());
      }
      results = matchSettings(searchIndex, searchQuery);
    }
    if (
      terms.length &&
      terms.every((term) => AGENT_DEFAULTS_TERMS.includes(term))
    )
      results = [...results, AGENT_DEFAULTS_RESULT];
    if (!sameResults(results, searchResults)) searchResults = results;
  }

  function setSearchQuery(value) {
    searchQuery = value;
    updateSearchResults();
  }

  function resultTitle(result) {
    return result.kind === 'shortcut' ? t('agents.shared.title') : result.label;
  }

  // Where a result lives: a page result shows what the page holds, a section
  // its page, a setting its page and section (only the page when the page
  // has a single section).
  function resultLocation(result) {
    if (result.kind === 'shortcut') return t('settings.agentShortcut.search');
    const page = pageForDestination(result.pageId);
    if (!page) return '';
    if (result.kind === 'page') return page.description();
    if (result.kind === 'section' || page.sections.length === 1)
      return page.label();
    return t('settings.search.location', {
      page: page.label(),
      section: panelById.get(result.sectionId)?.label() ?? '',
    });
  }

  function headingOffset(heading) {
    return Math.max(
      0,
      scrollContainer.scrollTop +
        heading.getBoundingClientRect().top -
        scrollContainer.getBoundingClientRect().top -
        24,
    );
  }

  function queueRestore() {
    if (
      (!restorePending && !restoreAnchor) ||
      loading ||
      !scrollContainer ||
      restoreFrame !== null
    )
      return;
    restoreFrame = requestAnimationFrame(() => {
      restoreFrame = null;
      if (!scrollContainer) return;
      if (restoreAnchor?.isConnected)
        scrollContainer.scrollTop = headingOffset(restoreAnchor);
      else if (restorePending) scrollContainer.scrollTop = restoreTop;
    });
  }

  function releaseRestore() {
    restorePending = false;
    restoreAnchor = null;
  }

  // The reading position App keeps while another main view is shown. Its
  // place tells a return to the same entry apart from a new deep link.
  function captureScrollPosition() {
    return scrollContainer
      ? {
          top: Math.max(0, scrollContainer.scrollTop),
          pageId: activePageId,
          place: [...navigation.place],
        }
      : null;
  }

  function handleContentScroll() {
    lastScrollTop = scrollContainer?.scrollTop ?? 0;
    if (!restorePending && !searchActive)
      onScrollPositionChange(captureScrollPosition());
  }

  function pageForDestination(id) {
    return (
      pages.find((page) => page.id === id) ??
      pages.find((page) => page.sections.includes(id))
    );
  }

  // The page and section a place names. A page with a single section has no
  // section headings, so the page itself is the target there; on a page with
  // sub-pages, a second segment that names no section is the shown sub-page;
  // anything unknown shows the start page.
  function resolvePlace(place) {
    const [first = '', second = ''] = place;
    const page = pageForDestination(first);
    if (!page) return { page: pages[0], sectionId: '', subPlace: [] };
    if (
      page.subPages &&
      page.id === first &&
      second &&
      !page.sections.includes(second)
    ) {
      return { page, sectionId: '', subPlace: [second] };
    }
    const sectionId = page.id === first ? second : first;
    return {
      page,
      sectionId:
        page.sections.length > 1 && page.sections.includes(sectionId)
          ? sectionId
          : '',
      subPlace: [],
    };
  }

  function placeFor({ page, sectionId, subPlace = [] }) {
    if (subPlace.length) return [page.id, ...subPlace];
    return sectionId ? [page.id, sectionId] : [page.id];
  }

  // The handle a page's sub-page panel moves with: its place is the shown
  // sub-page of that page (empty otherwise), and its steps stay below it.
  function subPageNavigation(pageId) {
    return {
      get place() {
        return shownPlace.page.id === pageId ? shownPlace.subPlace : [];
      },
      navigate: (place = []) => navigation.navigate([pageId, ...place]),
      replace: (place = []) => navigation.replace([pageId, ...place]),
      up: (place = []) => navigation.up([pageId, ...place]),
    };
  }

  const archiveNavigation = subPageNavigation('archive');

  // Opening a sub-page, another one, or returning from one to its page.
  function isSubPageStep(previous, target) {
    return (
      previous?.page.id === target.page.id &&
      !target.sectionId &&
      (previous.subPlace.length > 0 || target.subPlace.length > 0)
    );
  }

  // A sub-page opens at its top, where its panel puts the focus; returning
  // to the page resumes the position it was opened from, and the panel
  // returns the focus to what opened it.
  function showSubPageStep(previous, target) {
    showGeneration += 1;
    pendingReveal = null;
    releaseRestore();
    setSearchQuery('');
    if (!scrollContainer) return;
    if (target.subPlace.length) {
      if (!previous.subPlace.length) subPageReturnTop = lastScrollTop;
      scrollContainer.scrollTop = 0;
    } else {
      restoreTop = subPageReturnTop;
      restorePending = true;
      scrollContainer.scrollTop = restoreTop;
      queueRestore();
    }
    onScrollPositionChange(captureScrollPosition());
  }

  function samePlace(left, right) {
    return (
      Array.isArray(left) &&
      left.length === right.length &&
      left.every((segment, index) => segment === right[index])
    );
  }

  // The first place shown after loading. Returning to the entry the reading
  // position was kept for, or to a page without a section target, resumes
  // that position when it belongs to the shown page; a section target
  // otherwise scrolls to its section and any other page opens at its top.
  function showFirstTarget(target, place) {
    const kept = initialScrollPosition;
    if (
      kept?.pageId === target.page.id &&
      (!target.sectionId || samePlace(kept.place, place))
    ) {
      restoreTop = Math.max(0, kept.top || 0);
      restorePending = true;
      queueRestore();
    } else if (target.sectionId) {
      void showTarget(target);
    }
  }

  async function showTarget({ page, sectionId, subPlace = [] }) {
    const generation = ++showGeneration;
    const key = placeFor({ page, sectionId, subPlace }).join('/');
    const reveal = pendingReveal?.key === key ? pendingReveal.result : null;
    pendingReveal = null;
    releaseRestore();
    setSearchQuery('');
    // A sub-page opened from elsewhere opens at its top, and its panel owns
    // the focus there.
    if (subPlace.length) subPageReturnTop = 0;
    await tick();
    if (generation !== showGeneration) return;
    const headingId = sectionId
      ? 'settings-section-' + sectionId
      : 'settings-page-' + page.id;
    const heading = subPlace.length
      ? null
      : documentRoot?.querySelector('#' + headingId);
    if (reveal && (await revealResult(reveal, heading, generation))) return;
    if (scrollContainer) {
      scrollContainer.scrollTop =
        sectionId && heading ? headingOffset(heading) : 0;
      // Keep a section in view while asynchronous editors above it settle.
      if (sectionId && heading) {
        restoreAnchor = heading;
        queueRestore();
      }
    }
    onScrollPositionChange(captureScrollPosition());
    heading?.focus({ preventScroll: true });
  }

  // Brings a setting result into view once its place is shown: opens what
  // hides it through the page's own disclosures, keeps its row (or the
  // switch that reveals it) in view, focuses its first control and
  // highlights it briefly. Revealing is not a navigation step. Returns false
  // when only the section heading can stand for it.
  async function revealResult(result, heading, generation) {
    const section = documentRoot?.querySelector(
      `[data-settings-section="${result.sectionId}"]`,
    );
    const body = section?.querySelector('.s-section__body');
    if (!body || !scrollContainer) return false;
    if (expandSettingsEntry(result, section)) {
      await tick();
      if (generation !== showGeneration) return true;
    }
    const anchor = settingsEntryAnchor(result.element, body);
    if (!anchor) return false;
    // The section heading stays in view while the setting fits below it.
    const fitsBelowHeading =
      heading &&
      anchor.getBoundingClientRect().bottom -
        heading.getBoundingClientRect().top +
        48 <=
        scrollContainer.clientHeight;
    restoreAnchor = fitsBelowHeading ? heading : anchor;
    scrollContainer.scrollTop = headingOffset(restoreAnchor);
    queueRestore();
    onScrollPositionChange(captureScrollPosition());
    if (!focusSettingsControl(anchor)) heading?.focus({ preventScroll: true });
    markSearchHit(anchor);
    return true;
  }

  function markSearchHit(element) {
    clearSearchHit();
    // Restarts the highlight when the same element is marked again.
    void element.offsetWidth;
    element.classList.add('settings-search-hit');
    searchHit = { element, timer: setTimeout(clearSearchHit, SEARCH_HIT_MS) };
  }

  function clearSearchHit() {
    if (!searchHit) return;
    clearTimeout(searchHit.timer);
    searchHit.element.classList.remove('settings-search-hit');
    searchHit = null;
  }

  // Opening a page or a search result is a step. Opening the place already
  // shown returns to its start: the page top or the section heading.
  function openDestination(id) {
    return showPlace(resolvePlace([id]), null);
  }

  // A setting result opens its section's place, then reveals the setting.
  function openResult(result) {
    if (result.kind === 'shortcut') return navigateToDefaults();
    const target = resolvePlace([
      result.kind === 'page' ? result.pageId : result.sectionId,
    ]);
    return showPlace(target, result.kind === 'row' ? result : null);
  }

  function showPlace(target, reveal) {
    const place = placeFor(target);
    pendingReveal = reveal ? { key: place.join('/'), result: reveal } : null;
    if (samePlace(place, navigation.place)) return showTarget(target);
    return navigation.navigate(place);
  }

  function navigateToDefaults() {
    return onNavigateToAgentDefaults('defaults');
  }

  function handleSearchInput(event) {
    releaseRestore();
    pendingReveal = null;
    setSearchQuery(event.currentTarget.value);
    if (scrollContainer) scrollContainer.scrollTop = 0;
  }

  // Enter opens the best match; an IME keeps its own Enter.
  function handleSearchKeydown(event) {
    if (event.key !== 'Enter' || isImeComposing(event)) return;
    if (searchFrame !== null) updateSearchResults();
    const [first] = searchResults;
    if (!first) return;
    event.preventDefault();
    void openResult(first);
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
      title: t('errors.appError'),
      message,
      variant: 'error',
    });
  }

  function applySettings(nextSettings) {
    settings = nextSettings;
    setApplicationTimeZone(nextSettings?.general?.timezone);

    applyAppearanceSettings(nextSettings?.appearance);
    applyArchiveSettings(nextSettings?.archive);
  }

  function commitSettings(nextSettings) {
    // Each other Settings editor saves its own section. Its full response may
    // predate a Provider refresh, whose projection has an independent owner.
    applySettings({
      ...nextSettings,
      providers: settings.providers,
      local_models: settings.local_models,
    });
    onSettingsCommit(settings);
  }

  function commitProviderSettings(nextSettings) {
    const refreshAfterCommit = providerRefreshPending;
    providerRefreshGeneration += 1;
    settings = {
      ...settings,
      providers: nextSettings.providers,
      local_models: nextSettings.local_models,
    };
    onSettingsCommit(settings);
    if (refreshAfterCommit) {
      // A Model-refresh result may predate another Provider change. Re-read
      // after its commit rather than losing the pending invalidation.
      void refreshProviderSettings().catch(reportProviderRefreshError);
    }
  }

  function reportProviderRefreshError(error) {
    reportSettingsError(`${t('settings.loadError')} ${error.message}`);
  }

  async function refreshProviderSettings() {
    const generation = ++providerRefreshGeneration;
    providerRefreshPending = true;
    try {
      const nextSettings = await getSettings();
      if (generation !== providerRefreshGeneration) return;
      // Provider invalidations also refresh local Model context limits. Other
      // editors retain their own baseline and pending draft; a Provider refresh
      // must neither remount them nor turn remote values into local edits.
      settings = {
        ...settings,
        providers: nextSettings.providers,
        local_models: nextSettings.local_models,
      };
      onSettingsCommit(settings);
    } catch (error) {
      if (generation === providerRefreshGeneration) throw error;
    } finally {
      if (generation === providerRefreshGeneration)
        providerRefreshPending = false;
    }
  }

  async function loadSettings() {
    loading = true;
    loadError = '';
    // The loaded content shows the current place afresh, also after a retry.
    appliedPlaceKey = null;

    try {
      const nextSettings = await getSettings();
      applySettings(nextSettings);
    } catch (error) {
      loadError = `${t('settings.loadError')} ${error.message}`;
    } finally {
      loading = false;
    }
  }
</script>

{#snippet panelContent(panelId)}
  {#if panelId === 'preferences'}
    <SettingsGeneralPanel
      page="preferences"
      {settings}
      {onOpenSetupGuide}
      onCommit={commitSettings}
      onError={reportSettingsError}
    />
  {:else if panelId === 'providers'}
    <SettingsProvidersPanel
      bind:this={providersPanel}
      {settings}
      visible={true}
      {providerAuthEvent}
      {connectProvider}
      {disconnectProvider}
      onCommitProviderSettings={commitProviderSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
      onRefreshProviderSettings={refreshProviderSettings}
      {modelsRefreshToken}
    />
  {:else if panelId === 'channels'}
    <SettingsChannelsPanel
      {onToast}
      {channelsRefreshToken}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'extensions'}
    <SettingsExtensionsPanel
      {onToast}
      {subscribeExtensionInvalidations}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if modelTasksBySection[panelId]}
    <SettingsSpecializedModelsPanel
      taskTypes={modelTasksBySection[panelId]}
      showTaskLabels={panelId !== 'live_voice_model'}
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'session_titles'}
    <SettingsSessionTitlesPanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'recall'}
    <SettingsRecallPanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
      {recallIndexStatus}
    />
  {:else if panelId === 'voice_controls'}
    <WakewordVoiceSettings
      {agents}
      wakewordAvailable={desktopCapabilities?.wakeword === true}
      {desktopVoice}
      {onToast}
    />
  {:else if panelId === 'transcription_audio'}
    <TranscriptionAudioSettings
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_fetch'}
    <SettingsWebFetchPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_search'}
    <SettingsWebSearchPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'subagents'}
    <SettingsSubAgentsPanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'reflection'}
    <SettingsReflectionPanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'librarian'}
    <SettingsLibrarianPanel
      {settings}
      {agents}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
      {onOpenSession}
      {modelsRefreshToken}
      {agentsRefreshToken}
      {skillsRefreshToken}
    />
  {:else if panelId === 'notifications'}
    <SettingsNotificationsPanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'appearance'}
    <SettingsAppearancePanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'archive_retention'}
    <SettingsArchivePanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'archive_entries'}
    <ArchiveEntriesPanel
      navigation={archiveNavigation}
      active={activePageId === 'archive'}
      {agents}
      {projects}
      {archiveRefreshToken}
      {agentsRefreshToken}
      {projectsRefreshToken}
      {sessionsRefreshToken}
      {onToast}
    />
  {:else if panelId === 'debug'}
    <SettingsDebugPanel
      {settings}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
      {onDebugEnabledChange}
    />
  {:else if panelId === 'server'}
    <SettingsGeneralPanel
      {settings}
      {clientsRefreshToken}
      {onOpenSetupGuide}
      onCommit={commitSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'desktop_connection'}
    <DesktopConnectionSettings {onToast} />
  {:else if panelId === 'live_voice_shortcut'}
    <DesktopLiveVoiceShortcut {onToast} />
  {/if}
{/snippet}

<section class={SETTINGS_LAYOUT_CLASS} aria-label={t('settings.title')}>
  <nav class="settings-nav secondary-pane" aria-label={t('settings.sections')}>
    <div class="settings-nav-title secondary-pane__title">
      {t('settings.title')}
    </div>
    <div class="settings-search">
      <input
        class="settings-search-input"
        type="search"
        spellcheck="false"
        autocomplete="off"
        value={searchQuery}
        oninput={handleSearchInput}
        onkeydown={handleSearchKeydown}
        placeholder={t('settings.search.placeholder')}
        aria-label={t('settings.search.label')}
      />
    </div>
    <div class="settings-desktop-index">
      {#each pages as page (page.id)}
        <button
          class="snav-item"
          class:snav-item--active={!searchActive && page.id === activePageId}
          type="button"
          aria-current={!searchActive && page.id === activePageId
            ? 'page'
            : undefined}
          onclick={() => openDestination(page.id)}>{page.label()}</button
        >
      {/each}
    </div>
    <div class="settings-mobile-section-picker">
      <Dropdown
        id="settings-mobile-section"
        value={activePageId}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections')}
        onValueChange={openDestination}
      />
    </div>
  </nav>
  <!-- Keyboard interaction releases scroll restoration for this scrollable region. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div
    class="settings-content"
    role="region"
    aria-label={t('settings.content')}
    tabindex="0"
    bind:this={scrollContainer}
    onscroll={handleContentScroll}
    onwheel={releaseRestore}
    ontouchstart={releaseRestore}
    onpointerdown={releaseRestore}
    onkeydown={releaseRestore}
  >
    <div class="s-doc" bind:this={documentRoot}>
      {#if loading}
        <Banner variant="neutral">{t('settings.loading')}</Banner>
      {:else if loadError}
        <Banner variant="error"
          ><span>{loadError}</span><Button
            variant="secondary"
            onClick={loadSettings}>{t('common.retry')}</Button
          ></Banner
        >
      {:else}
        {#if searchActive}
          <header class="settings-page-heading">
            <h2>{t('settings.search.results')}</h2>
            <p role="status">
              {t('settings.search.resultCount', {
                count: searchResults.length,
              })}
            </p>
          </header>
          <div class="settings-search-results">
            {#each searchResults as result (result.key)}
              <Button
                class="settings-search-result"
                onClick={() => openResult(result)}
              >
                <span class="settings-search-result__title"
                  >{resultTitle(result)}</span
                >
                <span class="settings-search-result__description"
                  >{resultLocation(result)}</span
                >
              </Button>
            {:else}
              <EmptyState
                density="compact"
                description={t('settings.search.noMatches')}
              />
            {/each}
          </div>
        {/if}
        <div class="settings-editors" bind:this={editorsElement}>
          {#each pages as page (page.id)}
            <!-- A shown sub-page replaces its page's heading and other sections. -->
            {@const subPage = subPageShown && activePageId === page.id}
            <article
              class="settings-page"
              class:settings-page--sub-page={subPage}
              data-settings-page={page.id}
              hidden={searchActive || activePageId !== page.id}
              aria-labelledby={'settings-page-' + page.id}
            >
              <header class="settings-page-heading" hidden={subPage}>
                <h2 id={'settings-page-' + page.id} tabindex="-1">
                  {page.label()}
                </h2>
                <p>{page.description()}</p>
              </header>
              {#if page.id === 'providers'}
                <p class="settings-related">
                  {t('settings.agentShortcut.hint')}
                  <button
                    type="button"
                    class="settings-defaults-link"
                    onclick={navigateToDefaults}
                  >
                    {t('agents.shared.title')}<span aria-hidden="true">↗</span>
                  </button>
                </p>
              {/if}
              <!-- Only while something runs or needs attention; not a section,
                   so search and navigation never lead to it. -->
              {#if page.id === 'general' && backgroundActivity.length > 0}
                <SettingsActivityPanel
                  activities={backgroundActivity}
                  onOpen={openDestination}
                  onError={(message) => reportSettingsError(message)}
                />
              {/if}
              {#each page.sections as panelId (panelId)}
                {@const panel = panelById.get(panelId)}
                <section
                  class="settings-editor s-section"
                  data-settings-section={panelId}
                  hidden={searchActive ||
                    activePageId !== page.id ||
                    (subPage && panelId !== page.subPages)}
                  aria-labelledby={page.sections.length === 1
                    ? 'settings-page-' + page.id
                    : 'settings-section-' + panelId}
                >
                  {#if page.sections.length > 1}
                    <header
                      class="settings-section-heading s-section__head"
                      hidden={subPage}
                    >
                      <h3
                        class="s-section__title"
                        id={'settings-section-' + panelId}
                        tabindex="-1"
                      >
                        {panel.label()}
                      </h3>
                    </header>
                  {/if}
                  <div class="settings-editor-body s-section__body">
                    {@render panelContent(panelId)}
                  </div>
                </section>
              {/each}
            </article>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</section>
