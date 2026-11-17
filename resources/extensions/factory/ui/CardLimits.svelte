<script>
  import { untrack } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import LimitsEditor from './LimitsEditor.svelte';
  import { limitsProblem, normalizeLimits } from './limits.js';
  let { model, card, steps, stopped, act } = $props();
  // Limits of a running Card: the Card's and one chosen step's, saved with
  // an explicit button while the Card waits.
  let limitStep = $state('');
  let cardLimits = $state({});
  let stepLimits = $state({});
  let limitsError = $state('');
  const limitTarget = $derived(
    steps.some((step) => step.id === limitStep && step.kind !== 'workflow')
      ? limitStep
      : (stopped.find((step) => step.kind !== 'workflow')?.id ??
          steps.find((step) => step.kind !== 'workflow')?.id ??
          ''),
  );
  const savedCardLimits = $derived(
    card.runtime?.card_limits ?? card.runtime?.workflow?.limits ?? {},
  );
  const savedStepLimits = $derived(
    card.runtime?.steps?.[limitTarget]?.limits ??
      steps.find((step) => step.id === limitTarget)?.limits ??
      {},
  );
  const limitsChanged = $derived(
    JSON.stringify(normalizeLimits(cardLimits)) !==
      JSON.stringify(normalizeLimits(savedCardLimits)) ||
      JSON.stringify(normalizeLimits(stepLimits)) !==
        JSON.stringify(normalizeLimits(savedStepLimits)),
  );
  const budget = $derived(
    model.detail.budget?.[limitTarget] ??
      Object.values(model.detail.budget ?? {})[0] ??
      null,
  );
  const limitsSnapshot = $derived(
    JSON.stringify([limitTarget, savedCardLimits, savedStepLimits]),
  );
  $effect(() => {
    void limitsSnapshot;
    untrack(() => {
      cardLimits = { ...savedCardLimits };
      stepLimits = { ...savedStepLimits };
      limitsError = '';
    });
  });

  async function saveLimits() {
    limitsError = limitsProblem(cardLimits) || limitsProblem(stepLimits);
    if (limitsError) return;
    await act('card.limits', {
      expected_revision: card.revision,
      card_limits: normalizeLimits(cardLimits),
      ...(limitTarget
        ? { step: limitTarget, step_limits: normalizeLimits(stepLimits) }
        : {}),
    });
  }
</script>

<section class="factory-section" aria-label={t('factory.limit.title')}>
  <h3>{t('factory.limit.title')}</h3>
  <LimitsEditor
    bind:value={cardLimits}
    prefix="factory-card-limit"
    label={t('factory.limit.card')}
    disabled={card.state !== 'waiting'}
    usage={budget?.card?.usage}
  />
  {#if limitTarget}
    <div class="factory-field">
      <label id="factory-limit-step-label" for="factory-limit-step"
        >{t('factory.limit.stepChoice')}</label
      ><Dropdown
        id="factory-limit-step"
        ariaLabelledby="factory-limit-step-label"
        value={limitTarget}
        options={steps
          .filter((step) => step.kind !== 'workflow')
          .map((step) => ({ value: step.id, label: step.name }))}
        onOpenChange={model.pickerOpen}
        onValueChange={(value) => (limitStep = value)}
      />
    </div>
    <LimitsEditor
      bind:value={stepLimits}
      prefix="factory-step-limit"
      label={t('factory.limit.step')}
      disabled={card.state !== 'waiting'}
      usage={budget?.step?.usage}
    />
  {/if}
  <p class="factory-help">{t('factory.limit.help')}</p>
  {#if card.state === 'waiting'}
    <p class="factory-help">{t('factory.limit.resume')}</p>
    <Button
      variant="secondary"
      disabled={!limitsChanged || Boolean(model.pending)}
      loading={model.pending === 'card.limits'}
      onClick={saveLimits}>{t('factory.limit.save')}</Button
    >
    {#if limitsError}<Banner variant="error" role="alert">{limitsError}</Banner
      >{/if}
  {/if}
</section>
