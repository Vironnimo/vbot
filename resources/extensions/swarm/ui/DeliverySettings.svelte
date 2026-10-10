<script>
  // How Board messages reach participants, edited as one `delivery` value:
  // for each route (main discussion, joined discussions, direct mentions)
  // when its messages are received and whether a message starts an idle
  // participant, plus the batching limits behind one disclosure row. A Swarm
  // and a Run's live communication settings share it; the caller owns saving.
  // `onChange(next)` receives the complete next value. Number fields report
  // their text as entered, so callers normalize numbers at their payload
  // boundary. `idPrefix` keeps the element ids of several instances apart.
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import InfoHint from '../../../../webui/src/components/ui/InfoHint.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';

  let { value, onChange, disabled = false, idPrefix } = $props();

  let advancedOpen = $state(false);

  const routes = $derived([
    { id: 'main', label: t('swarm.delivery.route.main') },
    { id: 'discussion', label: t('swarm.delivery.route.discussion') },
    { id: 'ping', label: t('swarm.delivery.route.ping') },
  ]);
  const modes = $derived([
    { value: 'all', label: t('swarm.delivery.receive.all') },
    { value: 'idle', label: t('swarm.delivery.receive.idle') },
    { value: 'pull', label: t('swarm.delivery.receive.pull') },
  ]);
  const limits = $derived([
    {
      field: 'coalesce_ms',
      id: `${idPrefix}-coalesce`,
      label: t('swarm.delivery.batchDelay'),
      description: t('swarm.delivery.batchDelayHelp'),
      min: 0,
      max: 5000,
    },
    {
      field: 'batch_messages',
      id: `${idPrefix}-batch-messages`,
      label: t('swarm.delivery.batchMessages'),
      description: t('swarm.delivery.batchMessagesHelp'),
      min: 1,
      max: 100,
    },
    {
      field: 'batch_chars',
      id: `${idPrefix}-batch-chars`,
      label: t('swarm.delivery.batchChars'),
      description: t('swarm.delivery.batchCharsHelp'),
      min: 16000,
      max: 128000,
    },
  ]);

  function setRoute(route, field, next) {
    onChange({ ...value, [route]: { ...value[route], [field]: next } });
  }
</script>

<div class="swarm-delivery">
  <div class="s-subhead">
    <h4 class="s-subhead__title">
      <span id={`${idPrefix}-receive`}>{t('swarm.delivery.receive')}</span>
      <InfoHint text={t('swarm.delivery.receiveHelp')} />
    </h4>
  </div>
  <div class="s-group">
    {#each routes as route (route.id)}
      <div class="s-row">
        <div class="s-row-info">
          <label
            class="s-row-label"
            id={`${idPrefix}-${route.id}-receive-label`}
            for={`${idPrefix}-${route.id}-receive`}>{route.label}</label
          >
        </div>
        <div class="s-row-control">
          <Dropdown
            id={`${idPrefix}-${route.id}-receive`}
            value={value[route.id].mode}
            options={modes}
            {disabled}
            ariaLabelledby={`${idPrefix}-receive ${idPrefix}-${route.id}-receive-label`}
            onValueChange={(mode) => setRoute(route.id, 'mode', mode)}
          />
        </div>
      </div>
    {/each}
  </div>

  <div class="s-subhead">
    <h4 class="s-subhead__title">
      <span id={`${idPrefix}-wake`}>{t('swarm.delivery.wakeIdle')}</span>
      <InfoHint text={t('swarm.delivery.wakeIdleHelp')} />
    </h4>
  </div>
  <div class="s-group">
    {#each routes as route (route.id)}
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <span class="s-row-label" id={`${idPrefix}-${route.id}-wake-label`}
            >{route.label}</span
          >
        </div>
        <div class="s-row-control">
          <Toggle
            id={`${idPrefix}-${route.id}-wake`}
            checked={value[route.id].wake_idle}
            {disabled}
            aria-labelledby={`${idPrefix}-wake ${idPrefix}-${route.id}-wake-label`}
            onChange={(next) => setRoute(route.id, 'wake_idle', next)}
          />
        </div>
      </div>
    {/each}
  </div>

  <div class="s-group">
    <button
      type="button"
      class="s-row s-row--compact s-disclosure-row"
      aria-expanded={advancedOpen}
      aria-controls={`${idPrefix}-advanced`}
      onclick={() => (advancedOpen = !advancedOpen)}
    >
      <span class="s-row-label" id={`${idPrefix}-advanced-label`}>
        <span
          class="disclosure-chevron"
          class:disclosure-chevron--open={advancedOpen}
          aria-hidden="true"
        ></span>
        {t('swarm.delivery.advanced')}
      </span>
    </button>
    <div
      class="s-group__rows"
      id={`${idPrefix}-advanced`}
      role="group"
      aria-labelledby={`${idPrefix}-advanced-label`}
      hidden={!advancedOpen}
    >
      {#each limits as limit (limit.field)}
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for={limit.id}>{limit.label}</label>
            <div class="s-row-desc" id={`${limit.id}-help`}>
              {limit.description}
            </div>
          </div>
          <div class="s-row-control s-row-control--number">
            <TextField
              id={limit.id}
              type="number"
              inputmode="numeric"
              min={limit.min}
              max={limit.max}
              step="1"
              value={value[limit.field]}
              {disabled}
              aria-describedby={`${limit.id}-help`}
              onInput={(next) => onChange({ ...value, [limit.field]: next })}
            />
          </div>
        </div>
      {/each}
    </div>
  </div>
</div>

<style>
  .swarm-delivery {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 12px;
  }

  .swarm-delivery > .s-subhead:first-child {
    margin-top: 0;
  }

  .s-subhead__title {
    display: flex;
    align-items: center;
    gap: 6px;
  }
</style>
