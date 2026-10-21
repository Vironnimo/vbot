<script>
  // The composer's footer row: the Project the displayed conversation works
  // in (left), and the Model and thinking effort it uses (right). The view
  // model comes from `view/sessionSettings.svelte.js`; this component only
  // presents it and reports choices.
  //
  // - Project: a picker in a draft of an Identity Agent; afterwards, and for
  //   a Project team Agent, a read-only name.
  // - Agent: in a draft only, the Agents that can work in its Project; a
  //   choice moves the draft to that Agent.
  // - Model: the Session's concrete choice, initially copied from the Agent;
  //   the row names the Model by its catalog name, the tooltip by its id.
  // - Thinking effort: Provider default or a level the effective Model
  //   offers; hidden for a Model without reasoning.
  // - Output speed and request latency: independent server projections,
  //   each identifying the Model step that supplied its measurement.
  import {
    THINKING_EFFORT_OPTIONS,
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '$lib/agentForm.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelIdParts,
    modelSelectionParts,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import { formatDurationMs } from '$lib/chatTimelinePresentation/time.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import SessionPermissionsBadge from '../permissions/SessionPermissionsBadge.svelte';

  const noop = () => {};

  let {
    view,
    // The Session's newest measured `usage.speed`; null hides it.
    outputSpeed = null,
    outputLatency = null,
    projects = [],
    // Until the Project list is read, a Project missing from it is unknown.
    projectsLoaded = false,
    onSelectProject = noop,
    onSelectAgent = noop,
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

  // --- Agent -----------------------------------------------------------

  let agentOptions = $derived.by(() => {
    if (!view.agent) {
      return [];
    }
    const options = view.agent.choices.map((choice) => ({
      value: choice.address,
      label: choice.name,
      secondaryLabel: choice.projectId ? projectName(choice.projectId) : '',
    }));
    if (!options.some((option) => option.value === view.agent.value)) {
      options.unshift({ value: view.agent.value, label: view.agent.value });
    }
    return options;
  });
  let agentTooltip = $derived({
    title:
      agentOptions.find((option) => option.value === view.agent?.value)
        ?.label ?? t('chat.sessionSettings.agent'),
    text: t('chat.sessionSettings.agentDraftHint'),
    placement: 'top',
  });

  // --- Model -----------------------------------------------------------

  // A Connection pinned by the Agent's Model shows in the tooltip, not the
  // label.
  let allModelOptions = $derived.by(() => {
    if (!view.catalog) {
      // Until the catalog answers, offer what is known without calling a
      // chosen Model unavailable.
      const { model } = parseModelSelectionValue(view.model);
      return view.model
        ? [
            {
              value: view.model,
              label: model,
              labelLead: modelIdParts(model).lead,
            },
          ]
        : [];
    }
    // Canonical `provider/model` values: the Session leaves Connection
    // routing to the Runtime. `model.list` lists only usable Models.
    const [, ...catalogOptions] = buildModelSelectOptions({
      models,
      connections,
      modelOnly: true,
      selectedModelValue: view.model,
    });
    return catalogOptions;
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
  // The quiet row shows a Model's catalog name (its id until the catalog
  // answers); the list keeps the ids it is searched by.
  let modelNames = $derived(
    new Map(models.map((model) => [model.id, model.name])),
  );
  let modelPickerOptions = $derived(
    modelOptions.map((option) => {
      const value = option.value || view.defaultModel;
      if (!value) return option;
      const { model } = parseModelSelectionValue(value);
      const name = modelNames.get(model);
      // Without a catalog name, an option already labelled with the id shows
      // it as is, its provider path muted.
      if (!name && option.labelLead) return option;
      return { ...option, triggerLabel: name || model };
    }),
  );
  let modelTooltip = $derived.by(() => {
    const value = view.model || view.defaultModel;
    const text = t('chat.sessionSettings.modelHint');
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
        },
        ...(parts.connection
          ? [{ label: t('agents.details.connection'), value: parts.connection }]
          : []),
      ],
      placement: 'top',
    };
  });

  // --- Output speed ----------------------------------------------------

  let speedLabel = $derived.by(() => {
    const rate = outputSpeed?.output_tokens_per_second;
    if (!Number.isFinite(rate) || rate <= 0) return '';
    const label = t('chat.sessionSettings.speedValue', {
      rate: new Intl.NumberFormat(activeLocaleTag(), {
        maximumFractionDigits: rate < 10 ? 1 : 0,
        minimumFractionDigits: rate < 10 ? 1 : 0,
      }).format(rate),
    });
    return outputSpeed.basis === 'provider'
      ? label
      : t('chat.sessionSettings.approximateSpeed', { value: label });
  });
  let speedTooltip = $derived.by(() => {
    if (!speedLabel) return null;
    const speed = outputSpeed;
    const rows = [
      {
        label: t('chat.sessionSettings.speedRate'),
        value: speed.output_tokens_per_second.toFixed(1),
      },
    ];
    // The Model that answered; the footer may already name another one.
    if (speed.model) {
      const { model } = parseModelSelectionValue(speed.model);
      rows.push({
        label: t('chat.sessionSettings.model'),
        value: modelNames.get(model) || model,
      });
    }
    if (Number.isFinite(speed.tokens) && Number.isFinite(speed.window_ms)) {
      const tokens = speed.tokens;
      const duration = formatDurationMs(speed.window_ms);
      rows.push({
        label: t('chat.sessionSettings.speedMeasured'),
        value: speed.estimated
          ? t('chat.sessionSettings.speedEstimatedValue', { tokens, duration })
          : t('chat.sessionSettings.speedMeasuredValue', { tokens, duration }),
      });
    }
    const notes = [t('chat.sessionSettings.speedHint')];
    if (speed.basis === 'answer') {
      notes.push(t('chat.sessionSettings.speedAnswerOnly'));
    } else if (speed.basis === 'provider') {
      notes.push(t('chat.sessionSettings.speedReported'));
    } else {
      notes.push(t('chat.sessionSettings.speedStreamedOutput'));
    }
    return {
      title: t('chat.sessionSettings.speed'),
      text: notes.join(' '),
      rows,
      placement: 'top',
    };
  });

  let latencyLabel = $derived(
    Number.isFinite(outputLatency?.first_token_ms)
      ? t('chat.sessionSettings.latencyValue', {
          duration: formatDurationMs(outputLatency.first_token_ms),
        })
      : '',
  );
  let latencyTooltip = $derived.by(() => {
    if (!latencyLabel) return null;
    const latency = outputLatency;
    const rows = [];
    if (latency.model) {
      const { model } = parseModelSelectionValue(latency.model);
      rows.push({
        label: t('chat.sessionSettings.model'),
        value: modelNames.get(model) || model,
      });
    }
    rows.push({
      label: t('chat.sessionSettings.firstToken'),
      value: formatDurationMs(latency.first_token_ms),
    });
    if (
      Number.isFinite(latency.first_answer_ms) &&
      latency.first_answer_ms > latency.first_token_ms
    ) {
      rows.push({
        label: t('chat.sessionSettings.firstAnswer'),
        value: formatDurationMs(latency.first_answer_ms),
      });
    }
    return {
      title: t('chat.sessionSettings.latency'),
      text: t('chat.sessionSettings.latencyHint'),
      rows,
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
        label: t('chat.sessionSettings.providerDefault'),
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
    text: t('chat.sessionSettings.effortHint'),
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
    {#if view.agent}
      <span class="session-settings__field session-settings__field--icon">
        <svg
          class="session-settings__icon"
          viewBox="0 0 16 16"
          width="13"
          height="13"
          aria-hidden="true"
        >
          <circle cx="8" cy="5.5" r="2.5" />
          <path d="M3 13.5a5 5 0 0 1 10 0" />
        </svg>
        <Dropdown
          value={view.agent.value}
          options={agentOptions}
          ariaLabel={t('chat.sessionSettings.agent')}
          triggerClass="session-settings__picker"
          triggerTooltip={agentTooltip}
          panelMinWidth={200}
          onValueChange={(next) => onSelectAgent(next)}
        />
      </span>
    {/if}
    <SessionPermissionsBadge
      agentAddress={view.agentAddress}
      sessionId={view.sessionId}
      workingProjectId={view.kind === 'draft' && !view.project.team
        ? (view.project.value ?? null)
        : undefined}
    />
    {#if speedLabel || latencyLabel}
      <span class="session-settings__field session-settings__field--icon">
        <svg
          class="session-settings__icon"
          viewBox="0 0 16 16"
          width="13"
          height="13"
          aria-hidden="true"
        >
          <path d="M2.5 11.5a5.5 5.5 0 1 1 11 0" />
          <path d="M8 11.5l2.5-3.5" />
        </svg>
        {#if speedLabel}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex (The measurement details are keyboard-accessible.) -->
          <span
            class="session-settings__value session-settings__value--speed"
            role="group"
            tabindex="0"
            aria-label={t('chat.sessionSettings.speed')}
            use:tooltip={speedTooltip}
          >
            <span class="session-settings__value-label">{speedLabel}</span>
          </span>
        {/if}
        {#if latencyLabel}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex (The latency details are keyboard-accessible.) -->
          <span
            class="session-settings__value session-settings__value--speed"
            role="group"
            tabindex="0"
            aria-label={t('chat.sessionSettings.latency')}
            use:tooltip={latencyTooltip}
          >
            <span class="session-settings__value-label">{latencyLabel}</span>
          </span>
        {/if}
      </span>
    {/if}
  </div>
  <div class="session-settings__end">
    <SaveStatus
      saving={view.saving}
      pending={view.pending}
      onClick={view.save}
    />
    <span
      class="session-settings__field session-settings__field--model"
      class:session-settings__field--override={Boolean(view.model)}
    >
      <SearchableDropdown
        value={modelValue}
        options={modelPickerOptions}
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
          <path
            d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
          />
          <path d="M6 13h4" />
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

  /* The Model name takes the room the row has and truncates only without it. */
  .session-settings__field--model {
    flex: 0 1 auto;
  }

  .session-settings__field--model :global(.session-settings__picker) {
    max-width: 100%;
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

  .session-settings__value--speed {
    font-variant-numeric: tabular-nums;
  }

  .session-settings__field--icon
    .session-settings__value--speed
    + .session-settings__value--speed {
    padding-left: 6px;
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
