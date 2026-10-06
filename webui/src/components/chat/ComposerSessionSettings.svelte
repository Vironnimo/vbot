<script>
  // The composer's footer row: the Project the displayed conversation works
  // in (left), and the Model and thinking effort it uses (right). The view
  // model comes from `view/sessionSettings.svelte.js`; this component only
  // presents it and reports choices.
  //
  // - Project: a picker in a draft of an Identity Agent; afterwards, and for
  //   a Project team Agent, a read-only name.
  // - Model: "Agent default" (naming the Agent's Model) or a catalog Model.
  // - Thinking effort: the Agent's effort or a level the effective Model
  //   offers; hidden for a Model without reasoning.
  import {
    THINKING_EFFORT_OPTIONS,
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionParts,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';

  const noop = () => {};

  let {
    view,
    projects = [],
    // Until the Project list is read, a Project missing from it is unknown.
    projectsLoaded = false,
    onSelectProject = noop,
    onSelectModel = noop,
    onSelectThinkingEffort = noop,
    onModelPickerOpen = noop,
  } = $props();

  let showAllModels = $state(false);

  let models = $derived(view.catalog?.models ?? []);
  let connections = $derived(view.catalog?.connections ?? []);

  // --- Project ---------------------------------------------------------

  let projectValue = $derived(view.project.value ?? null);
  let projectListed = $derived(
    !projectValue ||
      projects.some((project) => project.project_id === projectValue),
  );
  // Only a loaded list can say a Project is no longer registered.
  let projectMissing = $derived(
    view.project.known &&
      !view.project.team &&
      projectsLoaded &&
      !projectListed,
  );
  // A draft's own choice reads like a Model or effort override.
  let projectChosen = $derived(
    view.project.editable &&
      projectValue !== (view.project.defaultValue ?? null),
  );
  let projectLabel = $derived(projectName(projectValue));
  let projectOptions = $derived.by(() => {
    const defaultValue = view.project.defaultValue ?? null;
    const agentDefault = t('chat.sessionSettings.agentDefault');
    const options = [
      {
        value: '',
        label: t('chat.sessionSettings.workspace'),
        secondaryLabel: defaultValue === null ? agentDefault : '',
      },
      ...projects.map((project) => ({
        value: project.project_id,
        label: project.display_name || project.project_id,
        secondaryLabel: project.project_id === defaultValue ? agentDefault : '',
      })),
    ];
    if (!projectListed) {
      // Not (yet) listed: shown by id, marked unavailable once the list says
      // so.
      options.push({
        value: projectValue,
        label: projectValue,
        secondaryLabel: projectMissing
          ? t('chat.sessionSettings.projectUnavailable')
          : '',
        disabled: projectMissing,
      });
    }
    return options;
  });
  let projectTooltip = $derived({
    title: projectLabel,
    text: projectHint(),
    placement: 'top',
  });

  function projectName(projectId) {
    if (!projectId) {
      return t('chat.sessionSettings.workspace');
    }
    const project = projects.find(
      (candidate) => candidate.project_id === projectId,
    );
    return project?.display_name || projectId;
  }

  function projectHint() {
    if (view.project.team) {
      return t('chat.sessionSettings.teamProjectHint');
    }
    if (projectMissing) {
      return t('chat.sessionSettings.projectMissing');
    }
    return view.project.editable
      ? t('chat.sessionSettings.projectDraftHint')
      : t('chat.sessionSettings.projectFixedHint');
  }

  // --- Model -----------------------------------------------------------

  // A Connection pinned by the Agent's Model shows in the tooltip, not the
  // label.
  let defaultModelName = $derived(
    parseModelSelectionValue(view.defaultModel).model,
  );
  let modelDefaultOption = $derived({
    value: '',
    label: defaultModelName || t('chat.sessionSettings.agentDefault'),
    secondaryLabel: defaultModelName
      ? t('chat.sessionSettings.agentDefault')
      : '',
    code: Boolean(defaultModelName),
  });
  let allModelOptions = $derived.by(() => {
    if (!view.catalog) {
      // Until the catalog answers, offer what is known without calling a
      // chosen Model unavailable.
      const { model } = parseModelSelectionValue(view.model);
      return view.model
        ? [modelDefaultOption, { value: view.model, label: model, code: true }]
        : [modelDefaultOption];
    }
    // Canonical `provider/model` values: the Session leaves Connection
    // routing to the Runtime. `model.list` lists only usable Models.
    const [, ...catalogOptions] = buildModelSelectOptions({
      models,
      connections,
      modelOnly: true,
      selectedModelValue: view.model,
    });
    return [modelDefaultOption, ...catalogOptions];
  });
  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: view.model,
    }),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
    }),
  );
  let modelValue = $derived(selectModelValue(view.model, modelOptions));
  let modelTooltip = $derived.by(() => {
    const value = view.model || view.defaultModel;
    const text = view.model
      ? t('chat.sessionSettings.modelOverrideHint')
      : t('chat.sessionSettings.modelDefaultHint');
    if (!value) {
      return { title: t('chat.sessionSettings.model'), text, placement: 'top' };
    }
    const parts = modelSelectionParts(value, connections);
    return {
      text,
      rows: [
        {
          label: t('chat.sessionSettings.model'),
          value: parts.model,
          mono: true,
        },
        ...(parts.connection
          ? [{ label: t('agents.details.connection'), value: parts.connection }]
          : []),
      ],
      placement: 'top',
    };
  });

  // --- Thinking effort -------------------------------------------------

  let effectiveModel = $derived(view.model || view.defaultModel);
  let reasoning = $derived(reasoningForModelValue(effectiveModel, models));
  // Shown once the catalog says whether the effective Model reasons (or
  // could not say); a Model without reasoning has no effort to steer.
  let effortVisible = $derived(
    Boolean(effectiveModel) &&
      (Boolean(view.catalog) || view.catalogFailed) &&
      reasoning?.supported !== false,
  );
  let effortOptions = $derived.by(() => {
    const levels = effortOptionsForReasoning(reasoning).filter(Boolean);
    const options = [
      {
        value: '',
        label: view.defaultThinkingEffort
          ? effortLabel(view.defaultThinkingEffort)
          : t('chat.sessionSettings.providerDefault'),
        secondaryLabel: view.defaultThinkingEffort
          ? t('chat.sessionSettings.agentDefault')
          : '',
      },
      ...levels.map((level) => ({ value: level, label: effortLabel(level) })),
    ];
    if (view.thinkingEffort && !levels.includes(view.thinkingEffort)) {
      options.push({
        value: view.thinkingEffort,
        label: effortLabel(view.thinkingEffort),
        disabled: true,
      });
    }
    return options;
  });
  let effortTooltip = $derived({
    title: t('chat.sessionSettings.thinkingEffort'),
    text: view.thinkingEffort
      ? t('chat.sessionSettings.effortOverrideHint')
      : t('chat.sessionSettings.effortDefaultHint'),
    placement: 'top',
  });

  function effortLabel(level) {
    return level && THINKING_EFFORT_OPTIONS.includes(level)
      ? t(`agents.form.thinkingEffortOption.${level}`)
      : level;
  }
