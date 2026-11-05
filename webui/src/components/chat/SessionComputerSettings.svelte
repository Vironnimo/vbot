<script>
  // The dialog that chooses a conversation's computers; the composer footer's
  // more menu opens it.
  import { t } from '$lib/i18n.js';
  import {
    computerDescription,
    computerName,
    computerOptions,
    computersWithSelection,
    normalizeExecution,
    useComputers,
  } from '$lib/computers.svelte.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import Modal from '../ui/Modal.svelte';

  let {
    value,
    disabled = false,
    onClose = () => {},
    onChange = () => {},
    onRetry = () => {},
  } = $props();
  const catalog = useComputers();
  const policy = $derived(value ? normalizeExecution(value) : null);
  const computers = $derived(
    computersWithSelection(
      catalog?.computers ?? [],
      policy?.allowed_computer_ids ?? [],
    ),
  );
  const selected = $derived(
    computers.find((computer) => computer.id === policy?.computer_id),
  );
  const revoked = $derived(
    Boolean(
      policy && !policy.allowed_computer_ids.includes(policy.computer_id),
    ),
  );
  const options = $derived(
    computerOptions(
      catalog?.computers ?? [],
      policy?.allowed_computer_ids ?? [],
    ),
  );

  function changeDefault(computerId) {
    onChange(
      normalizeExecution({
        computer_id: computerId,
        allowed_computer_ids: [
          computerId,
          ...policy.allowed_computer_ids.filter(
            (id) => id !== policy.computer_id,
          ),
        ],
      }),
    );
  }

  function changeAdditional(id, enabled) {
    onChange(
      normalizeExecution({
        ...policy,
        allowed_computer_ids: enabled
          ? [...policy.allowed_computer_ids, id]
          : policy.allowed_computer_ids.filter((candidate) => candidate !== id),
      }),
    );
  }
</script>

<Modal title={t('computers.sessionTitle')} {onClose}>
  {#snippet body()}
    <div class="modal-body">
      <p class="computer-settings-help">{t('computers.sessionHelp')}</p>
      {#if catalog?.error}
        <Banner variant="error" role="alert">
          {catalog.error}
          <Button variant="secondary" onClick={() => void catalog.refresh()}
            >{t('common.retry')}</Button
          >
        </Banner>
      {/if}
      {#if !policy}
        <Banner variant="neutral" role="status">
          {t('computers.sessionUnknownHelp')}
          <Button variant="secondary" onClick={onRetry}
            >{t('common.retry')}</Button
          >
        </Banner>
      {:else}
        {#if disabled}<Banner variant="neutral">{t('computers.running')}</Banner
          >{/if}
        <div class="computer-settings-field">
          <span class="computer-settings-label">{t('computers.default')}</span>
          <Dropdown
            value={policy.computer_id}
            {options}
            {disabled}
            ariaLabel={t('computers.default')}
            onValueChange={changeDefault}
          />
          <span class="computer-settings-detail"
            >{revoked
              ? t('computers.defaultRevoked')
              : computerDescription(selected)}</span
          >
        </div>
        <p class="computer-settings-label">{t('computers.additional')}</p>
        <p class="computer-settings-help">{t('computers.additionalHelp')}</p>
        <div class="computer-settings-list">
          {#each computers.filter((computer) => computer.id !== policy.computer_id) as computer (computer.id)}
            <Checkbox
              checked={policy.allowed_computer_ids.includes(computer.id)}
              {disabled}
              ariaLabel={computerName(computer)}
              onChange={(enabled) => changeAdditional(computer.id, enabled)}
            >
              <span>{computerName(computer)}</span>
              <span class="computer-settings-detail"
                >{computerDescription(computer)}</span
              >
            </Checkbox>
          {/each}
        </div>
      {/if}
    </div>
  {/snippet}
  {#snippet footer()}
    <Button variant="secondary" onClick={onClose}>{t('common.close')}</Button>
  {/snippet}
</Modal>

<style>
  .computer-settings-help {
    color: var(--text-med);
    font-size: var(--fs-sm);
    margin: 0 0 12px;
  }
  .computer-settings-label {
    font-weight: 500;
    font-size: var(--fs-sm);
    margin: 12px 0 6px;
  }
  .computer-settings-field,
  .computer-settings-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .computer-settings-detail {
    display: block;
    color: var(--text-med);
    font-size: var(--fs-sm);
  }
</style>
