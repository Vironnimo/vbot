<script>
  import {
    participantCard,
    participantColor,
    participantLabel,
    participantState,
  } from './pagePresentation.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';
  import './participantChip.css';

  // A participant of the Board's discussion or of the Activity list: its
  // avatar, name and, unless it is idle, a state dot. Its tooltip is the
  // participant's details card. `selected` marks the inspected participant;
  // undefined omits the pressed state. `context` is the participant's context
  // use as display text, when the caller knows it. `onInspect` receives the
  // participant.
  let { participant, selected = undefined, context = '', onInspect } = $props();

  let state = $derived(participantState(participant));
</script>

<Button
  variant="tertiary"
  class="participant-chip"
  style={`--participant-color: ${participantColor(participant.id)}`}
  tooltip={() => participantCard(participant, { context })}
  ariaLabel={participantLabel(participant)}
  aria-pressed={selected}
  onClick={() => onInspect(participant)}
>
  <ParticipantAvatar id={participant.id} name={participant.display_name} />
  <span class="participant-chip__name">{participant.display_name}</span>
  {#if state !== 'idle'}<span
      class="participant-status"
      data-state={state}
      aria-hidden="true"
    ></span>{/if}
</Button>
