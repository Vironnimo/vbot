<script>
  // An active wake phrase whose model is no longer installed on this Desktop.
  // It cannot be heard or tuned; the card only explains that and lets the
  // user stop listening for it. Its stored sensitivity and actions stay, so
  // importing the model again brings the phrase back as it was.
  import { t } from '$lib/i18n.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import { errorMessage } from './voiceLabels.js';

  let {
    modelId,
    // Label the Desktop reports for the phrase (the model id when unknown).
    label = '',
    deactivateDisabled = false,
    onDeactivate = () => {},
  } = $props();

  let name = $derived(label || modelId);
</script>

<div
  class="voice-model-card voice-model-card--active voice-model-card--unavailable"
  data-model-id={modelId}
>
  <div class="voice-model-card__header">
    <div class="voice-model-card__identity">
      <span class="voice-model-card__name">{name}</span>
      <Badge variant="warn" class="voice-model-card__problem-badge">
        {t('settings.voice.phraseUnavailable', 'Not installed')}
      </Badge>
    </div>
  </div>
  <p class="voice-model-card__notice" role="status">
    {errorMessage('wakeword_model_unavailable')}
  </p>
  <div class="voice-model-card__actions">
    <Button
      variant="tertiary"
      disabled={deactivateDisabled}
      ariaLabel={t(
        'settings.voice.deactivatePhraseAria',
        'Stop listening for {name}',
        {
          name,
        },
      )}
      onClick={onDeactivate}
    >
      {t('settings.voice.deactivatePhrase', 'Stop listening')}
    </Button>
  </div>
</div>
