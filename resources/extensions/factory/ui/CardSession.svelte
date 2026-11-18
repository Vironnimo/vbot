<script>
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import SessionEmbed from '@vbot/extension-sdk/ui/SessionEmbed.svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import { participantId, participantName } from './presentation.js';

  let {
    client,
    groupId,
    participant,
    presentation = 'conversation',
    onClose,
  } = $props();
  const headingId = $props.id();
</script>

<section
  class="factory-section factory-session-history"
  aria-labelledby={headingId}
>
  <header>
    <h3 id={headingId}>
      {t('factory.history')} · {participantName(participant)}
    </h3>
    <Button variant="tertiary" onClick={onClose}>{t('common.close')}</Button>
  </header>
  <div class="session-slot">
    <SessionEmbed
      {client}
      {groupId}
      participantId={participantId(participant)}
      {presentation}
    />
  </div>
</section>

<style>
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }
  header h3 {
    margin: 0;
  }
  .session-slot {
    height: clamp(320px, 60dvh, 840px);
  }
</style>
