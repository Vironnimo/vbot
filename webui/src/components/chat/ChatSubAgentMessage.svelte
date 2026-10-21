<script>
  // The `message_parent` messages one Parent Session received at this point of
  // its conversation: a Run child or a History row of type `subagent_message`.
  // Each message is one collapsed line naming its sender; the text shows on
  // expanding.
  import Button from '../ui/Button.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import { t } from '$lib/i18n.js';
  import {
    subAgentMessages,
    subAgentMessageTarget,
  } from '$lib/chatTimelinePresentation.js';
  import MarkdownContent from './MarkdownContent.svelte';
  import { timelineViewState } from './timelineViewState.svelte.js';

  let {
    item,
    onNavigateToSubAgent = () => {},
    onSubAgentContextMenu = null,
  } = $props();

  // Disclosure state lives in the timeline's view state, so it survives this
  // row being unmounted and mounted again.
  const viewState = timelineViewState();

  let entries = $derived(subAgentMessages(item));

  const disclosureKey = (entry, index) =>
    `subagent-message:${item.id}:${entry.delivery_id ?? index}`;

  async function navigate(event, entry) {
    event.preventDefault();
    event.stopPropagation();
    const target = subAgentMessageTarget(entry);
    if (target) await onNavigateToSubAgent(target);
  }

  function contextMenu(event, entry) {
    if (!onSubAgentContextMenu || event.defaultPrevented) return;
    if (event.type === 'keydown' && !isContextMenuKey(event)) return;
    const target = subAgentMessageTarget(entry);
    if (target) onSubAgentContextMenu(event, target);
  }
</script>

{#each entries as entry, index (entry.delivery_id ?? index)}
  {@const key = disclosureKey(entry, index)}
  {@const target = subAgentMessageTarget(entry)}
  <details
    class="tool-event run-tool-event subagent-tool-event subagent-message"
    open={viewState.isOpen(key)}
    ontoggle={(event) => viewState.setOpen(key, event.currentTarget.open)}
  >
    <summary class="tool-event-line subagent-line">
      <span class="te-dot subagent-message-mark" aria-hidden="true">●</span>
      <span class="te-fn">{t('chat.subagent.messageFromLabel')}</span>
      {#if entry.title}
        <span class="subagent-agent">{entry.title}</span>
      {/if}
      {#if target}
        <Button
          variant="tertiary"
          icon
          class="tool-row-action subagent-session-action subagent-link"
          tooltip={t('chat.subagent.openSession')}
          ariaLabel={t('chat.subagent.openSession')}
          onClick={(event) => navigate(event, entry)}
          oncontextmenu={(event) => contextMenu(event, entry)}
          onkeydown={(event) => contextMenu(event, entry)}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="M2.5 3.5h7v6h-4l-2.5 2v-2h-.5z" />
            <path d="M9 6h4.5v4.5M13.5 6 8 11.5" />
          </svg>
        </Button>
      {/if}
    </summary>
    <div class="tool-event-body">
      <MarkdownContent
        contentId={key}
        source={entry.content ?? ''}
        class="msg-markdown"
      />
    </div>
  </details>
{/each}

<style>
  .subagent-message-mark {
    color: var(--text-lo);
  }
</style>
