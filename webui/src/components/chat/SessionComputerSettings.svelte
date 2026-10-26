<script>
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
    onChange = () => {},
    onRetry = () => {},
  } = $props();
  const catalog = useComputers();
  let open = $state(false);
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
  const label = $derived(
    !policy
      ? t('computers.sessionUnknown')
      : revoked
        ? t('computers.selectedRevoked', { name: computerName(selected) })
        : selected?.known && !selected.online
          ? t('computers.selectedOffline', { name: computerName(selected) })
          : computerName(selected),
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

<Button
  variant="tertiary"
  class="session-computer-trigger"
  ariaLabel={t('computers.sessionTitle')}
  tooltip={t('computers.sessionHint')}
  onClick={() => {
    open = true;
    void catalog?.refresh();
  }}
>
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <rect
      x="1.5"
      y="2"
      width="13"
      height="9"
      rx="1"
      fill="none"
      stroke="currentColor"
    />
    <path d="M5 14h6M8 11v3" fill="none" stroke="currentColor" />
  </svg>
  <span class="session-computer-label">{label}</span>
</Button>

{#if open}
  <Modal title={t('computers.sessionTitle')} onClose={() => (open = false)}>
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
          {#if disabled}<Banner variant="neutral"
              >{t('computers.running')}</Banner
            >{/if}
          <div class="computer-settings-field">
            <span class="computer-settings-label">{t('computers.default')}</span
            >
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
      <Button variant="secondary" onClick={() => (open = false)}
        >{t('common.close')}</Button
      >
    {/snippet}
  </Modal>
{/if}

<style>
  :global(.session-computer-trigger) {
    min-width: 0;
    max-width: 180px;
    min-height: 26px;
    padding: 2px 6px;
    gap: 4px;
    font-size: var(--fs-label-sm);
  }
  .session-computer-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
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
