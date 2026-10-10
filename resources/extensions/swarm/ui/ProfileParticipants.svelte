<script>
  // The Participants section of the Swarm editor: one group per formation
  // row (its Model, thinking effort, number of participants and, while the
  // Swarm works in a Project with Team agents, the row's Team agent). It
  // edits the editor's bound draft; ProfileEditor owns validation and passes
  // the current field errors in by control id.
  import { tick } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import InfoHint from '../../../../webui/src/components/ui/InfoHint.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
  } from '../../../../webui/src/lib/modelSelection.js';
  import {
    effortOptionsForReasoning,
    reasoningForModelValue,
  } from '../../../../webui/src/lib/agentForm.js';
  import { participantCount } from './pagePresentation.js';

  let { draft = $bindable(), catalog = {}, fieldErrors = {} } = $props();

  let showAllModels = $state(false);

  const models = $derived(catalog.models ?? []);
  const total = $derived(
    draft.participants.reduce((sum, row) => sum + (Number(row.count) || 0), 0),
  );
  // Team agents are the members of the Project the Swarm works in.
  const teamAgents = $derived(
    draft.working_directory.kind === 'project'
      ? ((catalog.projects ?? []).find(
          (project) => project.id === draft.working_directory.project_id,
        )?.agent_profiles ?? [])
      : null,
  );
  const teamAgentOptions = $derived([
    { value: '', label: t('swarm.profile.teamAgentNone') },
    ...(teamAgents ?? []).map((agent) => ({
      value: agent.id,
      label: agent.name,
      secondaryLabel: agent.source,
    })),
  ]);

  function modelOptions(value) {
    return buildModelSelectOptions({
      models,
      modelOnly: true,
      selectedModelValue: value,
      emptyLabel: t('swarm.profile.selectModel'),
    });
  }

  function effortOptions(model) {
    return effortOptionsForReasoning(reasoningForModelValue(model, models)).map(
      (value) => ({
        value,
        label:
          value === ''
            ? t('swarm.profile.providerDefault')
            : t(`agents.form.thinkingEffortOption.${value}`),
      }),
    );
  }

  function selectModel(row, value) {
    row.model = value;
    if (
      reasoningForModelValue(value, models)?.supported === false ||
      !effortOptions(value).some(
        (option) => option.value === (row.thinking_effort ?? ''),
      )
    ) {
      row.thinking_effort = '';
    }
  }

  // A saved Team agent the Project no longer offers stays visible as such.
  function rowTeamAgentOptions(row) {
    const saved = row.repository_profile;
    if (!saved || teamAgentOptions.some((option) => option.value === saved))
      return teamAgentOptions;
    return [
      ...teamAgentOptions,
      {
        value: saved,
        label: saved,
        secondaryLabel: t('swarm.profile.teamAgentUnavailable'),
        disabled: true,
      },
    ];
  }

  function selectTeamAgent(row, value) {
    if (value) row.repository_profile = value;
    else delete row.repository_profile;
  }

  async function focusModel(index) {
    await tick();
    document.getElementById(`swarm-model-${index}`)?.focus();
  }

  function addRow() {
    draft.participants = [
      ...draft.participants,
      { model: '', count: 1, thinking_effort: '' },
    ];
    void focusModel(draft.participants.length - 1);
  }

  // Focus stays in the formation instead of falling back to the page.
  function removeRow(index) {
    draft.participants = draft.participants.filter((_, i) => i !== index);
    void focusModel(Math.min(index, draft.participants.length - 1));
  }

  const describedBy = (id) => (fieldErrors[id] ? `${id}-error` : undefined);
</script>

