<script>
  import { mount, unmount } from 'svelte';

  import { t } from '$lib/i18n.js';
  import {
    renderMarkdownDocument,
    renderMarkdownStreamingDocument,
    renderReasoningMarkdownDocument,
    renderReasoningMarkdownStreamingDocument,
  } from '$lib/markdown.js';

  import CopyButton from '../ui/CopyButton.svelte';

  let {
    source = '',
    streaming = false,
    reasoning = false,
    caret = false,
    class: className = '',
  } = $props();

  let container = $state();
  let renderedDocument = $derived.by(() => {
    const options = {
      plainLanguageLabel: t('chat.codeLanguagePlain', 'text'),
    };
    if (reasoning) {
      return streaming
        ? renderReasoningMarkdownStreamingDocument(source, options)
        : renderReasoningMarkdownDocument(source, options);
    }
    return streaming
      ? renderMarkdownStreamingDocument(source, options)
      : renderMarkdownDocument(source, options);
  });

  $effect(() => {
    const target = container;
    const codeBlocks = renderedDocument.codeBlocks;
    if (!target) {
      return undefined;
    }

    const mountedCopyButtons = [];
    for (const slot of target.querySelectorAll('[data-markdown-code-index]')) {
      const codeBlockIndex = Number(slot.dataset.markdownCodeIndex);
      const codeBlock = codeBlocks[codeBlockIndex];
      if (!codeBlock?.copyable || !codeBlock.text) {
        continue;
      }

      mountedCopyButtons.push(
        mount(CopyButton, {
          target: slot,
          props: {
            text: codeBlock.text,
            label: t('chat.copyCode', 'Copy code'),
            copiedLabel: t('chat.codeCopied', 'Code copied'),
            class: 'msg-code__copy',
          },
        }),
      );
    }

    return () => {
      for (const copyButton of mountedCopyButtons) {
        void unmount(copyButton);
      }
    };
  });
</script>

<div bind:this={container} class={className}>
  <!-- eslint-disable-next-line svelte/no-at-html-tags -->
  {@html renderedDocument.html}
  {#if caret}<span class="streaming-caret" aria-hidden="true"></span>{/if}
</div>
