<script>
  // A Model id as text: the UI face with the provider path muted, so the
  // Model's own name stands out (webui/design.md -> Typography).
  import { modelIdParts } from '$lib/modelSelection.js';

  let { id = '', class: className = '' } = $props();

  let parts = $derived(modelIdParts(id));
</script>

<span class={['model-id', className]}
  >{#if parts.lead}<span class="model-id__lead">{parts.lead}</span
    >{/if}{parts.name}</span
>

<style>
  .model-id__lead {
    color: var(--text-lo);
  }
</style>
