<script>
  import {
    participantColor,
    participantState,
    participantDetails,
  } from './pagePresentation.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';

  // A participant of the Board's discussion or of the Activity list.
  // `selected` marks the inspected participant; undefined omits the pressed
  // state. `onInspect` receives the participant.
  let { participant, selected = undefined, onInspect } = $props();
</script>

<Button
  variant="tertiary"
  class="participant-chip"
  style={`--participant-color: ${participantColor(participant.id)}`}
  tooltip={participantDetails(participant)}
  ariaLabel={participantDetails(participant)}
  aria-pressed={selected}
  onClick={() => onInspect(participant)}
>
  <ParticipantAvatar id={participant.id} name={participant.display_name} />
  <strong>{participant.display_name}</strong>
  <span
    class="participant-status"
    data-state={participantState(participant)}
    aria-hidden="true"
  ></span>
</Button>