</script>

<div
  class="session-settings"
  role="group"
  aria-label={t('chat.sessionSettings.label')}
>
  <div class="session-settings__start">
    {#if view.project.editable || view.project.known}
      <span
        class="session-settings__field session-settings__field--icon"
        class:session-settings__field--override={projectChosen}
        class:session-settings__field--warning={projectMissing}
      >
        <svg
          class="session-settings__icon"
          viewBox="0 0 16 16"
          width="13"
          height="13"
          aria-hidden="true"
        >
          <path
            d="M2 4.5V12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H8L6.5 3H3a1 1 0 0 0-1 1.5z"
          />
        </svg>
        {#if view.project.editable}
          <Dropdown
            value={projectValue ?? ''}
            options={projectOptions}
            ariaLabel={t('chat.sessionSettings.project')}
            triggerClass="session-settings__picker"
            triggerTooltip={projectTooltip}
            panelMinWidth={200}
            onValueChange={(next) => onSelectProject(next)}
          />
        {:else}
          <span
            class="session-settings__value"
            role="group"
            aria-label={t('chat.sessionSettings.project')}
            use:tooltip={projectTooltip}
          >
            <span class="session-settings__value-label">{projectLabel}</span>
          </span>
        {/if}
      </span>
    {/if}
  </div>
  <div class="session-settings__end">
    <span
      class="session-settings__field session-settings__field--model"
      class:session-settings__field--override={Boolean(view.model)}
    >
      <SearchableDropdown
        value={modelValue}
        options={modelOptions}
        ariaLabel={t('chat.sessionSettings.model')}
        searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
        emptyLabel={t('agents.form.modelSearchEmpty')}
        triggerClass="session-settings__picker"
        triggerTooltip={modelTooltip}
        panelMinWidth={300}
        footerActionLabel={modelFilterFooter}
        onFooterAction={() => (showAllModels = !showAllModels)}
        onOpenChange={(open) => {
          if (open) {
            onModelPickerOpen();
          }
        }}
        onValueChange={(next) => onSelectModel(next)}
      />
    </span>
    {#if effortVisible}
      <span
        class="session-settings__field session-settings__field--icon"
        class:session-settings__field--override={Boolean(view.thinkingEffort)}
      >
        <svg
          class="session-settings__icon"
          viewBox="0 0 16 16"
          width="13"
          height="13"
          aria-hidden="true"
        >
          <path d="M2.5 11.5a5.5 5.5 0 1 1 11 0M8 11.5l2.5-3.5" />
        </svg>
        <Dropdown
          value={view.thinkingEffort}
          options={effortOptions}
          ariaLabel={t('chat.sessionSettings.thinkingEffort')}
          triggerClass="session-settings__picker"
          triggerTooltip={effortTooltip}
          panelMinWidth={170}
          onValueChange={(next) => onSelectThinkingEffort(next)}
        />
      </span>
    {/if}
  </div>
</div>

<style>
  /* A quiet row under the input box: transparent triggers in small muted
     text, a neutral surface on hover, the shared focus ring. A Session's own
     choice reads in full-strength text; the Agent's default stays muted. */
  /* When the row runs out of width, Model and effort wrap below the
     Project, still right-aligned; within them the Model id truncates first. */
  .session-settings {
    display: flex;
    min-width: 0;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 2px 8px;
    padding: 4px 2px 0;
  }

  .session-settings__start,
  .session-settings__end {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 2px;
  }

  .session-settings__start {
    flex: 0 1 auto;
  }

  .session-settings__end {
    flex: 0 1 auto;
    justify-content: flex-end;
    margin-left: auto;
  }

  .session-settings__field {
    position: relative;
    display: flex;
    min-width: 0;
    align-items: center;
    color: var(--text-lo);
  }

  .session-settings__end .session-settings__field--icon {
    flex-shrink: 0;
  }

  .session-settings__field--override {
    color: var(--text-hi);
  }

  .session-settings__field--warning {
    color: var(--amber);
  }

  /* The icon sits over the trigger's start, above its hover and open
     background; clicks pass through to it. */
  .session-settings__icon {
    position: absolute;
    z-index: 1;
    left: 7px;
    flex-shrink: 0;
    fill: none;
    pointer-events: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.3;
  }

  .session-settings__field :global(.session-settings__picker) {
    width: auto;
    min-width: 0;
    max-width: 240px;
  }

  .session-settings__field--model :global(.session-settings__picker) {
    max-width: 300px;
  }

  .session-settings__field :global(.session-settings__picker .dropdown-trigger),
  .session-settings__field
    :global(.session-settings__picker .s-dropdown-trigger),
  .session-settings__value {
    min-height: 26px;
    gap: 4px;
    padding: 2px 6px;
    border: 1px solid transparent;
    border-radius: var(--r-md);
    color: inherit;
    background: transparent;
    font-size: var(--fs-label-sm);
    line-height: 1.4;
  }

  .session-settings__field--icon
    :global(.session-settings__picker .dropdown-trigger),
  .session-settings__field--icon .session-settings__value {
    padding-left: 23px;
  }

  .session-settings__field
    :global(.session-settings__picker .dropdown-trigger:hover:not(:disabled)),
  .session-settings__field
    :global(.session-settings__picker .s-dropdown-trigger:hover:not(:disabled)),
  .session-settings__field
    :global(.session-settings__picker.open .dropdown-trigger),
  .session-settings__field
    :global(.session-settings__picker.open .s-dropdown-trigger) {
    border-color: transparent;
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .session-settings__field--icon:has(
      :global(.dropdown-trigger:hover:not(:disabled))
    )
    .session-settings__icon,
  .session-settings__field--icon:has(:global(.session-settings__picker.open))
    .session-settings__icon {
    color: var(--text-hi);
  }

  .session-settings__field
    :global(.session-settings__picker .searchable-dropdown__label--code) {
    font-size: var(--fs-mono-sm);
  }

  .session-settings__field
    :global(.session-settings__picker .dropdown-chevron) {
    color: currentColor;
    opacity: 0.7;
  }

  .session-settings__value {
    display: flex;
    min-width: 0;
    max-width: 240px;
    align-items: center;
    cursor: default;
  }

  .session-settings__value-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 640px) {
    .session-settings__field :global(.session-settings__picker),
    .session-settings__value {
      max-width: 150px;
    }

    .session-settings__field--model :global(.session-settings__picker) {
      max-width: 170px;
    }
  }
</style>
