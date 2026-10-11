<script>
  // How Board messages reach participants, edited as one `delivery` value:
  // for each route (main discussion, joined discussions, direct mentions)
  // when its messages are received and whether a message starts an idle
  // participant, plus the batching limits behind one disclosure row. A Swarm
  // and a Run's live communication settings share it; the caller owns saving
  // and validation. `onChange(next)` receives the complete next value. Number
  // fields report their text as entered, so callers normalize numbers at
  // their payload boundary (`normalizeDelivery`). `errors` holds the message
  // of each invalid batching limit by its field (`batch_messages`, ...), which
  // shows under that field. `idPrefix` keeps the element ids of several
  // instances apart: a limit's field is `${idPrefix}-${limit.id}`
  // (DELIVERY_LIMITS).
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import InfoHint from '../../../../webui/src/components/ui/InfoHint.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import { DELIVERY_LIMITS } from './runPresentation.js';

  let { value, onChange, errors = {}, disabled = false, idPrefix } = $props();

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
  // The batching limits' field texts; their ids and ranges are
  // DELIVERY_LIMITS', which the callers validate with.
  const limitTexts = $derived({
    coalesce_ms: {
      label: t('swarm.delivery.batchDelay'),
      description: t('swarm.delivery.batchDelayHelp'),
    },
    batch_messages: {
      label: t('swarm.delivery.batchMessages'),
      description: t('swarm.delivery.batchMessagesHelp'),
    },
    batch_chars: {
      label: t('swarm.delivery.batchChars'),
      description: t('swarm.delivery.batchCharsHelp'),
    },
  });
  const limits = $derived(
    DELIVERY_LIMITS.map((limit) => ({
      ...limitTexts[limit.field],
      field: limit.field,
      id: `${idPrefix}-${limit.id}`,
      min: limit.min,
      max: limit.max,
      error: errors[limit.field] ?? '',
    })),
  );
  const holdsError = $derived(limits.some((limit) => limit.error));

  // An error never hides behind the disclosure: it opens to show one and
  // stays open while one remains.
  let advancedOpen = $state(false);
  $effect.pre(() => {
    if (holdsError) advancedOpen = true;
  });

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
      onclick={() => (advancedOpen = holdsError || !advancedOpen)}
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
            {#if limit.error}
              <p
                class="swarm-profile-editor__error"
                id={`${limit.id}-error`}
                role="alert"
              >
                {limit.error}
              </p>
            {/if}
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
              invalid={Boolean(limit.error)}
              aria-describedby={limit.error
                ? `${limit.id}-help ${limit.id}-error`
                : `${limit.id}-help`}
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
