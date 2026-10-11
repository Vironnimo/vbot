<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import DeliverySettings from './DeliverySettings.svelte';
  import { runTitle } from './runPresentation.js';
  import './runDialogs.css';

  // A Run's live communication settings: the Swarm editor's delivery fields
  // for `model.settingsRun`, the valid changes they make and Apply. An
  // invalid batching limit shows its error under its field and keeps Apply
  // off. A failed apply stays in the dialog for another try.
  let { model } = $props();

  const busy = $derived(model.pending === 'settings');
  const invalid = $derived(Object.keys(model.deliveryErrors).length > 0);
  const applyReason = $derived(
    invalid
      ? t('swarm.communication.invalid')
      : model.deliveryChangeLines.length
        ? ''
        : t('swarm.communication.noChanges'),
  );
</script>

{#if model.settingsRun && model.deliveryDraft}<Modal
    title={t('swarm.communication.title')}
    class="swarm-dialog swarm-communication-dialog"
    closeDisabled={busy}
    onClose={model.closeSettings}
    >{#snippet body()}<div class="modal-body">
        <p class="swarm-dialog__target">{runTitle(model.settingsRun)}</p>
        <DeliverySettings
          idPrefix="swarm-live-delivery"
          value={model.deliveryDraft}
          errors={model.deliveryErrors}
          disabled={busy}
          onChange={model.changeDelivery}
        />
        <section
          class="swarm-communication__changes"
          aria-labelledby="swarm-communication-changes"
          aria-live="polite"
        >
          <h4 id="swarm-communication-changes" class="s-subhead__title">
            {t('swarm.communication.proposed')}
          </h4>
          {#if model.deliveryChangeLines.length}<ul
              class="swarm-communication__list"
            >
              {#each model.deliveryChangeLines as change (change.id)}<li>
                  {change.text}
                </li>{/each}
            </ul>
            <p class="swarm-dialog__note">
              {t('swarm.communication.effect')}
            </p>{:else}<p class="swarm-dialog__note">
              {invalid
                ? t('swarm.communication.invalidNote')
                : t('swarm.communication.noChanges')}
            </p>{/if}
        </section>
        {#if model.settingsError}<Banner variant="error" role="alert"
            >{model.settingsError}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={busy}
        onClick={model.closeSettings}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={busy}
        disabled={Boolean(applyReason)}
        disabledReason={applyReason}
        onClick={model.applyDelivery}
        >{busy ? t('swarm.applying') : t('swarm.apply')}</Button
      >{/snippet}</Modal
  >{/if}
