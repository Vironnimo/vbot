<script>
  import {
    clearSessionCeiling,
    getSessionPermissions,
    removeSessionGrants,
    setSessionPermissions,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { permissionProfileLabel } from '$lib/permissionRules.js';
  import { floatingHoverCard } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import InfoHint from '../ui/InfoHint.svelte';

  // The access of a Session (or a draft's next Session) in the composer: a
  // lock with the name of the Permission Profile that applies, amber when its
  // Runs work under Permission Rules. Its card is a short choice: the Agent's
  // default (named by the profile it uses) or one of the profiles, which the
  // Session keeps by name (`permissions.set_session_rules`). Full Access also
  // removes inherited limits, so with stored ones it asks first. Limits the
  // Session inherited (from the Session that started it, a scheduled job or an
  // Extension) appear as one line each, removable after a confirmation
  // (`permissions.clear_session_ceiling`). The rules that apply, the Session
  // Grants (each removable, `permissions.remove_session_grants`) and the
  // honest limits of the checks (`.vorch/domain-maps/permissions.md`) wait
  // behind Details. The limits change elsewhere (the Agent's rules, the
  // profiles, a Project override, the stored ceiling), so they are read again
  // when `revision` changes and whenever the card is about to open.
  let {
    agentAddress = '',
    sessionId = '',
    // A draft's chosen Working Project (null: the Agent's Workspace);
    // undefined for a Session or a Team member, whose folder is known.
    workingProjectId = undefined,
    // Changes when the server may have changed the Agent or the Session.
    revision = '',
  } = $props();

  let summary = $state(null);
  // The confirmation shown: 'clear' (inherited limits) or 'full' (full access).
  let confirming = $state('');
  let clearing = $state(false);
  let clearError = $state('');
  let choosing = $state(false);
  let chooseError = $state('');
  let removingGrant = $state('');
  let grantError = $state('');
  let sequence = 0;

  let requestKey = $derived(
    JSON.stringify([
      agentAddress,
      sessionId,
      workingProjectId ?? '\u0000',
      revision,
    ]),
  );

  $effect(() => {
    const key = requestKey;
    void load(key);
  });

  async function load(key) {
    const ticket = ++sequence;
    if (!agentAddress) {
      summary = null;
      return;
    }
    try {
      const response = await getSessionPermissions({
        agentId: agentAddress,
        sessionId,
        workingProjectId,
      });
      if (ticket !== sequence || key !== requestKey) return;
      summary = response;
    } catch {
      // Unknown limits show no mark; the Tools still enforce them.
      if (ticket === sequence) summary = null;
    }
  }

  function errorText(error, fallback) {
    return typeof error?.message === 'string' && error.message.trim()
      ? error.message.trim()
      : fallback;
  }

  async function clearInherited() {
    clearing = true;
    clearError = '';
    try {
      await clearSessionCeiling({ agentId: agentAddress, sessionId });
      confirming = '';
      await load(requestKey);
    } catch (error) {
      clearError = errorText(error, t('permissions.session.clearFailed'));
    } finally {
      clearing = false;
    }
  }

  async function removeGrant(rule, folder, computer) {
    removingGrant = rule;
    grantError = '';
    try {
      await removeSessionGrants({
        agentId: agentAddress,
        sessionId,
        ...(folder
          ? { readFolder: folder, computerId: computer }
          : { rules: [rule] }),
      });
      await load(requestKey);
    } catch (error) {
      grantError = errorText(error, t('permissions.session.grantsFailed'));
    } finally {
      removingGrant = '';
    }
  }

  const DEFAULT_CHOICE = '';
  const RULES_CHOICE = 'rules';
  const FULL_ACCESS = 'full_access';

  function profileHint(profile) {
    if (profile.name === FULL_ACCESS)
      return t('permissions.session.fullAccessHint');
    if (profile.name === 'restricted')
      return t('permissions.session.restrictedHint');
    return profile.restricted
      ? t('permissions.session.customProfileHint')
      : t('permissions.session.fullAccessHint');
  }

  // The Session's choice: a profile name, older rules of its own, or the
  // Agent's default.
  let choice = $derived(
    summary?.session_profile
      ? `profile:${summary.session_profile}`
      : summary?.session_rules
        ? RULES_CHOICE
        : DEFAULT_CHOICE,
  );
  let profiles = $derived(summary?.profiles ?? []);
  let missingProfile = $derived(
    summary?.session_profile &&
      !profiles.some(({ name }) => name === summary.session_profile)
      ? summary.session_profile
      : '',
  );
  let agentDefaultLabel = $derived(
    summary?.agent_profile
      ? permissionProfileLabel(summary.agent_profile)
      : t('permissions.session.agentRules'),
  );
  let restricted = $derived(Boolean(summary?.restricted));
  // The name on the lock: what the Session uses. Full Access under inherited
  // limits is no full access, so the lock then says Restricted.
  let accessLabel = $derived.by(() => {
    if (!summary) return '';
    let name = summary.session_profile ?? null;
    if (!name && summary.session_rules)
      return t('permissions.session.customRules');
    name ??= summary.agent_profile;
    if (!name || (name === FULL_ACCESS && restricted))
      return restricted
        ? t('permissions.session.badge')
        : permissionProfileLabel(FULL_ACCESS);
    return permissionProfileLabel(name);
  });

  function choose(value) {
    if (choosing || value === choice) return;
    chooseError = '';
    // Full access also removes the limits the Session inherited.
    if (value === `profile:${FULL_ACCESS}` && summary?.stored_ceiling) {
      confirming = 'full';
      return;
    }
    void applyAccess(value);
  }

  async function applyAccess(value) {
    choosing = true;
    clearError = '';
    try {
      await setSessionPermissions({
        agentId: agentAddress,
        sessionId,
        profile: value ? value.slice('profile:'.length) : null,
      });
      confirming = '';
      await load(requestKey);
    } catch (error) {
      const message = errorText(error, t('permissions.session.accessFailed'));
      if (confirming) clearError = message;
      else chooseError = message;
    } finally {
      choosing = false;
    }
  }

  function cancelConfirm() {
    if (clearing || choosing) return;
    confirming = '';
    clearError = '';
  }

  const SCRATCH = '{scratch}';
  const SEPARATOR = ', ';

  // A row's value as items: the folders or rules of a list (`mono`), or one
  // word for no limit or nothing. The rules keep `{scratch}`, the
  // conversation's scratch area, which has no folder before a Run creates it:
  // it reads as a name, and a rule below it is explained under the rows.
  function listed(values, anything) {
    if (values === null || values === undefined) return [{ text: anything }];
    if (values.length === 0)
      return [{ text: t('permissions.preview.nothing') }];
    return values.map((value) =>
      value === SCRATCH
        ? { text: t('permissions.session.scratch') }
        : { text: value, mono: true },
    );
  }

  function codes(values) {
    return values.map((value) => ({ text: value, mono: true }));
  }

  const SOURCE_LABELS = {
    agent: () => t('permissions.session.source.agent'),
    parent: () => t('permissions.session.source.parent'),
    extension: () => t('permissions.session.source.extension'),
    cron: () => t('permissions.session.source.cron'),
  };

  function sourceLabel(source) {
    return (
      SOURCE_LABELS[source] ?? (() => t('permissions.session.source.other'))
    )();
  }

  function layerRows(layer, withHeading) {
    const rows = [];
    if (withHeading) rows.push({ label: sourceLabel(layer.source), items: [] });
    if (layer.workdir) {
      rows.push({
        label: t('permissions.session.workdir'),
        items: codes([layer.workdir]),
      });
    }
    rows.push(
      {
        label: t('permissions.preview.read'),
        items: listed(layer.read, t('permissions.preview.anywhere')),
      },
      {
        label: t('permissions.preview.write'),
        items: listed(layer.write, t('permissions.preview.anywhere')),
      },
      {
        label: t('permissions.preview.commands'),
        items: listed(layer.commands, t('permissions.preview.anyCommand')),
      },
      {
        label: t('permissions.preview.web'),
        items: listed(layer.web, t('permissions.preview.anySite')),
      },
    );
    if ((layer.deny ?? []).length > 0) {
      rows.push({
        label: t('permissions.preview.deny'),
        items: codes(layer.deny),
      });
    }
    if ((layer.inactive ?? []).length > 0) {
      rows.push({
        label: t('permissions.preview.inactive'),
        items: codes(layer.inactive),
      });
    }
    return rows;
  }

  let layers = $derived(restricted ? (summary?.layers ?? []) : []);
  // A source heading only separates several sets of limits.
  let rows = $derived(
    layers.flatMap((layer) => layerRows(layer, layers.length > 1)),
  );
  // Limits the Session inherited, one line each in the card.
  let inherited = $derived(layers.filter(({ source }) => source !== 'agent'));
  // A folder or rule below the scratch area still names `{scratch}`.
  let namesScratch = $derived(
    rows.some((row) =>
      row.items.some((item) => item.mono && item.text.includes(SCRATCH)),
    ),
  );
  let clearable = $derived(
    Boolean(restricted && summary?.stored_ceiling && sessionId),
  );
  let grants = $derived(
    sessionId
      ? [
          ...(summary?.session_grants ?? []).map((rule) => ({ rule })),
          ...(summary?.session_read_folders ?? []).map(
            ({ folder, computer_id: computer }) => ({
              rule: t('permissions.session.readFolder', { folder, computer }),
              folder,
              computer,
            }),
          ),
        ]
      : [],
  );
</script>

{#snippet option(value, label, hint, disabled = false)}
  <button
    type="button"
    role="radio"
    class="session-permissions-card__choice"
    aria-checked={choice === value ||
      (value === 'missing' && Boolean(missingProfile))}
    disabled={choosing || disabled}
    onclick={() => choose(value)}
    data-access-choice={value || 'agent'}
  >
    <span class="session-permissions-card__radio" aria-hidden="true"></span>
    <span class="session-permissions-card__choice-text">
      <span class="session-permissions-card__choice-label">{label}</span>
      {#if hint}
        <span class="session-permissions-card__choice-hint">{hint}</span>
      {/if}
    </span>
  </button>
{/snippet}

{#if summary}
  <span class="session-permissions-anchor">
    <button
      type="button"
      class="session-permissions"
      class:session-permissions--full={!restricted}
      onpointerenter={() => void load(requestKey)}
      onfocus={() => void load(requestKey)}
      aria-label={t('permissions.session.badgeLabel', { access: accessLabel })}
      data-session-permissions={restricted ? 'restricted' : 'full'}
    >
      <svg
        class="session-permissions__icon"
        viewBox="0 0 16 16"
        width="13"
        height="13"
        aria-hidden="true"
      >
        <rect x="3.5" y="7" width="9" height="6.5" rx="1" />
        {#if restricted}
          <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" />
        {:else}
          <path d="M5.5 7V5a2.5 2.5 0 0 1 4.9-.7" />
        {/if}
      </svg>
      <span class="session-permissions__label" data-session-access-label>
        {accessLabel}
      </span>
    </button>
    <div
      class="floating-card session-permissions-card"
      use:floatingHoverCard={{ openOnPress: true }}
      data-session-permissions-card
    >
      <div class="session-permissions-card__title">
        {t('permissions.session.access')}
        <InfoHint text={t('permissions.session.accessHelp')} />
      </div>

      {#if sessionId}
        <div
          class="session-permissions-card__choices"
          role="radiogroup"
          aria-label={t('permissions.session.access')}
          data-session-access
        >
          {@render option(
            DEFAULT_CHOICE,
            t('permissions.session.agentDefault', {
              profile: agentDefaultLabel,
            }),
            '',
          )}
          {#each profiles as profile (profile.name)}
            {@render option(
              `profile:${profile.name}`,
              permissionProfileLabel(profile.name),
              profileHint(profile),
            )}
          {/each}
          {#if missingProfile}
            {@render option(
              'missing',
              t('permissions.profile.missing', { name: missingProfile }),
              '',
              true,
            )}
          {/if}
          {#if choice === RULES_CHOICE}
            {@render option(
              RULES_CHOICE,
              t('permissions.session.customRules'),
              '',
              true,
            )}
          {/if}
        </div>
        {#if summary.session_profile_error}
          <p class="session-permissions-card__error" role="alert">
            {summary.session_profile_error}
          </p>
        {/if}
        {#if chooseError}
          <p class="session-permissions-card__error" role="alert">
            {chooseError}
          </p>
        {/if}
      {:else}
        <p class="session-permissions-card__current" data-session-draft-access>
          {t('permissions.session.agentDefault', {
            profile: agentDefaultLabel,
          })}
        </p>
        <p class="session-permissions-card__note">
          {t('permissions.session.draftHint')}
        </p>
      {/if}

      {#if inherited.length > 0}
        <div class="session-permissions-card__inherited" data-session-inherited>
          {#each inherited as layer, index (index)}
            <p>{sourceLabel(layer.source)}</p>
          {/each}
          {#if clearable}
            <Button
              variant="tertiary"
              class="session-permissions-card__action"
              onClick={() => (confirming = 'clear')}
            >
              {t('permissions.session.clear')}
            </Button>
          {/if}
        </div>
      {/if}

      {#if rows.length > 0 || grants.length > 0}
        <details
          class="session-permissions-card__details"
          data-session-permissions-details
        >
          <summary>
            {grants.length > 0
              ? t('permissions.session.detailsWithGrants', {
                  count: grants.length,
                })
              : t('permissions.session.details')}
          </summary>
          <!-- Only the details scroll: the choice stays in view on a short
               window. -->
          <div class="session-permissions-card__body">
            {#if rows.length > 0}
              <dl class="app-tooltip__rows">
                {#each rows as row, index (index)}
                  <dt class="app-tooltip__label">{row.label}</dt>
                  <dd class="app-tooltip__value">
                    {#each row.items as item, position (position)}
                      {#if position > 0}{SEPARATOR}{/if}<span
                        class:app-tooltip__mono={item.mono}>{item.text}</span
                      >
                    {/each}
                  </dd>
                {/each}
              </dl>
              {#if namesScratch}
                <p class="session-permissions-card__note">
                  {t('permissions.preview.scratch', { scratch: SCRATCH })}
                </p>
              {/if}
            {/if}
            {#if grants.length > 0}
              <div class="session-permissions-card__grants" data-session-grants>
                <div class="session-permissions-card__label">
                  {t('permissions.session.grants')}
                  <InfoHint text={t('permissions.session.grantsHelp')} />
                </div>
                <ul>
                  {#each grants as { rule, folder, computer } (rule)}
                    <li>
                      <span class="app-tooltip__mono">{rule}</span>
                      <Button
                        variant="tertiary"
                        icon
                        class="session-permissions-card__remove"
                        loading={removingGrant === rule}
                        disabled={Boolean(removingGrant)}
                        tooltip={t('permissions.session.grantRemove')}
                        ariaLabel={t('permissions.session.grantRemoveRule', {
                          rule,
                        })}
                        onClick={() => removeGrant(rule, folder, computer)}
                      >
                        <svg
                          viewBox="0 0 16 16"
                          width="12"
                          height="12"
                          aria-hidden="true"
                        >
                          <path d="m4 4 8 8M12 4l-8 8" />
                        </svg>
                      </Button>
                    </li>
                  {/each}
                </ul>
                {#if grantError}
                  <p class="session-permissions-card__error" role="alert">
                    {grantError}
                  </p>
                {/if}
              </div>
            {/if}
            {#if restricted}
              <p class="session-permissions-card__note">
                {t('permissions.limits')}
                <InfoHint text={t('permissions.limitsHelp')} />
              </p>
            {/if}
          </div>
        </details>
      {/if}
    </div>
  </span>
{/if}

{#if confirming === 'clear'}
  <ConfirmDialog
    title={t('permissions.session.clearTitle')}
    body={t('permissions.session.clearText')}
    confirmLabel={t('permissions.session.clearConfirm')}
    busy={clearing}
    error={clearError}
    onConfirm={clearInherited}
    onCancel={cancelConfirm}
  />
{:else if confirming === 'full'}
  <ConfirmDialog
    title={t('permissions.session.fullConfirmTitle')}
    body={t('permissions.session.fullConfirmText')}
    confirmLabel={t('permissions.session.fullConfirm')}
    busy={choosing}
    error={clearError}
    onConfirm={() => applyAccess(`profile:${FULL_ACCESS}`)}
    onCancel={cancelConfirm}
  />
{/if}

<style>
  .session-permissions-anchor {
    display: inline-flex;
    flex-shrink: 0;
  }
  .session-permissions {
    display: inline-flex;
    flex-shrink: 0;
    gap: 4px;
    align-items: center;
    height: 26px;
    padding: 0 7px;
    border: 1px solid transparent;
    border-radius: var(--r-md);
    color: var(--amber);
    background: transparent;
    font: inherit;
    font-size: var(--fs-label-sm);
    cursor: default;
  }
  .session-permissions--full {
    color: var(--text-lo);
  }
  button.session-permissions:hover,
  button.session-permissions:focus-visible {
    background: var(--surface-3);
  }
  button.session-permissions:focus-visible {
    box-shadow: var(--field-focus-ring);
    outline: none;
  }
  .session-permissions__icon {
    flex-shrink: 0;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.3;
  }
  .session-permissions__label {
    max-width: 14ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* The card on the shared floating card (styles/app/hints.css), portaled to
     <body>; the detail rows use the details-card styles of the quick tooltip. */
  .session-permissions-card.floating-card {
    display: flex;
    flex-direction: column;
    width: min(320px, calc(100vw - 16px));
    max-width: min(320px, calc(100vw - 16px));
    max-height: min(520px, calc(100vh - 16px));
    overflow: hidden;
    font-size: var(--fs-label-sm);
    line-height: 1.4;
    user-select: text;
  }
  .session-permissions-card__title {
    flex-shrink: 0;
    margin-bottom: 6px;
    color: var(--text-hi);
    font-weight: 600;
  }
  .session-permissions-card__title :global(.info-hint),
  .session-permissions-card__label :global(.info-hint),
  .session-permissions-card__note :global(.info-hint) {
    margin-left: 2px;
    vertical-align: -3px;
  }
  .session-permissions-card__choices {
    display: grid;
    flex-shrink: 0;
    gap: 2px;
  }
  .session-permissions-card__choice {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    width: 100%;
    padding: 5px 6px;
    border: 0;
    border-radius: var(--r-md);
    color: var(--text-lo);
    background: transparent;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .session-permissions-card__choice:hover:not(:disabled) {
    background: var(--surface-3);
  }
  .session-permissions-card__choice:focus-visible {
    box-shadow: var(--field-focus-ring);
    outline: none;
  }
  .session-permissions-card__choice:disabled {
    cursor: default;
  }
  .session-permissions-card__choice[aria-checked='true'] {
    color: var(--text-hi);
  }
  .session-permissions-card__radio {
    flex-shrink: 0;
    width: 12px;
    height: 12px;
    margin-top: 2px;
    border: 1px solid var(--border-2);
    border-radius: 50%;
  }
  .session-permissions-card__choice[aria-checked='true']
    .session-permissions-card__radio {
    border: 4px solid var(--accent);
  }
  .session-permissions-card__choice-text {
    display: grid;
    min-width: 0;
  }
  .session-permissions-card__choice-hint {
    color: var(--text-lo);
  }
  .session-permissions-card__current {
    margin: 0;
    color: var(--text-hi);
  }
  .session-permissions-card__inherited {
    flex-shrink: 0;
    margin-top: 8px;
    padding-top: 7px;
    border-top: 1px solid var(--border-2);
    color: var(--amber);
  }
  .session-permissions-card__inherited p {
    margin: 0;
  }
  .session-permissions-card__details {
    display: flex;
    flex: 0 1 auto;
    flex-direction: column;
    min-height: 0;
    margin-top: 8px;
    padding-top: 7px;
    border-top: 1px solid var(--border-2);
  }
  .session-permissions-card__details summary {
    color: var(--text-lo);
    cursor: pointer;
  }
  .session-permissions-card__details summary:hover {
    color: var(--text-hi);
  }
  .session-permissions-card__body {
    min-height: 0;
    margin-top: 6px;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .session-permissions-card__note {
    margin: 6px 0 0;
    color: var(--text-lo);
  }
  .session-permissions-card__label {
    color: var(--text-lo);
  }
  .session-permissions-card__grants {
    margin-top: 8px;
  }
  .session-permissions-card__grants ul {
    display: grid;
    gap: 2px;
    margin: 4px 0 0;
    padding: 0;
    list-style: none;
  }
  .session-permissions-card__grants li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    overflow-wrap: anywhere;
  }
  .session-permissions-card__error {
    margin: 4px 0 0;
    color: var(--red);
  }
</style>
