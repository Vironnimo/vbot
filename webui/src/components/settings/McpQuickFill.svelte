<script>
  // Fills a new connection from the command line or URL a server's setup
  // instructions show. `onFill` receives the text and resolves once the form
  // was filled or handed on; a rejection shows its message here.
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import { t } from '$lib/i18n.js';

  const noop = async () => {};
  const componentId = $props.id();

  let { disabled = false, onFill = noop } = $props();
  let text = $state('');
  let error = $state('');
  let filling = $state(false);

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

<div class="mcp-quick-fill">
  <FormField
    controlId={`${componentId}-text`}
    label={t('mcp.quickFill')}
    help={t('mcp.quickFillHelp')}
    {error}
  >
    {#snippet children(field)}<TextField
        id={field.controlId}
        aria-describedby={field.describedBy}
        invalid={field.invalid}
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
      />{/snippet}
  </FormField>
  <Button
    variant="secondary"
    disabled={disabled || filling || !text.trim()}
    loading={filling}
    onClick={fill}>{t('mcp.quickFillApply')}</Button
  >
</div>
