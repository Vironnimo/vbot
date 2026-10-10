<script>
  import { participantColor, participantInitials } from './pagePresentation.js';

  // `kind` is a post author's kind; only a participant has its own color.
  let { id, name, kind = 'participant' } = $props();
</script>

<span
  class="participant-avatar"
  style:--participant-color={kind === 'participant' && id
    ? participantColor(id)
    : 'var(--text-med)'}
  aria-hidden="true">{participantInitials(name)}</span
>
