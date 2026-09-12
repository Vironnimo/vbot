import { fuzzyFilterFiles, isMentionTokenChar } from '$lib/fileMentions.js';
import { t } from '$lib/i18n.js';
import {
  buildModelSelectOptions,
  filterModelSelectOptions,
  modelFilterFooterLabel,
} from '$lib/modelSelection.js';
import { tick } from 'svelte';

export function createComposerPicker(context) {
  const SKILL_TRIGGER_PATTERN = /[A-Za-z0-9_-]/u;

  // Mirrors FileAutocomplete's render cap so keyboard navigation and the
  // rendered list can never disagree on the match set.
  const MAX_FILE_MATCHES = 50;

  let autocompleteElement = $state(null);

  let fileAutocompleteElement = $state(null);

  let modelAutocompleteElement = $state(null);

  let triggerContext = $state(null);

  let activeSkillIndex = $state(0);

  // @-mention picker data: `null` = never fetched for this session. Fetched
  // once per picker open (fresh list, no cache-invalidation problem) and reused
  // at submit to decide which @-tokens are real files.
  let fileCandidates = $state(null);

  let fileListTruncated = $state(false);

  let fileListLoading = $state(false);

  let _fileFetchToken = 0;

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

    return context.content.slice(triggerContext.start + 1, triggerContext.end);
  });

  let matchingFiles = $derived.by(() =>
    triggerContext?.marker === '@'
      ? fuzzyFilterFiles(
          fileCandidates ?? [],
          autocompleteQuery,
          MAX_FILE_MATCHES,
        )
      : [],
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
      (fileListLoading || matchingFiles.length > 0),
  );

  let allModelOptions = $derived.by(() => {
    if (!modelCatalog) {
      return [];
    }
    return buildModelSelectOptions({
      models: modelCatalog.models,
      connections: modelCatalog.connections,
      translate: t,
    }).filter((option) => option.value !== '');
  });

  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, { showAll: showAllModels }),
  );

  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
      translate: t,
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

  const activeMatchCount = () => {
    if (triggerContext?.marker === '@') {
      return matchingFiles.length;
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
      activeSkillIndex = 0;
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
    activeSkillIndex = 0;

    // Reset show-all when leaving the model trigger.
    if (
      previousContext?.marker === 'model' &&
      triggerContext?.marker !== 'model'
    ) {
      showAllModels = false;
    }

    // A newly opened @-picker (or the caret jumping to a different @-token)
    // fetches a fresh file list; typing within the same token filters locally.
    if (
      triggerContext?.marker === '@' &&
      (previousContext?.marker !== '@' ||
        previousContext.start !== triggerContext.start)
    ) {
      refreshFileCandidates();
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
    if (typeof context.onListFiles !== 'function') {
      fileCandidates = [];
      fileListTruncated = false;
      return;
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
      fileCandidates = Array.isArray(result?.files) ? result.files : [];
      fileListTruncated = Boolean(result?.truncated);
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

  const detectFileTrigger = (value, boundedCursor) => {
    let start = boundedCursor - 1;

    while (start >= 0 && isMentionTokenChar(value[start])) {
      start -= 1;
    }

    if (start < 0 || value[start] !== '@') {
      return null;
    }

    if (start > 0) {
      const previous = value[start - 1];
      if (isMentionTokenChar(previous) || previous === '@') {
        return null;
      }
    }

    return { marker: '@', start, end: boundedCursor };
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

  const selectFile = async (file) => {
    if (!triggerContext || typeof file !== 'string' || !file) {
      return;
    }

    const prefix = context.content.slice(0, triggerContext.start);
    const suffix = context.content.slice(triggerContext.end);
    // The trailing space ends the mention token, so typing continues normally.
    const insertedToken = `@${file} `;
    const nextCursorPosition = prefix.length + insertedToken.length;
    context.content = `${prefix}${insertedToken}${suffix}`;
    context.noteContentEdited();
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;

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
    activeSkillIndex = 0;
    _triggerClosed = true;

    await tick();
    context.inputElement?.focus();
    context.inputElement?.setSelectionRange(
      nextCursorPosition,
      nextCursorPosition,
    );
    context.resizeInput();
  };
  function resetForDraft() {
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = false;
    // A different session may sit on a different cwd — drop the file list.
    fileCandidates = null;
    fileListTruncated = false;
    fileListLoading = false;
    _fileFetchToken += 1;
    // Drop the model catalog so a fresh `/model ` fetches the latest list.
    modelCatalog = null;
    modelCatalogLoading = false;
    _modelCatalogFetchToken += 1;
    showAllModels = false;
  }

  return {
    resetForDraft,
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
    get activeSkillIndex() {
      return activeSkillIndex;
    },
    set activeSkillIndex(value) {
      activeSkillIndex = value;
    },
    get fileCandidates() {
      return fileCandidates;
    },
    set fileCandidates(value) {
      fileCandidates = value;
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
