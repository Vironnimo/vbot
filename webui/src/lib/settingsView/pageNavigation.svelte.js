import { onDestroy, tick, untrack } from 'svelte';
import { t } from '$lib/i18n.js';
import { isImeComposing } from '$lib/keyboard.js';
import { panelById } from './catalog.js';
import {
  expandSettingsEntry,
  focusSettingsControl,
  indexSettings,
  matchSettings,
  normalizeSearchText,
  searchTerms,
  settingsEntryAnchor,
} from '$lib/settingsSearch.js';

// Shared Agent defaults live in Agents; these words lead there.
const AGENT_DEFAULTS_TERMS = normalizeSearchText(
  'Agent defaults global shared Model Thinking effort advanced sampling temperature top_p fallback compaction',
);
const AGENT_DEFAULTS_RESULT = { key: 'agent_defaults', kind: 'shortcut' };
// How long an opened search result stays highlighted.
const SEARCH_HIT_MS = 1600;

export function createSettingsPageNavigation(context) {
  let scrollContainer = $state(null);
  let documentRoot = $state(null);
  let shownPlace = $derived(resolvePlace(context.navigation.place));
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

  onDestroy(() => {
    if (restoreFrame !== null) cancelAnimationFrame(restoreFrame);
    if (searchFrame !== null) cancelAnimationFrame(searchFrame);
    pendingReveal = null;
    clearSearchHit();
  });

  // Place -> shown page and section. The page follows the place directly;
  // once the content has loaded, a section target scrolls to its heading and
  // any other page change starts at the page top. A place that is not
  // canonical (a bare page or section id from an App deep link, an empty or
  // unknown place) is then corrected without a step.
  $effect(() => {
    const place = context.navigation.place;
    if (context.loading) return;
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
      if (!samePlace(place, canonical)) context.navigation.replace(canonical);
    });
  });

  // Loading and page composition changes re-index the editors.
  $effect(() => {
    void context.pages;
    void context.loading;
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
    return context.pages.map((page) => ({
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
      if (!searchIndex || indexedPages !== context.pages) {
        indexedPages = context.pages;
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
      context.loading ||
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
          place: [...context.navigation.place],
        }
      : null;
  }

  function handleContentScroll() {
    lastScrollTop = scrollContainer?.scrollTop ?? 0;
    if (!restorePending && !searchActive)
      context.onScrollPositionChange(captureScrollPosition());
  }

  function pageForDestination(id) {
    return (
      context.pages.find((page) => page.id === id) ??
      context.pages.find((page) => page.sections.includes(id))
    );
  }

  // The page and section a place names. A page with a single section has no
  // section headings, so the page itself is the target there; on a page with
  // sub-pages, a second segment that names no section is the shown sub-page;
  // anything unknown shows the start page.
  function resolvePlace(place) {
    const [first = '', second = ''] = place;
    const page = pageForDestination(first);
    if (!page) return { page: context.pages[0], sectionId: '', subPlace: [] };
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
      navigate: (place = []) => context.navigation.navigate([pageId, ...place]),
      replace: (place = []) => context.navigation.replace([pageId, ...place]),
      up: (place = []) => context.navigation.up([pageId, ...place]),
    };
  }

  const archiveNavigation = subPageNavigation('archive');
  const debugNavigation = subPageNavigation('debug');

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
    context.onScrollPositionChange(captureScrollPosition());
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
    const kept = context.initialScrollPosition;
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
    context.onScrollPositionChange(captureScrollPosition());
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
    context.onScrollPositionChange(captureScrollPosition());
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
    if (samePlace(place, context.navigation.place)) return showTarget(target);
    return context.navigation.navigate(place);
  }

  function navigateToDefaults() {
    return context.onNavigateToAgentDefaults('defaults');
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

  return {
    get scrollContainer() {
      return scrollContainer;
    },
    set scrollContainer(value) {
      scrollContainer = value;
    },
    get documentRoot() {
      return documentRoot;
    },
    set documentRoot(value) {
      documentRoot = value;
    },
    get editorsElement() {
      return editorsElement;
    },
    set editorsElement(value) {
      editorsElement = value;
    },
    get searchQuery() {
      return searchQuery;
    },
    get searchResults() {
      return searchResults;
    },
    get searchActive() {
      return searchActive;
    },
    get shownPlace() {
      return shownPlace;
    },
    get activePageId() {
      return activePageId;
    },
    get subPageShown() {
      return subPageShown;
    },
    get archiveNavigation() {
      return archiveNavigation;
    },
    get debugNavigation() {
      return debugNavigation;
    },
    get appliedPlaceKey() {
      return appliedPlaceKey;
    },
    set appliedPlaceKey(value) {
      appliedPlaceKey = value;
    },
    captureScrollPosition,
    openDestination,
    openResult,
    resultTitle,
    resultLocation,
    navigateToDefaults,
    refreshSearchResults() {
      searchIndex = null;
      updateSearchResults();
    },
    handleSearchInput,
    handleSearchKeydown,
    handleContentScroll,
    releaseRestore,
  };
}
