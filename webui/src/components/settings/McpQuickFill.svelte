<script>
  // Fills a new connection from the command line or URL a server's setup
  // instructions show. `onFill` receives the text and resolves once the form
  // was filled or handed on; a rejection shows its message here. It renders
  // as one stacked Settings row: label and hint, then the field with its
  // Fill in button.
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import { t } from '$lib/i18n.js';

  const noop = async () => {};
  const componentId = $props.id();

  let { disabled = false, onFill = noop } = $props();
  let text = $state('');
  let error = $state('');
  let filling = $state(false);
  let controlId = `${componentId}-text`;

  async function fill() {
    if (!text.trim() || filling) return;
    filling = true;
    error = '';
    try {
      await onFill(text);
      text = '';
    } catch (failure) {
      error = failure.message;
    } finally {
      filling = false;
    }
  }
</script>

<div class="s-row s-row--stacked mcp-quick-fill">
  <div class="s-row-info">
    <label class="s-row-label" for={controlId}>{t('mcp.quickFill')}</label>
    <div class="s-row-desc" id={`${controlId}-help`}>
      {t('mcp.quickFillHelp')}
    </div>
  </div>
  <div class="mcp-quick-fill__line">
    <TextField
      id={controlId}
      aria-describedby={error
        ? `${controlId}-help ${controlId}-error`
        : `${controlId}-help`}
      invalid={Boolean(error)}
      code
      autocomplete="off"
      spellcheck="false"
      placeholder={t('mcp.quickFillPlaceholder')}
      value={text}
      disabled={disabled || filling}
      onInput={(value) => {
        text = value;
        error = '';
      }}
      onkeydown={(event) => {
        // Enter fills from the text instead of saving the connection.
        if (event.key !== 'Enter') return;
        event.preventDefault();
        void fill();
      }}
    />
    <Button
      variant="secondary"
      disabled={disabled || filling || !text.trim()}
      loading={filling}
      onClick={fill}>{t('mcp.quickFillApply')}</Button
    >
  </div>
  {#if error}
    <p class="s-row-error" id={`${controlId}-error`}>{error}</p>
  {/if}
</div>
