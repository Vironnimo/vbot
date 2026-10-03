<script>
  // The Librarian of one Identity Agent on its Skills manager page: the
  // schedule, the last pass (what it retired, merged, changed and created)
  // with the Librarian Session of its merge, the next scheduled pass, the
  // revisions the last pass recorded with links into each Skill's history,
  // one action that reverts them together, and Run now. It reads
  // `librarian.status` whenever the inventory reloads; the server announces
  // the start and end of a pass as a Skills change, which reloads the
  // inventory. An Agent without scheduled passes shows the one reason the
  // status names: maintenance is off in Settings, off for this Agent, the
  // Agent has no Skills of its own, or the Librarian is unavailable; all but
  // the first also block Run now.
  import { onDestroy, untrack } from 'svelte';
  import { librarianStatus, runLibrarian } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    LIBRARIAN_AGENT_ID,
    librarianMergeText,
    librarianProblemText,
    librarianTriggerText,
  } from '$lib/librarian.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import { formatSkillTime, skillRevisionText } from './skillRecords.js';

  const noop = () => {};
  const uid = $props.id();

  let {
    agent,
    inventory = [],
    archived = [],
    busy = false,
    onOpenHistory = noop,
    onOpenArchived = noop,
    onRevertPass = noop,
    onOpenSession = noop,
    onToast = noop,
  } = $props();

  let status = $state(null);
  let error = $state('');
  let starting = $state(false);
  let loadedId = '';
  let version = 0;
  let disposed = false;

  let scope = $derived(`agent:${agent.id}`);
  let settings = $derived(status?.settings ?? {});
  let lastPass = $derived(status?.last_pass ?? null);
  let changes = $derived(
    Array.isArray(status?.changes)
      ? status.changes.filter((revision) => Number.isInteger(revision?.id))
      : [],
  );
  let unavailableText = $derived(unavailableReason(status));
  let runBlocked = $derived(
    status?.available === false
      ? unavailableText || t('skills.librarian.runError')
      : status?.running
        ? t('skills.librarian.alreadyRunning')
        : '',
  );

  $effect(() => {
    const agentId = agent.id;
    void inventory;
    untrack(() => void load(agentId));
  });

  onDestroy(() => {
    disposed = true;
    version++;
  });

  async function load(agentId) {
    const current = ++version;
    // Another Agent starts empty; the same one keeps its status while it
    // reloads.
    if (agentId !== loadedId) {
      status = null;
      error = '';
    }
    try {
      const result = await librarianStatus(agentId);
      if (current !== version) return;
      status = result ?? {};
      loadedId = agentId;
      error = '';
    } catch (failure) {
      if (current === version) error = failure.message;
    }
  }

  // Why no pass can run for the Agent (`available` is false).
  function unavailableReason(value) {
    switch (value?.unscheduled_reason) {
      case 'agent_disabled':
        return t('skills.librarian.agentOff');
      case 'no_skills':
        return t('skills.librarian.noSkills');
      case 'librarian_unavailable':
        return t('skills.librarian.unavailable', {
          problem: librarianProblemText(value.librarian_problem),
        });
      default:
        return '';
    }
  }

  function runErrorText(failure) {
    if (failure?.code === 'agent_busy') return t('skills.librarian.busy');
    if (failure?.code === 'invalid_request' && unavailableText)
      return unavailableText;
    return `${t('skills.librarian.runError')} ${failure?.message ?? ''}`.trim();
  }

  async function start() {
    if (starting) return;
    const agentId = agent.id;
    starting = true;
    try {
      const result = await runLibrarian(agentId);
      if (!disposed && agentId === agent.id) {
        version++;
        status = result ?? {};
        loadedId = agentId;
      }
      onToast({
        title: t('skills.librarian.started', {
          name: agent.name || agentId,
        }),
        variant: 'success',
      });
    } catch (failure) {
      onToast({
        title: runErrorText(failure),
        variant: failure?.code === 'agent_busy' ? 'warn' : 'error',
      });
      if (!disposed && agentId === agent.id) void load(agentId);
    } finally {
      starting = false;
    }
  }

  function scheduleText() {
    if (settings.enabled === false) return t('skills.librarian.scheduleOff');
    if (!Number.isInteger(settings.interval_days)) return '';
    return settings.interval_days === 1
      ? t('skills.librarian.daily')
      : t('skills.librarian.everyDays', { days: settings.interval_days });
  }

  function nextPassText() {
    const next = Date.parse(
      String(status?.next_due_at ?? '').replace(/(\.\d{3})\d+/, '$1'),
    );
    return Number.isFinite(next) && next > Date.now()
      ? formatSkillTime(status.next_due_at)
      : t('skills.librarian.due');
  }

  // Each change links to its Skill's history, or to the archive when the
  // Skill is archived now; a purged Skill stays plain text.
  function changeTarget(revision) {
    const entry = inventory.find(
      (item) => item.editable_scope === scope && item.name === revision.skill,
    );
    if (entry)
      return {
        label: t('skills.librarian.openHistory', { name: revision.skill }),
        open: () => onOpenHistory(entry),
      };
    if (
      archived.some(
        (item) => item.scope === scope && item.name === revision.skill,
      )
    )
      return {
        label: t('skills.librarian.openArchived', { name: revision.skill }),
        open: () => onOpenArchived(revision.skill),
      };
    return null;
  }
