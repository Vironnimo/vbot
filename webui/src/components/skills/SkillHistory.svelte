<script>
  // The recorded revisions of one editable package, newest first: what each
  // did, when and by whom, the files it changed, and Revert (not for a
  // revision that only recorded the package as found). It reads
  // `skill.history` whenever the package changes, which every inventory
  // reload does, so a revert shows up here; `onRevert(revision)` asks for the
  // revert, which actions.svelte.js confirms and sends.
  import { onDestroy, untrack } from 'svelte';
  import { skillHistory } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import {
    canRevertRevision,
    formatSkillTime,
    SKILL_HISTORY_LIMIT,
    skillActorLabel,
    skillFileChangeText,
    skillRevisionText,
  } from './skillRecords.js';

  const noop = () => {};

  let { entry, busy = false, onRevert = noop } = $props();

  let revisions = $state([]);
  let loading = $state(true);
  let error = $state('');
  let loadedKey = '';
  let version = 0;

  $effect(() => {
    const scope = entry.editable_scope;
    const name = entry.name;
    untrack(() => void load(scope, name));
  });

  onDestroy(() => {
    version++;
  });

  async function load(scope, name) {
    const current = ++version;
    const key = `${scope}/${name}`;
    // Another package starts empty; the same one keeps its list while it
    // reloads.
    if (key !== loadedKey) {
      revisions = [];
      loading = true;
    }
    try {
      const result = await skillHistory(scope, name, SKILL_HISTORY_LIMIT);
      if (current !== version) return;
      revisions = Array.isArray(result?.revisions) ? result.revisions : [];
      loadedKey = key;
      error = '';
    } catch (failure) {
      if (current === version) error = failure.message;
    } finally {
      if (current === version) loading = false;
    }
  }
</script>

{#if loading}
  <Banner variant="neutral">{t('skills.history.loading')}</Banner>
{:else if error}
  <Banner variant="error" role="alert"
    >{t('skills.history.loadError')}
    {error}<Button
      variant="secondary"
      onClick={() => load(entry.editable_scope, entry.name)}
      >{t('common.retry')}</Button
    ></Banner
  >
{:else if !revisions.length}
  <p class="skills-secondary">{t('skills.history.empty')}</p>
{:else}
  <ol class="skills-history">
    {#each revisions as revision (revision.id)}
      <li class="skills-history__item" data-revision-id={revision.id}>
        <div class="skills-history__head">
          <span class="skills-history__id">#{revision.id}</span>
          <span class="skills-history__what">{skillRevisionText(revision)}</span
          >
          <span class="skills-history__meta"
            >{formatSkillTime(revision.at)} · {skillActorLabel(
              revision.actor,
            )}</span
          >
          {#if canRevertRevision(revision)}
            <Button
              variant="tertiary"
              class="skills-history__revert"
              disabled={busy}
              ariaLabel={t('skills.history.revertNamed', { id: revision.id })}
              onClick={() => onRevert(revision)}
              >{t('skills.history.revert')}</Button
            >
          {/if}
        </div>
        {#if revision.files?.length}
          <ul class="skills-history__files">
            {#each revision.files as file (file.path)}<li>
                {skillFileChangeText(file)}
              </li>{/each}
          </ul>
        {/if}
      </li>
    {/each}
  </ol>
  {#if revisions.length >= SKILL_HISTORY_LIMIT}<p class="skills-secondary">
      {t('skills.history.limited', { count: SKILL_HISTORY_LIMIT })}
    </p>{/if}
{/if}
