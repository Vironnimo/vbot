<script>
  // Shared shell for ordinary form controls. It owns the visible label,
  // supporting help/error copy, spacing, and the ids that connect that copy to
  // the control. The caller keeps value/state ownership and renders the actual
  // control through the snippet contract.

  let {
    controlId = '',
    label = '',
    help = '',
    error = '',
    required = false,
    full = false,
    class: className = '',
    labelContent,
    children,
    actions,
    ...rest
  } = $props();

  let labelId = $derived(controlId ? `${controlId}-label` : undefined);
  let helpId = $derived(controlId && help ? `${controlId}-help` : undefined);
  let errorId = $derived(controlId && error ? `${controlId}-error` : undefined);
  let describedBy = $derived(
    [helpId, errorId].filter(Boolean).join(' ') || undefined,
  );
  let invalid = $derived(Boolean(error));
  let fieldClass = $derived(
    ['form-field', full ? 'form-field--full' : '', className]
      .filter(Boolean)
      .join(' '),
  );
  let controlContract = $derived({
    controlId,
    labelId,
    helpId,
    errorId,
    describedBy,
    invalid,
  });
</script>

<div {...rest} class={fieldClass}>
  {#if label || labelContent}
    <label class="form-field__label" id={labelId} for={controlId || undefined}>
      {#if labelContent}
        {@render labelContent()}
      {:else}
        {label}
      {/if}
      {#if required}<span class="form-field__required" aria-hidden="true"
          >*</span
        >{/if}
    </label>
  {/if}

  {@render children?.(controlContract)}

  {#if help}
    <small class="form-field__help" id={helpId}>{help}</small>
  {/if}
  {#if error}
    <small class="form-field__error" id={errorId} role="alert">{error}</small>
  {/if}
  {#if actions}
    {@render actions()}
  {/if}
</div>
