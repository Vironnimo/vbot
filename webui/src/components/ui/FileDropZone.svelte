<script>
  // Shared file chooser: a drop area with a "Choose file" button that opens
  // the system file dialog, in place of the browser's native file input,
  // whose wording follows the browser's language instead of the app's. It
  // shows what it accepts (`hint`, already translated) until a file is
  // chosen, then that file's name and size. The native input stays hidden
  // inside; the button is the keyboard control, so a FormField label points
  // at it through `id` and `ariaLabelledby`. A dropped file outside `accept`
  // is refused with a message instead of being passed on. The caller owns
  // the chosen `file` and any further checks such as a size limit.
  import { formatFileSize, t } from '$lib/i18n.js';
  import Button from './Button.svelte';

  const noop = () => {};
  const componentId = $props.id();

  let {
    id = '',
    accept = '',
    file = null,
    hint = '',
    disabled = false,
    invalid = false,
    ariaLabelledby = undefined,
    ariaDescribedby = undefined,
    onFile = noop,
  } = $props();

  let inputElement = $state();
  // Nested drag enter/leave pairs; the area is a target while any is open.
  let dragDepth = $state(0);
  let refused = $state('');

  let buttonId = $derived(id || `${componentId}-choose`);
  let statusId = `${componentId}-status`;
  let dragging = $derived(dragDepth > 0 && !disabled);
  let acceptedTypes = $derived(
    accept
      .split(',')
      .map((entry) => entry.trim().toLowerCase())
      .filter(Boolean),
  );

  function accepts(candidate) {
    if (acceptedTypes.length === 0) return true;
    const name = candidate.name.toLowerCase();
    const type = (candidate.type || '').toLowerCase();
    return acceptedTypes.some((entry) => {
      if (entry.startsWith('.')) return name.endsWith(entry);
      if (entry.endsWith('/*')) return type.startsWith(entry.slice(0, -1));
      return type === entry;
    });
  }

  function take(candidate) {
    if (!candidate) return;
    if (!accepts(candidate)) {
      refused = t('fileDrop.wrongType', { name: candidate.name });
      return;
    }
    refused = '';
    onFile(candidate);
  }

  function choose() {
    if (!disabled) inputElement?.click();
  }

  function handleInputChange(event) {
    take(event.currentTarget.files?.[0] ?? null);
    // Choosing the same file again still reports it.
    event.currentTarget.value = '';
  }

  function hasFiles(event) {
    return Array.from(event.dataTransfer?.types ?? []).includes('Files');
  }

  function handleDragEnter(event) {
    if (disabled || !hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
  }

  function handleDragOver(event) {
    if (disabled || !hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }

  function handleDragLeave() {
    dragDepth = Math.max(0, dragDepth - 1);
  }

  function handleDrop(event) {
    dragDepth = 0;
    if (disabled || !hasFiles(event)) return;
    event.preventDefault();
    take(event.dataTransfer.files?.[0] ?? null);
  }
</script>

<!-- Dropping is a shortcut: the Choose file button does the same. -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="file-drop"
  class:file-drop--dragging={dragging}
  class:file-drop--chosen={Boolean(file)}
  class:file-drop--invalid={invalid || Boolean(refused)}
  class:file-drop--disabled={disabled}
  ondragenter={handleDragEnter}
  ondragover={handleDragOver}
  ondragleave={handleDragLeave}
  ondrop={handleDrop}
>
  <input
    bind:this={inputElement}
    class="file-drop__input"
    type="file"
    {accept}
    {disabled}
    tabindex="-1"
    aria-hidden="true"
    onchange={handleInputChange}
  />
  <svg
    class="file-drop__icon"
    viewBox="0 0 20 20"
    width="20"
    height="20"
    aria-hidden="true"
  >
    {#if file}
      <path
        d="M5 2.75h6.5l3.5 3.5v11H5zM11.5 2.75v3.5H15"
        fill="none"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linejoin="round"
      />
    {:else}
      <path
        d="M10 13V3.5M6.25 7.25 10 3.5l3.75 3.75M3.5 12.5v4h13v-4"
        fill="none"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    {/if}
  </svg>
  <div class="file-drop__text" id={statusId}>
    {#if dragging}
      <span class="file-drop__primary">{t('fileDrop.release')}</span>
    {:else if file}
      <span class="file-drop__primary file-drop__name">{file.name}</span>
      <span class="file-drop__meta">{formatFileSize(file.size)}</span>
    {:else}
      <span class="file-drop__primary">{t('fileDrop.prompt')}</span>
      {#if hint}<span class="file-drop__meta">{hint}</span>{/if}
    {/if}
  </div>
  <Button
    id={buttonId}
    variant="secondary"
    {disabled}
    aria-labelledby={ariaLabelledby
      ? `${ariaLabelledby} ${buttonId}`
      : undefined}
    aria-describedby={[statusId, ariaDescribedby].filter(Boolean).join(' ')}
    onClick={choose}
    >{file ? t('fileDrop.change') : t('fileDrop.choose')}</Button
  >
</div>
{#if refused}
  <p class="file-drop__error" role="alert">{refused}</p>
{/if}

<style>
  .file-drop {
    display: flex;
    min-width: 0;
    min-height: 72px;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 14px;
    padding: 14px 16px;
    border: 1px dashed var(--border-2);
    border-radius: var(--r-md);
    background: var(--field-surface);
    transition:
      border-color 120ms ease,
      background 120ms ease;
  }

  .file-drop--chosen {
    border-style: solid;
  }

  .file-drop--dragging {
    border-style: solid;
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 7%, var(--field-surface));
  }

  .file-drop--invalid {
    border-color: var(--red);
  }

  .file-drop--disabled {
    opacity: 0.55;
  }

  .file-drop__input {
    display: none;
  }

  .file-drop__icon {
    flex: none;
    color: var(--text-lo);
  }

  .file-drop--dragging .file-drop__icon {
    color: var(--accent);
  }

  .file-drop__text {
    display: flex;
    min-width: 0;
    flex: 1 1 180px;
    flex-direction: column;
    gap: 2px;
  }

  .file-drop__primary {
    color: var(--text-hi);
    font-size: var(--fs-body-md);
    line-height: 1.4;
  }

  .file-drop__name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-drop__meta {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    line-height: 1.45;
  }

  .file-drop__error {
    margin: 0;
    color: var(--red);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }

  @media (prefers-reduced-motion: reduce) {
    .file-drop {
      transition: none;
    }
  }
</style>
