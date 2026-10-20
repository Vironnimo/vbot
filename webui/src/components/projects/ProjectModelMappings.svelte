<script>
  // Maps the Model names repository Agent files ask for to configured vBot
  // Models; an unmapped name uses the Project default.
  import { t } from '$lib/i18n.js';
  import { buildModelSelectOptions } from '$lib/modelSelection.js';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';

  let { projectsState, projectsController } = $props();
  let wish = $state('');
  let target = $state('');

  let modelOptions = $derived(
    buildModelSelectOptions({
      models: projectsState.availableModels,
      connections: projectsState.availableConnections,
      selectedModelValue: '',
      emptyLabel: t('projects.sources.modelUnmapped'),
    }),
  );
  let mappings = $derived(
    Object.entries(projectsState.editForm.model_mappings),
  );
  function mapModel(name, value) {
    const next = { ...projectsState.editForm.model_mappings };
    if (value) next[name] = value;
    else delete next[name];
    projectsController.updateEditField('model_mappings', next);
  }
</script>

<div class="s-group">
  <details class="s-disclosure projects-model-mappings">
    <summary>
      {t('projects.sources.modelMappings')}
      {#if mappings.length > 0}
        <span class="s-disclosure__meta">{mappings.length}</span>
      {/if}
    </summary>
    <div class="s-group__rows">
      <p class="s-group__note projects-model-mappings__note">
        {t('projects.sources.modelHelp')}
      </p>
      {#each mappings as [name, value] (name)}
        <div class="s-row">
          <div class="s-row-info">
            <span class="s-row-label">{name}</span>
          </div>
          <div class="s-row-control projects-model-mappings__control">
            <SearchableDropdown
              {value}
              options={modelOptions}
              ariaLabel={name}
              onValueChange={(next) => mapModel(name, next)}
            />
            <Button variant="tertiary" onClick={() => mapModel(name, '')}
              >{t('common.remove')}</Button
            >
          </div>
        </div>
      {/each}
      <div class="s-row">
        <div class="s-row-info">
          <TextField
            value={wish}
            aria-label={t('projects.sources.modelWish')}
            placeholder={t('projects.sources.modelWish')}
            onInput={(value) => (wish = value)}
          />
        </div>
        <div class="s-row-control projects-model-mappings__control">
          <SearchableDropdown
            value={target}
            options={modelOptions.filter((option) => option.value)}
            ariaLabel={t('projects.sources.modelTarget')}
            onValueChange={(value) => (target = value)}
          />
          <Button
            disabled={!wish.trim() || !target}
            onClick={() => {
              mapModel(wish.trim(), target);
              wish = '';
              target = '';
            }}>{t('projects.sources.addMapping')}</Button
          >
        </div>
      </div>
    </div>
  </details>
</div>
