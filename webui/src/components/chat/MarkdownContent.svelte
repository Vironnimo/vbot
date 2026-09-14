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
  import { tooltip } from '$lib/tooltip.js';

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
    const fileActions = [];
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

    if (!reasoning && className.split(/\s+/).includes('msg-markdown')) {
      for (const link of target.querySelectorAll('a')) {
        const href = link.getAttribute('href') || '';
        const filename = link.textContent.trim();
        let url;
        try {
          url = new URL(href, window.location.href);
        } catch {
          continue;
        }
        if (
          url.origin !== window.location.origin ||
          !/^\/api\/files\/[A-Za-z0-9_.-]+$/.test(url.pathname)
        )
          continue;
        const path = link.getAttribute('title') || '';
        link.dataset.deliveredFile = href;
        link.dataset.fileName = filename;
        link.dataset.filePath = path;
        link.setAttribute('aria-haspopup', 'menu');
        link.removeAttribute('title');
        const pathHint = path ? tooltip(link, path) : null;
        fileActions.push(() => {
          pathHint?.destroy();
          if (path) link.setAttribute('title', path);
          delete link.dataset.deliveredFile;
          delete link.dataset.fileName;
          delete link.dataset.filePath;
          link.removeAttribute('aria-haspopup');
          link.removeAttribute('aria-expanded');
        });
        if (!/\.html?$/i.test(filename)) continue;
        link.dataset.previewFile = href;
        const external = document.createElement('a');
        const externalLabel = t(
          'preview.openExternal',
          'Open {filename} in browser',
          { filename },
        );
        external.href = href;
        external.target = '_blank';
        external.rel = 'noopener noreferrer';
        external.dataset.fileExternal = '';
        external.dataset.previewFile = href;
        external.dataset.deliveredFile = href;
        external.dataset.fileName = filename;
        external.dataset.filePath = path;
        external.setAttribute('aria-label', externalLabel);
        external.textContent = '↗';
        link.after(external);
        const hint = tooltip(external, externalLabel);
        fileActions.push(() => {
          hint.destroy();
          external.remove();
          delete link.dataset.previewFile;
          delete link.dataset.fileName;
          link.removeAttribute('aria-haspopup');
          link.removeAttribute('aria-expanded');
        });
      }
    }

    return () => {
      for (const copyButton of mountedCopyButtons) {
        void unmount(copyButton);
      }
      for (const cleanup of fileActions) cleanup();
    };
  });
</script>

<div bind:this={container} class={className}>
  <!-- eslint-disable-next-line svelte/no-at-html-tags -->
  {@html renderedDocument.html}
  {#if caret}<span class="streaming-caret" aria-hidden="true"></span>{/if}
</div>

<style>
  :global(.msg-markdown a[data-file-external]) {
    display: inline;
    margin-left: 0.25em;
    color: var(--text-med);
    text-decoration: none;
    font: inherit;
    vertical-align: baseline;
  }
  :global(.msg-markdown a[data-file-external]:hover),
  :global(.msg-markdown a[data-file-external]:focus-visible) {
    color: var(--text-hi);
    opacity: 1;
  }
</style>