</script>

<section class="skills-librarian" aria-labelledby={`${uid}-title`}>
  <div class="skills-librarian__head">
    <h3 id={`${uid}-title`} class="skills-librarian__title">
      {t('skills.librarian.title')}
    </h3>
    <InfoHint text={t('skills.librarian.help')} />
    <Button
      variant="secondary"
      class="skills-librarian__run"
      disabled={!status || Boolean(runBlocked) || busy || starting}
      disabledReason={runBlocked}
      loading={starting}
      onClick={start}>{t('skills.librarian.run')}</Button
    >
  </div>

  {#if error}
    <Banner variant="error" role="alert"
      >{t('skills.librarian.loadError')}
      {error}<Button variant="secondary" onClick={() => load(agent.id)}
        >{t('common.retry')}</Button
      ></Banner
    >
  {:else if status}
    {#if status.available === false && unavailableText}
      <p class="skills-page-note">{unavailableText}</p>
    {/if}
    <dl class="skills-page-facts">
      {#if status.available !== false && scheduleText()}
        <div class="skills-page-fact">
          <dt>{t('skills.librarian.schedule')}</dt>
          <dd>{scheduleText()}</dd>
        </div>
      {/if}
      {#if status.available !== false && !status.unscheduled_reason && settings.enabled === true}
        <div class="skills-page-fact">
          <dt>{t('skills.librarian.next')}</dt>
          <dd>{nextPassText()}</dd>
        </div>
      {/if}
      {#if status.running}
        <div class="skills-page-fact">
          <dt>{t('skills.librarian.now')}</dt>
          <dd>
            {t('skills.librarian.runningSince', {
              time: formatSkillTime(status.running_since),
            })}
          </dd>
        </div>
        {#if typeof status.running_session_id === 'string' && status.running_session_id}
          <div class="skills-page-fact">
            <dt>{t('skills.librarian.session')}</dt>
            <dd>
              <button
                type="button"
                class="skills-librarian__link"
                onclick={() =>
                  onOpenSession(LIBRARIAN_AGENT_ID, status.running_session_id)}
                >{t('skills.librarian.openSession')}</button
              >
            </dd>
          </div>
        {/if}
      {/if}
      {#if lastPass}
        <div class="skills-page-fact">
          <dt>{t('skills.librarian.lastPass')}</dt>
          <dd>
            {t('skills.librarian.passValue', {
              time: formatSkillTime(lastPass.finished_at),
              trigger: librarianTriggerText(lastPass),
            })}
          </dd>
        </div>
        {#if lastPass.outcome === 'failed' || lastPass.outcome === 'interrupted'}
          <div class="skills-page-fact">
            <dt>{t('skills.librarian.result')}</dt>
            <dd>
              {lastPass.outcome === 'failed'
                ? t('skills.librarian.resultFailed')
                : t('skills.librarian.resultInterrupted')}
            </dd>
          </div>
        {/if}
        <div class="skills-page-fact">
          <dt>{t('skills.librarian.retired')}</dt>
          <dd>{lastPass.archived ?? 0}</dd>
        </div>
        <div class="skills-page-fact">
          <dt>{t('skills.librarian.merging')}</dt>
          <dd>{librarianMergeText(lastPass)}</dd>
        </div>
        {#if typeof lastPass.session_id === 'string' && lastPass.session_id}
          <div class="skills-page-fact">
            <dt>{t('skills.librarian.session')}</dt>
            <dd>
              <button
                type="button"
                class="skills-librarian__link"
                onclick={() =>
                  onOpenSession(LIBRARIAN_AGENT_ID, lastPass.session_id)}
                >{t('skills.librarian.openSession')}</button
              >
            </dd>
          </div>
        {/if}
      {/if}
    </dl>
    {#if !lastPass}
      <p class="skills-page-note">{t('skills.librarian.never')}</p>
    {:else if changes.length}
      <details class="skills-librarian__changes">
        <summary
          >{t('skills.librarian.changes', { count: changes.length })}</summary
        >
        <ol class="skills-history">
          {#each changes as revision (revision.id)}
            {@const target = changeTarget(revision)}
            <li class="skills-history__item" data-revision-id={revision.id}>
              <div class="skills-history__head">
                <span class="skills-history__id">#{revision.id}</span>
                {#if target}
                  <button
                    type="button"
                    class="skills-librarian__skill"
                    aria-label={target.label}
                    onclick={target.open}>{revision.skill}</button
                  >
                {:else}
                  <span class="skills-librarian__skill">{revision.skill}</span>
                {/if}
                <span class="skills-history__what"
                  >{skillRevisionText(revision)}</span
                >
                <span class="skills-history__meta"
                  >{formatSkillTime(revision.at)}</span
                >
              </div>
            </li>
          {/each}
        </ol>
        <Button
          variant="secondary"
          class="skills-librarian__revert"
          disabled={busy}
          onClick={() =>
            onRevertPass(
              scope,
              changes.map((revision) => revision.id),
            )}>{t('skills.revert.togetherAction')}</Button
        >
      </details>
    {:else}
      <p class="skills-page-note">{t('skills.librarian.noChanges')}</p>
    {/if}
  {/if}
</section>
