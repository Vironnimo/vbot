<script>
  import { untrack } from 'svelte';
  import {
    composeContributions,
    contributionContext,
  } from '$lib/contributions.js';
  import { useContributions } from '$lib/contributions.svelte.js';
  import ContributionControl from './ContributionControl.svelte';

  let {
    surface,
    builtins = [],
    context = {},
    data = {},
    commands = {},
  } = $props();
  const registry = useContributions();
  const localContext = $derived(contributionContext(context));
  const entries = $derived(
    composeContributions(
      builtins,
      registry?.entries ?? [],
      surface,
      localContext,
      registry?.layout(surface),
    ),
  );
  $effect(() => {
    const items = builtins.map(({ id, title, order }) => ({
      id,
      title,
      order,
    }));
    return untrack(() => registry?.register(surface, items));
  });
</script>

{#each entries as entry (entry.id + ':' + (entry.epoch ?? 'core'))}
  {#if entry.render}
    {@render entry.render()}
  {:else}
    <ContributionControl
      descriptor={entry}
      {registry}
      context={localContext}
      {data}
      {commands}
    />
  {/if}
{/each}
