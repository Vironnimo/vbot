<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import './communicationDialog.css';

  // Changes the selected Run's message delivery: a mode and wake setting per
  // route, the batching limits, and a preview of the changes.
  let { model } = $props();
  // Same route names as the profile editor's delivery settings.
  const routeLabels = $derived({
    main: t('swarm.profile.mainMessages'),
    discussion: t('swarm.profile.joinedMessages'),
    ping: t('swarm.profile.mentions'),
  });
</script>

{#if model.settingsOpen}<Modal
    title={t('swarm.communication.title')}
    closeDisabled={model.pending === 'settings'}
    onClose={() => (model.settingsOpen = false)}
    >{#snippet body()}<div class="communication swarm-page-communication">
        {#each ['main', 'discussion', 'ping'] as route (route)}<section>
            <h3>{routeLabels[route]}</h3>
            <FormField
              controlId={`live-delivery-${route}`}
              label={t('swarm.delivery.mode')}
              ><select
                class="s-input"
                value={model.deliveryDraft[route].mode}
                onchange={(event) =>
                  model.changeDelivery(
                    route,
                    'mode',
                    event.currentTarget.value,
                  )}
                ><option value="all">{t('swarm.delivery.all')}</option><option
                  value="idle">{t('swarm.delivery.idle')}</option
                ><option value="pull">{t('swarm.delivery.pull')}</option
                ></select
              ></FormField
            ><label class="check"
              ><input
                type="checkbox"
                checked={model.deliveryDraft[route].wake_idle}
                onchange={(event) =>
                  model.changeDelivery(
                    route,
                    'wake_idle',
                    event.currentTarget.checked,
                  )}
              />
              {t('swarm.delivery.wake')}</label
            >
          </section>{/each}
        <section>
          <h3>{t('swarm.communication.advanced')}</h3>
          <div class="advanced-settings">
            <FormField
              controlId="live-coalesce"
              label={t('swarm.delivery.coalesce')}
              ><input
                class="s-input"
                id="live-coalesce"
                type="number"
                min="0"
                max="5000"
                value={model.deliveryDraft.coalesce_ms}
                onchange={(event) =>
                  model.changeDeliverySetting(
                    'coalesce_ms',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
            <FormField
              controlId="live-batch-messages"
              label={t('swarm.delivery.batchMessages')}
              ><input
                class="s-input"
                id="live-batch-messages"
                type="number"
                min="1"
                max="100"
                value={model.deliveryDraft.batch_messages}
                onchange={(event) =>
                  model.changeDeliverySetting(
                    'batch_messages',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
            <FormField
              controlId="live-batch-chars"
              label={t('swarm.delivery.batchChars')}
              ><input
                class="s-input"
                id="live-batch-chars"
                type="number"
                min="16000"
                max="128000"
                value={model.deliveryDraft.batch_chars}
                onchange={(event) =>
                  model.changeDeliverySetting(
                    'batch_chars',
                    event.currentTarget.value,
                  )}
              /></FormField
            >
          </div>
        </section>
        <section>
          <h3>{t('swarm.communication.proposed')}</h3>
          {#if model.settingChanges.length}<ul>
              {#each model.settingChanges as change (`${change.route}-${change.field}`)}<li
                >
                  {change.route} · {change.field}: <s>{change.before}</s> → {change.after}
                </li>{/each}
            </ul>{:else}<p>
              {t('swarm.communication.noChanges')}
            </p>{/if}
          <p>
            {t('swarm.communication.effect')}
          </p>
        </section>
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={model.pending === 'settings'}
        onClick={() => (model.settingsOpen = false)}
        >{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={model.pending === 'settings'}
        disabled={model.settingChanges.length === 0}
        onClick={model.applyDelivery}
        >{model.pending === 'settings'
          ? t('swarm.applying')
          : t('swarm.apply')}</Button
      >{/snippet}</Modal
  >{/if}
