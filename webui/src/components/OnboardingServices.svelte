<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    applyManagedService,
    getManagedService,
    getSettings,
  } from '$lib/api.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import SettingsWebSearchPanel from './settings/SettingsWebSearchPanel.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Checkbox from './ui/Checkbox.svelte';

  let settings = $state(null);
  let service = $state(null);
  let loading = $state(true);
  let applying = $state(false);
  let allowPrerequisites = $state(false);
  let error = $state('');
  let disposed = false;
  let timer;
  const autosave = useAutosaveContext();

  onMount(() => {
    void load();
  });
  onDestroy(() => {
    disposed = true;
    clearTimeout(timer);
  });

  async function load() {
    clearTimeout(timer);
    try {
      const [next, configuration] = await Promise.all([
        getManagedService(),
        getSettings(),
      ]);
      if (disposed) return;
      if (service?.plan?.id !== next.plan?.id) allowPrerequisites = false;
      service = next;
      settings = configuration;
      error = '';
      if (next.state === 'running') timer = setTimeout(load, 1500);
    } catch (failure) {
      if (!disposed) error = failure.message;
    } finally {
      if (!disposed) loading = false;
    }
  }

  async function apply(action) {
    if (applying) return;
    applying = true;
    error = '';
    try {
      const result = await applyManagedService(action, {
        ...(['install', 'update'].includes(action)
          ? {
              plan_id: service.plan.id,
              allow_prerequisites: allowPrerequisites,
            }
          : {}),
      });
      if (!disposed) {
        service = result;
        void load();
      }
    } catch (failure) {
      if (!disposed) error = failure.message;
    } finally {
      if (!disposed) applying = false;
    }
  }

  function requestAction(action) {
    void autosave.requestTransition(() => apply(action));
  }
</script>

<div class="onboarding-services">
  {#if error}<Banner variant="error" role="alert"
      >{error}<Button variant="secondary" onClick={load}
        >{t('onboarding.retry')}</Button
      ></Banner
    >{/if}
  {#if loading}<p role="status">{t('common.loading')}</p>{/if}
  {#if service}
    <h4>{service.name}</h4>
    <p>{t('onboarding.services.description')}</p>
    {#if service.message}<p role="status">{service.message}</p>{/if}
    {#if service.error}<Banner variant="error" role="alert"
        >{service.error}</Banner
      >{/if}
    {#if service.state === 'running'}
      <p role="status">
        <span class="s-inline-spinner" aria-hidden="true"></span>
        {service.phase}
      </p>
      <p class="onboarding-help">{t('onboarding.services.background')}</p>
    {:else if service.available}
      {#if service.plan?.steps?.length}
        <ul>
          {#each service.plan.steps as item (item.id)}<li>
              {item.label}
            </li>{/each}
        </ul>
      {/if}
      {#if service.plan?.requires_prerequisites}
        <Checkbox
          class="onboarding-prerequisites"
          checked={allowPrerequisites}
          onChange={(checked) => (allowPrerequisites = checked)}
          disabled={applying}>{t('onboarding.services.prerequisites')}</Checkbox
        >
      {/if}
      <div class="onboarding-service-actions">
        {#if !service.installed || service.state === 'action_required' || service.state === 'failed'}
          <Button
            variant="primary"
            disabled={applying ||
              !service.plan?.can_install ||
              (service.plan?.requires_prerequisites && !allowPrerequisites)}
            onClick={() => requestAction('install')}
            >{t('onboarding.services.install')}</Button
          >
        {:else}
          <Button
            variant="secondary"
            disabled={applying ||
              (service.plan?.requires_prerequisites && !allowPrerequisites)}
            onClick={() => requestAction('update')}
            >{t('onboarding.services.update')}</Button
          >
          {#if service.actions?.includes('start')}<Button
              variant="secondary"
              disabled={applying}
              onClick={() => requestAction('start')}
              >{t('onboarding.services.start')}</Button
            >{/if}
          {#if service.actions?.includes('stop')}<Button
              variant="secondary"
              disabled={applying}
              onClick={() => requestAction('stop')}
              >{t('onboarding.services.stop')}</Button
            >{/if}
          {#if service.actions?.includes('remove')}<Button
              variant="tertiary"
              disabled={applying}
              onClick={() => requestAction('remove')}
              >{t('onboarding.services.remove')}</Button
            >{/if}
        {/if}
      </div>
    {/if}
  {/if}
  {#if settings}
    <details class="onboarding-search-settings">
      <summary>{t('onboarding.services.other')}</summary>
      <SettingsWebSearchPanel
        {settings}
        onCommit={(next) => (settings = next)}
        onError={(failure) => (error = failure.message ?? String(failure))}
      />
    </details>
  {/if}
</div>
