<script>
  import { t } from '$lib/i18n.js';
  import {
    renderMarkdownDocument,
    renderMarkdownStreamingDocument,
    renderReasoningMarkdownDocument,
    renderReasoningMarkdownStreamingDocument,
  } from '$lib/markdown.js';
  import { renderMarkdownDom } from './markdownDom.js';

  let {
    source = '',
    streaming = false,
    reasoning = false,
    caret = false,
    class: className = '',
    contentId = undefined,
  } = $props();

  let renderedDocument = $derived.by(() => {
    const options = { plainLanguageLabel: t('chat.codeLanguagePlain') };
    if (reasoning) {
      return streaming
        ? renderReasoningMarkdownStreamingDocument(source, options)
        : renderReasoningMarkdownDocument(source, options);
    }
    return streaming
      ? renderMarkdownStreamingDocument(source, options)
      : renderMarkdownDocument(source, options);
  });
</script>

<div
  class={className}
  data-timeline-content-id={contentId}
  use:renderMarkdownDom={{
    document: renderedDocument,
    fileLinks: !reasoning && className.split(/\s+/).includes('msg-markdown'),
    copyLabel: t('chat.copyCode'),
    copiedLabel: t('chat.codeCopied'),
    previewHint: t('chat.fileLink.previewHint'),
    actionsHint: t('chat.fileLink.actionsHint'),
    externalLabel: (filename) => t('preview.openExternal', { filename }),
    caret,
  }}
></div>

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
