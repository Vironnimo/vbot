<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { formatMoment } from '../../../../webui/src/lib/timeText.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import CopyButton from '../../../../webui/src/components/ui/CopyButton.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import ModelId from '../../../../webui/src/components/ui/ModelId.svelte';
  import StatusChip from '../../../../webui/src/components/ui/StatusChip.svelte';
  import {
    runProfileName,
    runSize,
    runStateLabel,
    runStateVariant,
    runTitle,
  } from './runPresentation.js';
  import './runDialogs.css';

  // The details of `model.detailsRun`: its Swarm, start, state, size,
  // settings revision and id, its participants with their Models, and the
  // Swarm snapshot it started from behind a disclosure.
  let { model } = $props();

  const run = $derived(model.detailsRun);
  const snapshot = $derived(
    run ? JSON.stringify(run.profile_snapshot ?? {}, null, 2) : '',
  );
</script>

{#if run}<Modal
    title={t('swarm.run.details')}
    class="swarm-dialog swarm-details-dialog"
    onClose={model.closeDetails}
    >{#snippet body()}<div class="modal-body">
        <p class="swarm-dialog__target">{runTitle(run)}</p>
        <dl class="s-group swarm-details__facts">
          <div class="swarm-details__fact">
            <dt>{t('swarm.profile')}</dt>
            <dd>{runProfileName(run)}</dd>
          </div>
          {#if run.created_at}<div class="swarm-details__fact">
              <dt>{t('swarm.details.started')}</dt>
              <dd>{formatMoment(run.created_at, { nowMs: model.now })}</dd>
            </div>{/if}
          <div class="swarm-details__fact">
            <dt>{t('swarm.details.state')}</dt>
            <dd>
              <StatusChip variant={runStateVariant(run.state)}
                >{runStateLabel(run.state)}</StatusChip
              >
            </dd>
          </div>
          <div class="swarm-details__fact">
            <dt>{t('swarm.participants')}</dt>
            <dd>{runSize(run)}</dd>
          </div>
          {#if run.settings_revision !== undefined}<div
              class="swarm-details__fact"
            >
              <dt>{t('swarm.details.settingsRevision')}</dt>
              <dd>{run.settings_revision}</dd>
            </div>{/if}
          <div class="swarm-details__fact">
            <dt>{t('swarm.details.id')}</dt>
            <dd class="swarm-details__id">
              <code>{run.id}</code><CopyButton
                text={run.id}
                label={t('swarm.details.copyId')}
              />
            </dd>
          </div>
        </dl>
        {#if run.participants?.length}<ul
            class="s-group swarm-details__participants"
            aria-label={t('swarm.participants')}
          >
            {#each run.participants as participant (participant.id)}<li
                class="swarm-details__participant"
              >
                <span>{participant.display_name}</span><ModelId
                  id={participant.model}
                />
              </li>{/each}
          </ul>{/if}
        <details class="swarm-details__snapshot">
          <summary>{t('swarm.profileSnapshot')}</summary>
          <div class="swarm-details__snapshot-body">
            <CopyButton
              text={snapshot}
              label={t('swarm.details.copySnapshot')}
            />
            <pre>{snapshot}</pre>
          </div>
        </details>
      </div>{/snippet}{#snippet footer()}<Button
        variant="primary"
        onClick={model.closeDetails}>{t('common.close')}</Button
      >{/snippet}</Modal
  >{/if}
