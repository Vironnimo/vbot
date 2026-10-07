import {
  createMentionIndex,
  findOpenQuotedMention,
  formatMentionToken,
  isMentionTokenChar,
  mentionCandidates,
  mentionQueryParts,
} from '$lib/fileMentions.js';
import {
  buildModelSelectOptions,
  filterModelSelectOptions,
  modelFilterFooterLabel,
} from '$lib/modelSelection.js';
import { tick } from 'svelte';

// Directory names are data: only a listing's own keys count.
const listingOf = (listings, directory) =>
  Object.hasOwn(listings, directory) ? listings[directory] : undefined;

export function createComposerPicker(context) {
  const SKILL_TRIGGER_PATTERN = /[A-Za-z0-9_-]/u;

  // FileAutocomplete renders exactly these rows, so keyboard navigation and
  // the rendered list can never disagree on the match set.
  const MAX_FILE_MATCHES = 50;

  // Typing a directory prefix lists that directory once the typing pauses.
  const ENTRIES_DEBOUNCE_MS = 120;

  let autocompleteElement = $state(null);

  let fileAutocompleteElement = $state(null);

  let modelAutocompleteElement = $state(null);

  let triggerContext = $state(null);

  let activeSkillIndex = $state(0);

  // Whether arrow keys or the pointer moved the highlight since the list last
  // reset. Until then an @-list that opens on ignored entries highlights the
  // first entry the index holds, the likelier choice.
  let activeIndexMoved = $state(false);

  // @-mention picker data: `null` = never fetched for this session. The index
  // (files plus the folders holding them) is fetched once per picker open
  // (fresh list, no cache-invalidation problem) and reused at submit to decide
  // which @-tokens are real files.
  let fileCandidates = $state.raw(null);

  let fileDirectories = $state.raw([]);

  let fileListTruncated = $state(false);

  let fileListLoading = $state(false);

  let _fileFetchToken = 0;

  // The direct entries of each directory the picker visited during this open,
  // ignored ones included: directory -> `{ loading }` or `{ entries,
  // truncated, failed }`. Replaced, never mutated, so a send can keep it.
  let directoryEntries = $state.raw({});

  let pendingEntriesDirectory = $state(null);

  let _entriesTimer = null;

  let _entriesToken = 0;

  // /model argument autocomplete: `null` = never fetched. Fetched once when the
  // `/model ` trigger opens and reused while the popup stays active.
  let modelCatalog = $state(null);

  // /model argument autocomplete: `null` = never fetched. Fetched once when the
  // `/model ` trigger opens and reused while the popup stays active.
  let modelCatalogLoading = $state(false);

  let _modelCatalogFetchToken = 0;

  let showAllModels = $state(false);

  let _suppressSelectionUpdate = false;

  let _triggerClosed = false;

  let triggerItems = $derived(
    context.availableSkills.filter((item) => item?.name),
  );

  let autocompleteItems = $derived.by(() =>
    triggerItemsForContext(triggerContext),
  );

  let autocompleteQuery = $derived.by(() => {
    if (!triggerContext) {
      return '';
    }
    // A quoted @-mention carries its unescaped text.
    if (typeof triggerContext.query === 'string') {
      return triggerContext.query;
    }

    return context.content.slice(triggerContext.start + 1, triggerContext.end);
  });

  let mentionIndex = $derived(
    createMentionIndex({
      files: fileCandidates ?? [],
      directories: fileDirectories,
    }),
  );

  // The directory the @-query is in ('' = the listing's root), or null when
  // there is no @-query or its path leaves the root (only the index answers).
  let mentionDirectory = $derived(
    triggerContext?.marker === '@'
      ? mentionQueryParts(autocompleteQuery).directory
      : null,
  );

  let mentionListing = $derived(
    mentionDirectory === null
      ? null
      : listingOf(directoryEntries, mentionDirectory),
  );

  let fileRows = $derived.by(() =>
    triggerContext?.marker === '@'
      ? mentionCandidates({
          index: mentionIndex,
          directory: mentionDirectory ?? '',
          entries: mentionListing?.entries ?? [],
          query: autocompleteQuery,
          limit: MAX_FILE_MATCHES,
        })
      : [],
  );

  let fileRowsLoading = $derived(
    triggerContext?.marker === '@' &&
      (fileListLoading ||
        (mentionDirectory !== null &&
          (Boolean(mentionListing?.loading) ||
            pendingEntriesDirectory === mentionDirectory))),
  );

  let activeIndex = $derived.by(() => {
    if (activeIndexMoved || triggerContext?.marker !== '@') {
      return activeSkillIndex;
    }
    if (!fileRows[0]?.ignored) return activeSkillIndex;
    return Math.max(
      0,
      fileRows.findIndex((row) => !row.ignored),
    );
  });

  let fileRowsTruncated = $derived(
    fileListTruncated || Boolean(mentionListing?.truncated),
  );

  let showSkillAutocomplete = $derived(
    Boolean(triggerContext) &&
      triggerContext.marker !== '@' &&
      triggerContext.marker !== 'model' &&
      matchingSkillCount() > 0,
  );

  let showFileAutocomplete = $derived(
    Boolean(triggerContext) &&
      triggerContext.marker === '@' &&
      (fileRowsLoading || fileRows.length > 0),
  );

  let allModelOptions = $derived.by(() => {
    if (!modelCatalog) {
      return [];
    }
    return buildModelSelectOptions({
      models: modelCatalog.models,
      connections: modelCatalog.connections,
    }).filter((option) => option.value !== '');
  });

  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, { showAll: showAllModels }),
  );

  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
    }),
  );

  let showModelAutocomplete = $derived(
    Boolean(triggerContext) &&
      triggerContext.marker === 'model' &&
      (modelCatalogLoading || matchingModelCount() > 0),
  );

  const activeAutocompleteElement = () =>
    showFileAutocomplete
      ? fileAutocompleteElement
      : showModelAutocomplete
        ? modelAutocompleteElement
        : autocompleteElement;

  const activeAutocompleteLoading = () =>
    (showFileAutocomplete && fileRowsLoading) ||
    (showModelAutocomplete && modelCatalogLoading);

  const activeMatchCount = () => {
    if (triggerContext?.marker === '@') {
      return fileRows.length;
    }
    if (triggerContext?.marker === 'model') {
      return matchingModelCount();
    }
    return matchingSkillCount();
  };

  const matchingSkillCount = () => {
    if (!triggerContext) {
      return 0;
    }

    const normalizedQuery = autocompleteQuery.trim().toLowerCase();
    const matchingItems = normalizedQuery
      ? autocompleteItems.filter((item) =>
          `${item.name} ${item.description ?? ''}`
            .toLowerCase()
            .includes(normalizedQuery),
        )
      : autocompleteItems;

    // Mirror SkillAutocomplete's match set exactly (same predicate, no cap) so
    // arrow-key navigation can reach every rendered entry — the popup shows all
    // matches (scrollable), and the keyboard must not stop short of the list.
    return matchingItems.length;
  };

  const matchingModelCount = () => {
    if (!triggerContext || triggerContext.marker !== 'model') {
      return 0;
    }

    const normalizedQuery = autocompleteQuery.trim().toLowerCase();
    if (!normalizedQuery) {
      return modelOptions.length;
    }

    return modelOptions.filter((option) =>
      `${option.label} ${option.secondaryLabel ?? ''}`
        .toLowerCase()
        .includes(normalizedQuery),
    ).length;
  };

  function triggerItemsForContext(context) {
    if (!context) {
      return [];
    }

    if (context.marker === '$') {
      return triggerItems.filter((item) => item.type !== 'command');
    }

    return triggerItems;
  }

  const updateTriggerContext = () => {
    if (_triggerClosed) {
      return;
    }

    if (!context.inputElement) {
      triggerContext = null;
      resetActiveIndex();
      return;
    }

    const cursorPosition =
      context.inputElement.selectionStart ?? context.content.length;
    const previousContext = triggerContext;
    const skillTrigger = detectSkillTrigger(context.content, cursorPosition);
    const modelTrigger = skillTrigger
      ? null
      : detectModelArgumentTrigger(context.content, cursorPosition);
    triggerContext = skillTrigger ?? modelTrigger;
    resetActiveIndex();

    // Reset show-all when leaving the model trigger.
    if (
      previousContext?.marker === 'model' &&
      triggerContext?.marker !== 'model'
    ) {
      showAllModels = false;
    }

    // A newly opened @-picker (or the caret jumping to a different @-token)
    // fetches a fresh file list; typing within the same token filters locally
    // and lists each typed directory once.
    if (
      triggerContext?.marker === '@' &&
      (previousContext?.marker !== '@' ||
        previousContext.start !== triggerContext.start)
    ) {
      refreshFileCandidates();
    } else if (triggerContext?.marker === '@') {
      requestEntries(triggerDirectory(triggerContext));
    } else {
      cancelPendingEntries();
    }

    // A newly opened /model argument popup fetches the model catalog once;
    // typing within the same argument filters locally.
    if (
      triggerContext?.marker === 'model' &&
      previousContext?.marker !== 'model'
    ) {
      refreshModelCatalog();
    }
  };

  const refreshFileCandidates = async () => {
    resetDirectoryEntries();
    if (typeof context.onListFiles !== 'function') {
      fileCandidates = [];
      fileDirectories = [];
      fileListTruncated = false;
      return;
    }
    if (triggerContext?.marker === '@') {
      requestEntries(triggerDirectory(triggerContext), { immediate: true });
    }
    // The token invalidates stale responses: a session switch or a newer fetch
    // bumps it, and the slower response is dropped instead of applied.
    const fetchToken = ++_fileFetchToken;
    fileListLoading = true;
    try {
      const result = await context.onListFiles();
      if (fetchToken !== _fileFetchToken) {
        return;
      }
      applyFileIndex(result);
    } catch {
      if (fetchToken !== _fileFetchToken) {
        return;
      }
      // Keep whatever list we had; a picker without data simply shows nothing.
      fileCandidates = fileCandidates ?? [];
      fileListTruncated = false;
    } finally {
      if (fetchToken === _fileFetchToken) {
        fileListLoading = false;
      }
    }
  };

  // The listed index of files and the folders that hold them.
  function applyFileIndex(result) {
    fileCandidates = Array.isArray(result?.files) ? result.files : [];
    fileDirectories = Array.isArray(result?.directories)
      ? result.directories
      : [];
    fileListTruncated = Boolean(result?.truncated);
  }

  // Lists a directory's direct entries once per picker open: at once when the
  // picker opens or a folder is chosen, otherwise after typing pauses (a
  // directory typed past is never listed). A null directory lists nothing.
  function requestEntries(directory, { immediate = false } = {}) {
    if (directory === null || listingOf(directoryEntries, directory)) {
      cancelPendingEntries();
      return;
    }
    if (immediate) {
      cancelPendingEntries();
      void loadEntries(directory);
      return;
    }
    if (pendingEntriesDirectory === directory) {
      return;
    }
    cancelPendingEntries();
    pendingEntriesDirectory = directory;
    _entriesTimer = setTimeout(() => {
      _entriesTimer = null;
      pendingEntriesDirectory = null;
      void loadEntries(directory);
    }, ENTRIES_DEBOUNCE_MS);
  }

  function cancelPendingEntries() {
    if (_entriesTimer !== null) {
      clearTimeout(_entriesTimer);
      _entriesTimer = null;
    }
    pendingEntriesDirectory = null;
  }

  async function loadEntries(directory) {
    if (typeof context.onListFiles !== 'function') {
      return;
    }
    const entriesToken = _entriesToken;
    directoryEntries = { ...directoryEntries, [directory]: { loading: true } };
    let listing;
    try {
      const result = await context.onListFiles({ directory });
      listing = {
        entries: Array.isArray(result?.entries) ? result.entries : [],
        truncated: Boolean(result?.truncated),
      };
    } catch {
      // A missing or unreadable directory simply adds nothing.
      listing = { entries: [], truncated: false, failed: true };
    }
    if (entriesToken === _entriesToken) {
      directoryEntries = { ...directoryEntries, [directory]: listing };
    }
  }

  function resetDirectoryEntries() {
    cancelPendingEntries();
    _entriesToken += 1;
    directoryEntries = {};
  }

  // The directories listed so far in this open, as a lookup a send keeps
  // after the picker moves on: directory -> its entries, or null.
  function listedEntries() {
    const listings = directoryEntries;
    return (directory) => {
      const listing = listingOf(listings, directory);
      return listing?.entries && !listing.failed ? listing.entries : null;
    };
  }

  function resetActiveIndex() {
    activeSkillIndex = 0;
    activeIndexMoved = false;
  }

  // The user moved the highlight (arrow keys or pointer): it stays put.
  function moveActiveIndex(index) {
    activeSkillIndex = index;
    activeIndexMoved = true;
  }

  const refreshModelCatalog = async () => {
    if (typeof context.onLoadModelCatalog !== 'function') {
      modelCatalog = { models: [], connections: [] };
      return;
    }
    // The token invalidates stale responses: a session switch or a newer fetch
    // bumps it, and the slower response is dropped instead of applied.
    const fetchToken = ++_modelCatalogFetchToken;
    modelCatalogLoading = true;
    try {
      const result = await context.onLoadModelCatalog();
      if (fetchToken !== _modelCatalogFetchToken) {
        return;
      }
      modelCatalog = {
        models: Array.isArray(result?.models) ? result.models : [],
        connections: Array.isArray(result?.connections)
          ? result.connections
          : [],
      };
    } catch {
      if (fetchToken !== _modelCatalogFetchToken) {
        return;
      }
      modelCatalog = modelCatalog ?? { models: [], connections: [] };
    } finally {
      if (fetchToken === _modelCatalogFetchToken) {
        modelCatalogLoading = false;
      }
    }
  };

  const detectModelArgumentTrigger = (value, cursorPosition) => {
    const boundedCursor = Math.max(0, Math.min(cursorPosition, value.length));

    if (!value.startsWith('/model')) {
      return null;
    }

    if (value.length <= 6 || value[6] !== ' ') {
      return null;
    }

    if (boundedCursor < 7) {
      return null;
    }

    return { marker: 'model', start: 6, end: boundedCursor };
  };

  // The directory an @-trigger's text is in ('' = the listing's root), or
  // null when its path leaves the root.
  const triggerDirectory = (trigger) =>
    mentionQueryParts(
      typeof trigger.query === 'string'
        ? trigger.query
        : context.content.slice(trigger.start + 1, trigger.end),
    ).directory;

  const detectFileTrigger = (value, boundedCursor) => {
    let start = boundedCursor - 1;

    while (start >= 0 && isMentionTokenChar(value[start])) {
      start -= 1;
    }

    if (start >= 0 && value[start] === '@') {
      const previous = start > 0 ? value[start - 1] : '';
      if (!isMentionTokenChar(previous) && previous !== '@') {
        return { marker: '@', start, end: boundedCursor };
      }
    }

    // A quoted mention still being typed: `@"meeting notes/ag`.
    const quoted = findOpenQuotedMention(value, boundedCursor);
    if (!quoted) {
      return null;
    }
    return {
      marker: '@',
      start: quoted.start,
      end: boundedCursor,
      query: quoted.query,
    };
  };

  const detectSkillTrigger = (value, cursorPosition) => {
    const boundedCursor = Math.max(0, Math.min(cursorPosition, value.length));

    const fileTrigger = detectFileTrigger(value, boundedCursor);
    if (fileTrigger) {
      return fileTrigger;
    }

    let start = boundedCursor - 1;

    while (start >= 0 && SKILL_TRIGGER_PATTERN.test(value[start])) {
      start -= 1;
    }

    if (start < 0) {
      return null;
    }

    const trigger = value[start];

    if (trigger !== '/' && trigger !== '$') {
      return null;
    }

    if (trigger === '/' && start !== 0) {
      return null;
    }

    if (
      trigger === '$' &&
      start > 0 &&
      SKILL_TRIGGER_PATTERN.test(value[start - 1])
    ) {
      return null;
    }

    for (let index = start + 1; index < boundedCursor; index += 1) {
      if (!SKILL_TRIGGER_PATTERN.test(value[index])) {
        return null;
      }
    }

    return { marker: trigger, start, end: boundedCursor };
  };

  // A chosen file completes the mention; a chosen folder continues it inside
  // that folder with the picker still open.
  const selectFile = async (row) => {
    const path = typeof row?.path === 'string' ? row.path : '';
    if (!triggerContext || !path) {
      return;
    }

    const prefix = context.content.slice(0, triggerContext.start);
    const suffix = context.content.slice(triggerContext.end);
    const isDirectory = row.kind === 'directory';
    // The trailing space ends the mention token, so typing continues normally.
    // Paths outside the bare token grammar are inserted in quoted form, a
    // folder's left open for the rest of the path.
    const insertedToken = isDirectory
      ? formatMentionToken(`${path}/`, { open: true })
      : `${formatMentionToken(path)} `;
    const nextCursorPosition = prefix.length + insertedToken.length;
    context.content = `${prefix}${insertedToken}${suffix}`;
    context.noteContentEdited();
    resetActiveIndex();
    if (isDirectory) {
      triggerContext = {
        marker: '@',
        start: triggerContext.start,
        end: nextCursorPosition,
        ...(insertedToken.startsWith('@"') ? { query: `${path}/` } : {}),
      };
      requestEntries(path, { immediate: true });
    } else {
      triggerContext = null;
      _triggerClosed = true;
    }

    await tick();
    context.inputElement?.focus();
    context.inputElement?.setSelectionRange(
      nextCursorPosition,
      nextCursorPosition,
    );
    context.resizeInput();
  };

  const selectSkill = async (skill) => {
    if (!triggerContext || !skill?.name) {
      return;
    }

    if (
      triggerContext.marker === '/' &&
      skill.type === 'command' &&
      skill.argument === 'none'
    ) {
      context.executeImmediateCommand(skill);
      return;
    }

    const prefix = context.content.slice(0, triggerContext.start);
    const suffix = context.content.slice(triggerContext.end);
    const marker = triggerContext.marker;
    const stripPattern = marker === '/' ? /^\/+/ : /^\$+/;
    const normalizedSkillName = String(skill.name).replace(stripPattern, '');
    if (!normalizedSkillName) {
      return;
    }
    const insertedToken = `${marker}${normalizedSkillName}`;
    const nextCursorPosition = prefix.length + insertedToken.length;
    context.content = `${prefix}${insertedToken}${suffix}`;
    context.noteContentEdited();
    triggerContext = null;
    resetActiveIndex();
    _triggerClosed = true;

    await tick();
    context.inputElement?.focus();
    context.inputElement?.setSelectionRange(
      nextCursorPosition,
      nextCursorPosition,
    );
    context.resizeInput();
  };
  // The file list belongs to one listing (a Session's, or the Project a
  // draft chose); a different one is fetched again on the next `@`.
  function resetFileCandidates() {
    fileCandidates = null;
    fileDirectories = [];
    fileListTruncated = false;
    fileListLoading = false;
    _fileFetchToken += 1;
    resetDirectoryEntries();
  }

  function resetForDraft() {
    triggerContext = null;
    resetActiveIndex();
    _triggerClosed = false;
    // A different session may sit on a different cwd — drop the file list.
    resetFileCandidates();
    // Drop the model catalog so a fresh `/model ` fetches the latest list.
    modelCatalog = null;
    modelCatalogLoading = false;
    _modelCatalogFetchToken += 1;
    showAllModels = false;
  }

  return {
    resetForDraft,
    resetFileCandidates,
    get autocompleteElement() {
      return autocompleteElement;
    },
    set autocompleteElement(value) {
      autocompleteElement = value;
    },
    get fileAutocompleteElement() {
      return fileAutocompleteElement;
    },
    set fileAutocompleteElement(value) {
      fileAutocompleteElement = value;
    },
    get modelAutocompleteElement() {
      return modelAutocompleteElement;
    },
    set modelAutocompleteElement(value) {
      modelAutocompleteElement = value;
    },
    get triggerContext() {
      return triggerContext;
    },
    set triggerContext(value) {
      triggerContext = value;
    },
    // The highlighted row of the open list; assigning it resets the list's
    // highlight, `moveActiveIndex` moves it for the user.
    get activeSkillIndex() {
      return activeIndex;
    },
    set activeSkillIndex(value) {
      activeSkillIndex = value;
      activeIndexMoved = false;
    },
    moveActiveIndex,
    get fileCandidates() {
      return fileCandidates;
    },
    set fileCandidates(value) {
      fileCandidates = value;
    },
    get fileDirectories() {
      return fileDirectories;
    },
    set fileDirectories(value) {
      fileDirectories = value;
    },
    listedEntries,
    get fileRows() {
      return fileRows;
    },
    get fileRowsLoading() {
      return fileRowsLoading;
    },
    get fileRowsTruncated() {
      return fileRowsTruncated;
    },
    get fileListTruncated() {
      return fileListTruncated;
    },
    set fileListTruncated(value) {
      fileListTruncated = value;
    },
    get fileListLoading() {
      return fileListLoading;
    },
    set fileListLoading(value) {
      fileListLoading = value;
    },
    get modelCatalogLoading() {
      return modelCatalogLoading;
    },
    set modelCatalogLoading(value) {
      modelCatalogLoading = value;
    },
    get showAllModels() {
      return showAllModels;
    },
    set showAllModels(value) {
      showAllModels = value;
    },
    get _suppressSelectionUpdate() {
      return _suppressSelectionUpdate;
    },
    set _suppressSelectionUpdate(value) {
      _suppressSelectionUpdate = value;
    },
    get _triggerClosed() {
      return _triggerClosed;
    },
    set _triggerClosed(value) {
      _triggerClosed = value;
    },
    get autocompleteItems() {
      return autocompleteItems;
    },
    set autocompleteItems(value) {
      autocompleteItems = value;
    },
    get autocompleteQuery() {
      return autocompleteQuery;
    },
    set autocompleteQuery(value) {
      autocompleteQuery = value;
    },
    get showSkillAutocomplete() {
      return showSkillAutocomplete;
    },
    set showSkillAutocomplete(value) {
      showSkillAutocomplete = value;
    },
    get showFileAutocomplete() {
      return showFileAutocomplete;
    },
    set showFileAutocomplete(value) {
      showFileAutocomplete = value;
    },
    get modelOptions() {
      return modelOptions;
    },
    set modelOptions(value) {
      modelOptions = value;
    },
    get modelFilterFooter() {
      return modelFilterFooter;
    },
    set modelFilterFooter(value) {
      modelFilterFooter = value;
    },
    get showModelAutocomplete() {
      return showModelAutocomplete;
    },
    set showModelAutocomplete(value) {
      showModelAutocomplete = value;
    },
    get activeAutocompleteElement() {
      return activeAutocompleteElement;
    },
    get activeAutocompleteLoading() {
      return activeAutocompleteLoading;
    },
    get activeMatchCount() {
      return activeMatchCount;
    },
    get updateTriggerContext() {
      return updateTriggerContext;
    },
    get selectFile() {
      return selectFile;
    },
    get selectSkill() {
      return selectSkill;
    },
  };
}
