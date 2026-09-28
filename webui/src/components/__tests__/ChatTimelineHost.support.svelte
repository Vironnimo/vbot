<script>
  // Renders ChatTimeline for one Session the way ChatView does: projects the
  // Session's rows once and passes them with its key and current Run. Tests
  // hand in a `sessionState` and mutate it; the projection follows.
  import ChatTimeline from '../ChatTimeline.svelte';
  import { visibleTimelineItemsForRender } from '../../lib/chatState.js';

  let { sessionState, ...timelineProps } = $props();
  let timelineItems = $derived(visibleTimelineItemsForRender(sessionState));
</script>

<ChatTimeline
  {...timelineProps}
  {timelineItems}
  sessionKey={sessionState?.key ?? ''}
  currentRun={sessionState?.currentRun ?? null}
/>