<section class="s-section" aria-labelledby="swarm-section-participants">
  <header class="s-section__head">
    <h3 class="s-section__title" id="swarm-section-participants">
      {t('swarm.participants')}
    </h3>
    <div class="s-section__aside">
      <span class="swarm-profile-editor__count">{participantCount(total)}</span>
      <Button variant="tertiary" onClick={addRow}>
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg
        >{t('swarm.profile.addModel')}
      </Button>
    </div>
  </header>
  <p class="s-section__desc">{t('swarm.profile.participantsHelp')}</p>
  <div class="s-section__body">
    {#each draft.participants as row, index (index)}
      {@const allOptions = modelOptions(row.model)}
      {@const visibleOptions = filterModelSelectOptions(allOptions, {
        showAll: showAllModels,
        selectedModelValue: row.model,
      })}
      {@const noReasoning =
        reasoningForModelValue(row.model, models)?.supported === false}
      {@const modelId = `swarm-model-${index}`}
      {@const effortId = `swarm-effort-${index}`}
      {@const countId = `swarm-count-${index}`}
      {@const teamAgentId = `swarm-team-agent-${index}`}
      <div class="s-group" data-testid="swarm-formation-row">
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for={modelId}>
              {t('swarm.profile.model')}
            </label>
            {#if fieldErrors[modelId]}
              <p
                class="swarm-profile-editor__error"
                id={`${modelId}-error`}
                role="alert"
              >
                {fieldErrors[modelId]}
              </p>
            {/if}
          </div>
          <div class="s-row-control swarm-profile-editor__model-control">
            <SearchableDropdown
              id={modelId}
              value={row.model}
              options={visibleOptions}
              placeholder={t('swarm.profile.selectModel')}
              ariaLabel={t('swarm.profile.model')}
              ariaDescribedby={describedBy(modelId)}
              searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
              emptyLabel={t('agents.form.modelSearchEmpty')}
              footerActionLabel={modelFilterFooterLabel({
                showAll: showAllModels,
                hiddenCount: allOptions.length - visibleOptions.length,
              })}
              onFooterAction={() => (showAllModels = !showAllModels)}
              onValueChange={(value) => selectModel(row, value)}
            />
            {#if draft.participants.length > 1}
              <Button
                variant="tertiary"
                icon
                class="swarm-profile-editor__remove"
                ariaLabel={t('swarm.profile.removeModel', {
                  number: index + 1,
                })}
                tooltip={t('swarm.profile.remove')}
                onClick={() => removeRow(index)}
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.7"
                  aria-hidden="true"
                  ><path
                    d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"
                  /></svg
                >
              </Button>
            {/if}
          </div>
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label swarm-profile-editor__label">
              <label for={effortId}>{t('agents.form.thinkingEffort')}</label>
              <InfoHint text={t('swarm.profile.effortHelp')} />
            </div>
            {#if noReasoning}
              <div class="s-row-desc" id={`${effortId}-help`}>
                {t('agents.form.thinkingEffortUnsupported')}
              </div>
            {/if}
          </div>
          <div class="s-row-control">
            <Dropdown
              id={effortId}
              value={row.thinking_effort ?? ''}
              options={effortOptions(row.model)}
              ariaLabel={t('agents.form.thinkingEffort')}
              ariaDescribedby={noReasoning ? `${effortId}-help` : undefined}
              disabled={!row.model || noReasoning}
              onValueChange={(value) => (row.thinking_effort = value)}
            />
          </div>
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for={countId}>
              {t('swarm.profile.participants')}
            </label>
            {#if fieldErrors[countId]}
              <p
                class="swarm-profile-editor__error"
                id={`${countId}-error`}
                role="alert"
              >
                {fieldErrors[countId]}
              </p>
            {/if}
          </div>
          <div class="s-row-control s-row-control--number">
            <TextField
              id={countId}
              type="number"
              inputmode="numeric"
              min="1"
              step="1"
              value={row.count}
              invalid={Boolean(fieldErrors[countId])}
              aria-describedby={describedBy(countId)}
              onInput={(value) => (row.count = value)}
            />
          </div>
        </div>
        {#if teamAgents && (teamAgents.length > 0 || row.repository_profile)}
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label swarm-profile-editor__label">
                <label for={teamAgentId}>{t('swarm.profile.teamAgent')}</label>
                <InfoHint text={t('swarm.profile.teamAgentHelp')} />
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id={teamAgentId}
                value={row.repository_profile ?? ''}
                options={rowTeamAgentOptions(row)}
                ariaLabel={t('swarm.profile.teamAgent')}
                onValueChange={(value) => selectTeamAgent(row, value)}
              />
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </div>
</section>
