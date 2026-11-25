<script>
  import { getContext } from 'svelte';
  import { t } from '$lib/i18n.js';
  import ChatPermissionProfileReview from './ChatPermissionProfileReview.svelte';
  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import { contextMenuAnchor } from '../ui/contextMenu.js';

  // A Tool call waiting for the user's approval, shown at its Tool row (or at
  // the Sub-Agent row of the Session it waits in). It names the extra access
  // in plain words and answers the request (`permissions.resolve`): allow it
  // once, allow it for the rest of the Session (`remember` with the request's
  // suggested Session Grants, their exact variant or rules the user edits),
  // or reject it. The card leaves once the pending snapshot drops the request.
  let {
    request,
    // The Sub-Agent's Agent id when the request waits in a Sub-Agent Session.
    subAgent = '',
    onOpenSession = null,
  } = $props();

  let busy = $state(false);
  let sent = $state(false);
  let error = $state('');
  let menu = $state(null);
  let editing = $state(false);
  let draft = $state('');

  const resolveApproval = getContext('permissionApprovals')?.resolve;
  const grant = $derived(request?.session_grant ?? null);
  const folders = $derived(grant?.folders ?? []);
  const disabled = $derived(
    busy || sent || typeof resolveApproval !== 'function',
  );

  const ACCESS_VERBS = {
    read: () => t('approvals.need.read'),
    write: () => t('approvals.need.write'),
    start_dir: () => t('approvals.need.startDir'),
    command: () => t('approvals.need.command'),
    connect: () => t('approvals.need.connect'),
    web: () => t('approvals.need.web'),
  };

  // What one requirement asks for: a verb and the thing it reaches.
  function describe(requirement) {
    if (requirement.kind === 'review' && requirement.shown)
      return {
        verb: t('approvals.need.agentAccess'),
        target: requirement.shown,
      };
    if (requirement.kind === 'review')
      return { verb: t('approvals.need.review'), target: '' };
    const verb =
      requirement.kind === 'url'
        ? t('approvals.need.url')
        : (ACCESS_VERBS[requirement.access]?.() ?? t('approvals.need.other'));
    return {
      verb,
      target: requirement.shown || requirement.target,
      below: requirement.kind === 'path' && requirement.subtree,
    };
  }

  async function decide(decision, options = {}) {
    if (disabled) return;
    busy = true;
    error = '';
    try {
      await resolveApproval(request.id, decision, options);
      sent = true;
      editing = false;
    } catch (failure) {
      if (failure?.code === 'approval_not_found') sent = true;
      else if (options.remember && editing && failure?.message)
        error = failure.message;
      else error = t('approvals.error');
    } finally {
      busy = false;
    }
  }

  function allowSession(event) {
    if (grant.choose_folder) return openMenu(event);
    if (folders.length) return decide('allow', { read_folder: folders[0] });
    return decide('allow', { remember: grant.rules });
  }

  function sessionLabel() {
    if (grant.choose_folder) return t('approvals.session.chooseFolder');
    if (folders.length)
      return t('approvals.session.readFolder', {
        folder: folders[0].split('/').at(-1),
      });
    return t('approvals.allowSession');
  }

  function openMenu(event) {
    if (!grant) return;
    const items = folders.map((folder) => ({
      id: folder,
      label: folder,
      onSelect: () => decide('allow', { read_folder: folder }),
    }));
    if (!folders.length)
      items.push({
        id: 'default',
        label: t('approvals.session.default'),
        hint: grant.rules.join(', '),
        onSelect: () => decide('allow', { remember: grant.rules }),
      });
    if (grant.exact) {
      items.push({
        id: 'exact',
        label: t('approvals.session.exact'),
        hint: grant.exact.join(', '),
        onSelect: () => decide('allow', { remember: grant.exact }),
      });
    }
    if (grant.rules?.length)
      items.push({
        id: 'edit',
        label: t('approvals.session.edit'),
        group: 'edit',
        onSelect: startEditing,
      });
    menu = {
      ...contextMenuAnchor(event),
      wrapLabels: folders.length > 0,
      label: t('approvals.session.menu'),
      items,
    };
  }

  function startEditing() {
    draft = grant.rules.join('\n');
    error = '';
    editing = true;
  }

  const draftRules = $derived(
    draft
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
  );
