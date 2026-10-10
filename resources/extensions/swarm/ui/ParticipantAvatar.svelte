<script>
  import { participantColor, participantInitials } from './pagePresentation.js';
  import './participantChip.css';

  // A post author's or participant's avatar: a participant's initials in its
  // identity colour, a person for the user. `kind` is a post author's kind;
  // only a participant has its own colour.
  let { id, name, kind = 'participant' } = $props();
</script>

<span
  class="participant-avatar"
  style:--participant-color={kind === 'participant' && id
    ? participantColor(id)
    : 'var(--text-med)'}
  aria-hidden="true"
  >{#if kind === 'user'}<svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      ><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></svg
    >{:else}{participantInitials(name)}{/if}</span
>
