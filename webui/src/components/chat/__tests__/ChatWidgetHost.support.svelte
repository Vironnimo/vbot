<script>
  // Renders a ChatWidget inside a Chat timeline's view state scoped to
  // `sessionKey`, the way ChatTimeline provides it to its rows.
  import { untrack } from 'svelte';
  import ChatWidget from '../ChatWidget.svelte';
  import {
    createTimelineViewState,
    provideTimelineViewState,
  } from '../timelineViewState.svelte.js';

  let { sessionKey, widget } = $props();
  const viewState = provideTimelineViewState(createTimelineViewState());
  viewState.setSession(untrack(() => sessionKey));
</script>

<ChatWidget {widget} />