</script>

<div
  class="approval-card"
  role="group"
  aria-label={t('approvals.title')}
  data-approval-id={request.id}
>
  <div class="approval-card__head">
    <svg
      class="approval-card__icon"
      viewBox="0 0 16 16"
      width="14"
      height="14"
      aria-hidden="true"
    >
      <path
        d="M8 1.8 2.8 3.9v3.6c0 3.1 2.2 5.6 5.2 6.7 3-1.1 5.2-3.6 5.2-6.7V3.9z"
      />
      <path d="M8 5.2v3.4M8 10.6v.2" />
    </svg>
    <span class="approval-card__title">
      {subAgent
        ? t('approvals.subAgentWaiting', { agent: subAgent })
        : t('approvals.waiting')}
    </span>
    {#if subAgent && onOpenSession}
      <Button
        variant="tertiary"
        class="approval-card__open"
        onClick={onOpenSession}>{t('approvals.openSession')}</Button
      >
    {/if}
  </div>
  <ul class="approval-card__needs">
    {#each Object.entries(request.shell_credential_sources ?? {}) as [name, source] (name)}
      <li>
        <span class="approval-card__verb">{t('approvals.need.credential')}</span
        >
        <code class="approval-card__target"
          >{name === source ? name : `${name} ← ${source}`}</code
        >
      </li>
    {/each}
    {#each request.requirements ?? [] as requirement, index (index)}
      <li>
        {#if requirement.permission_profile}
          <ChatPermissionProfileReview
            profile={requirement.permission_profile}
            command={requirement.shown}
          />
        {:else}
          {@const need = describe(requirement)}
          <span class="approval-card__verb">{need.verb}</span>
          {#if need.target}<code class="approval-card__target"
              >{need.target}</code
            >{/if}
          {#if need.below}<span class="approval-card__below"
              >{t('approvals.need.below')}</span
            >{/if}
        {/if}
      </li>
    {/each}
  </ul>
  {#if folders.length && !grant.choose_folder}
    <div class="approval-card__scope">
      {t('approvals.session.readScope')} <code>{folders[0]}</code>
    </div>
  {/if}
  {#if editing}
    <div class="approval-card__edit">
      <label class="approval-card__edit-label" for={`grant-${request.id}`}
        >{t('approvals.session.editLabel')}</label
      >
      <TextArea
        id={`grant-${request.id}`}
        code
        autoGrow
        rows={2}
        value={draft}
        onInput={(value) => (draft = value)}
        {disabled}
      />
      <p class="approval-card__hint">{t('approvals.session.editHelp')}</p>
    </div>
  {/if}
  {#if error}<p class="approval-card__error" role="alert">{error}</p>{/if}
  <div class="approval-card__actions">
    {#if editing}
      <Button
        variant="primary"
        disabled={disabled || draftRules.length === 0}
        onClick={() => decide('allow', { remember: draftRules })}
        >{t('approvals.session.allowEdited')}</Button
      >
      <Button
        variant="tertiary"
        {disabled}
        onClick={() => {
          editing = false;
          error = '';
        }}>{t('common.cancel')}</Button
      >
    {:else}
      <Button
        variant="primary"
        {disabled}
        data-approval="once"
        onClick={() => decide('allow')}>{t('approvals.allowOnce')}</Button
      >
      {#if grant}
        <span class="approval-card__split">
          <Button
            variant="secondary"
            {disabled}
            tooltip={t('approvals.session.tooltip', {
              rules: folders[0] ?? grant.rules.join(', '),
            })}
            data-approval="session"
            onClick={allowSession}>{sessionLabel()}</Button
          >
          {#if !grant.choose_folder}
            <Button
              variant="secondary"
              icon
              {disabled}
              ariaLabel={t('approvals.session.menu')}
              tooltip={t('approvals.session.menu')}
              data-approval="session-menu"
              onClick={openMenu}
            >
              <svg
                viewBox="0 0 16 16"
                width="10"
                height="10"
                aria-hidden="true"
              >
                <path d="M4 6l4 4 4-4" />
              </svg>
            </Button>
          {/if}
        </span>
      {/if}
      <Button
        variant="tertiary"
        {disabled}
        data-approval="reject"
        onClick={() => decide('reject')}>{t('approvals.reject')}</Button
      >
    {/if}
  </div>
  <details class="approval-card__details">
    <summary>{t('approvals.details')}</summary>
    <dl>
      <dt>{t('approvals.tool')}</dt>
      <dd>{request.tool_name}</dd>
      {#if request.reason}<dt>{t('approvals.reason')}</dt>
        <dd>{request.reason}</dd>{/if}
      <dt>{t('approvals.computer')}</dt>
      <dd>{request.execution?.computer_id ?? 'server'}</dd>
      {#if request.cwd}<dt>{t('approvals.cwd')}</dt>
        <dd>{request.cwd}</dd>{/if}
    </dl>
    {#each request.requirements ?? [] as requirement, index (index)}
      {#if requirement.resolved && requirement.resolved !== requirement.target}<div
        >
          {t('approvals.resolved')}: <code>{requirement.resolved}</code>
        </div>{/if}
      {#if requirement.filtered}<div>{t('approvals.filtered')}</div>{/if}
      {#if requirement.host_aliases?.length > 1}<div>
          {t('approvals.hostAliases')}: {requirement.host_aliases.join(', ')}
        </div>{/if}
      {#if requirement.glob}<div>
          {t('approvals.glob')}: <code>{requirement.glob}</code>
        </div>{/if}
      {#if requirement.pruned?.length}<div>
          {t('approvals.excluded')}: {requirement.pruned.join(', ')}
        </div>{/if}
      {#if requirement.git}<div>{t('approvals.git')}</div>{/if}
    {/each}
    <div class="approval-card__label">{t('approvals.invocation')}</div>
    <pre>{JSON.stringify(request.arguments, null, 2)}</pre>
    {#if request.display_redacted}<p>{t('approvals.redacted')}</p>{/if}
  </details>
</div>

<ContextMenu {menu} onClose={() => (menu = null)} />

<style>
  .approval-card {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--space-sm);
    margin: var(--space-xs) 0 var(--space-sm);
    padding: 10px 12px;
    border: 1px solid color-mix(in srgb, var(--amber) 45%, var(--border));
    border-radius: var(--r-md);
    background: var(--amber-dim);
    font-size: var(--fs-body-sm);
    color: var(--text-body);
  }
  .approval-card__head {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }
  .approval-card__icon {
    flex-shrink: 0;
    fill: none;
    stroke: var(--amber);
    stroke-width: 1.4;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .approval-card__title {
    flex: 1;
    min-width: 0;
    font-weight: 500;
    color: var(--text-hi);
  }
  .approval-card__needs {
    display: grid;
    gap: 2px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .approval-card__verb {
    color: var(--text-med);
    margin-right: 6px;
  }
  .approval-card code,
  .approval-card pre {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .approval-card__scope {
    overflow-wrap: anywhere;
  }
  .approval-card__target {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-hi);
  }
  .approval-card__below,
  .approval-card__hint {
    color: var(--text-lo);
  }
  .approval-card__below {
    margin-left: 6px;
  }
  .approval-card__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .approval-card__split {
    display: inline-flex;
    gap: 1px;
  }
  .approval-card__edit {
    display: grid;
    gap: 4px;
  }
  .approval-card__edit-label {
    color: var(--text-med);
  }
  .approval-card__hint,
  .approval-card__error {
    margin: 0;
  }
  .approval-card__error {
    color: var(--red);
  }
  .approval-card__details summary {
    cursor: pointer;
    color: var(--text-lo);
    width: max-content;
  }
  .approval-card__details dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 2px 12px;
    margin: 6px 0;
  }
  .approval-card__details dt,
  .approval-card__label {
    color: var(--text-lo);
  }
  .approval-card__details dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .approval-card__details pre {
    margin: 4px 0 0;
    max-height: 16rem;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .approval-card__details p {
    margin: 4px 0 0;
    color: var(--text-lo);
  }
</style>
