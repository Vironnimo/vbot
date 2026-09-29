<script>
  // API key of the chosen keyed web service (Web search, Web page reading).
  // The key is write-only: the server reports only whether it is set and
  // where the lookup finds it, and a typed value stays in this component until
  // it is saved. It is never part of a panel's autosave draft or dirty state.
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import { setServiceKey } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    id,
    service,
    dataDirectory = '',
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  // A typed value and an open Replace belong to one variable, so choosing
  // another service never carries a typed key over.
  let entry = $state({ variable: '', value: '' });
  let replacingVariable = $state('');
  let saving = $state(false);

  const variable = $derived(service.api_key_env);
  const typed = $derived(entry.variable === variable ? entry.value : '');
  // A variable in the server's process environment takes precedence over the
  // .env file, so a key saved here could not take effect while it exists.
  const fromEnvironment = $derived(service.source === 'process_environment');
  const replacing = $derived(
    service.configured && replacingVariable === variable,
  );
  const editing = $derived(
    !fromEnvironment && (!service.configured || replacing),
  );
  const description = $derived.by(() => {
    if (fromEnvironment) {
      return service.configured
        ? t('settings.serviceKey.stateEnvironment', { variable })
        : t('settings.serviceKey.stateEnvironmentEmpty', { variable });
    }
    return service.configured
      ? t('settings.serviceKey.stateSaved', { variable })
      : t('settings.serviceKey.stateMissing', { variable });
  });
  const help = $derived(
    [
      t('settings.serviceKey.help'),
      dataDirectory
        ? t('settings.serviceKey.helpFile', { path: dataDirectory })
        : '',
      service.shared ? t('settings.serviceKey.helpShared', { variable }) : '',
    ]
      .filter(Boolean)
      .join('\n\n'),
  );

  function setTyped(value) {
    entry = { variable, value };
  }

  function submit(event) {
    event.preventDefault();
    const value = typed.trim();
    if (value) {
      void save(value);
    }
  }

  // An empty value removes the saved key.
  async function save(value) {
    if (saving) {
      return;
    }
    saving = true;
    onError('');
    try {
      const response = await setServiceKey({ api_key_env: variable, value });
      entry = { variable: '', value: '' };
      replacingVariable = '';
      onCommit(response);
      onToast({
        title: value
          ? t('settings.serviceKey.saveSuccess')
          : t('settings.serviceKey.removeSuccess'),
        variant: 'success',
      });
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
    } finally {
      saving = false;
    }
  }

  function cancelReplace() {
    entry = { variable: '', value: '' };
    replacingVariable = '';
  }
</script>

<div
  class="s-row"
  data-api-key={service.configured ? 'set' : 'missing'}
  data-api-key-source={service.source ?? 'none'}
>
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.serviceKey.label')}
      <InfoHint text={help} />
    </div>
    <div class="s-row-desc">{description}</div>
  </div>
  <form class="s-row-control service-key" onsubmit={submit}>
    <div class="service-key__status">
      <StatusChip variant={service.configured ? 'success' : 'warn'}>
        {service.configured
          ? t('settings.serviceKey.set')
          : t('settings.serviceKey.missing')}
      </StatusChip>
      {#if service.configured && !fromEnvironment && !replacing}
        <Button
          disabled={saving}
          onClick={() => (replacingVariable = variable)}
        >
          {t('settings.serviceKey.replace')}
        </Button>
        <Button variant="danger" disabled={saving} onClick={() => save('')}>
          {t('common.remove')}
        </Button>
      {/if}
    </div>
    {#if editing}
      <div class="service-key__entry">
        <TextField
          {id}
          type="password"
          autocomplete="off"
          value={typed}
          disabled={saving}
          placeholder={t('settings.serviceKey.placeholder')}
          ariaLabel={t('settings.serviceKey.inputLabel', { variable })}
          onInput={setTyped}
        />
        <Button
          variant="primary"
          type="submit"
          disabled={saving || !typed.trim()}
        >
          {saving ? t('common.saving') : t('common.save')}
        </Button>
        {#if replacing}
          <Button disabled={saving} onClick={cancelReplace}>
            {t('common.cancel')}
          </Button>
        {/if}
      </div>
    {/if}
  </form>
</div>

<style>
  .service-key {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-sm);
  }

  .service-key__status,
  .service-key__entry {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .service-key__status {
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .service-key__entry > :global(input) {
    flex: 1 1 auto;
    min-width: 0;
  }
</style>
