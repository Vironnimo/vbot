<script>
  import { writeClipboardText } from '$lib/clipboard.js';
  import ContributionSurface from '../extensions/ContributionSurface.svelte';

  let {
    surface = 'chat.message',
    message,
    text = '',
    uiContext = {},
    builtins = [],
  } = $props();

  // The public message projection deliberately excludes provider payloads,
  // attachment locations, editable internals, and Tool reasoning/state.
  let context = $derived({
    ...uiContext,
    message_id: String(message?.id ?? ''),
    message_role: String(message?.role ?? ''),
    run_id: String(message?.run_id ?? uiContext.run_id ?? ''),
  });
  let data = $derived({
    message: {
      id: context.message_id,
      role: context.message_role,
      text: text.slice(0, 32768),
      truncated: text.length > 32768,
      timestamp:
        typeof message?.timestamp === 'string'
          ? message.timestamp.slice(0, 100)
          : null,
    },
  });
</script>

<ContributionSurface
  {surface}
  {context}
  {data}
  {builtins}
  commands={{ 'message.copy': () => writeClipboardText(text) }}
/>
