<script>
  import { untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import { validatePermissionRules } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    PERMISSION_LEVEL_AGENT,
    PERMISSION_LEVEL_DEFAULT,
    PERMISSION_LEVEL_OVERRIDE,
    PERMISSION_PRESET_CUSTOM,
    PERMISSION_PRESET_FULL_ACCESS,
    PERMISSION_PRESET_INHERIT,
    PERMISSION_RULE_EFFECTS,
    PERMISSION_RULE_KINDS,
    applyPermissionPreset,
    joinPermissionRule,
    normalizePermissionRules,
    parsePastedPermissionRules,
    permissionPresetChoice,
    permissionProblems,
    permissionRulesComplete,
    splitPermissionRule,
    withPermissionBase,
    withPermissionRuleList,
    withPastedPermissionRules,
  } from '$lib/permissionRules.js';

  const noop = () => {};
  const INHERIT = 'inherit';
  const VALIDATION_DELAY_MS = 250;
  const PRESET_LABELS = {
    full_access: () => t('permissions.preset.full_access'),
    work_dir: () => t('permissions.preset.work_dir'),
    work_dir_read_only: () => t('permissions.preset.work_dir_read_only'),
    custom: () => t('permissions.preset.custom'),
  };
  const PART_LABELS = {
    read: () => t('permissions.part.read'),
    write: () => t('permissions.part.write'),
    commands: () => t('permissions.part.commands'),
    web: () => t('permissions.part.web'),
  };
  const BASE_LABELS = {
    read: {
      anywhere: () => t('permissions.base.read.anywhere'),
      work_area: () => t('permissions.base.read.work_area'),
    },
    write: {
      anywhere: () => t('permissions.base.write.anywhere'),
      work_area: () => t('permissions.base.write.work_area'),
      scratch: () => t('permissions.base.write.scratch'),
    },
    commands: {
      any: () => t('permissions.base.commands.any'),
      listed: () => t('permissions.base.commands.listed'),
    },
    web: {
      any: () => t('permissions.base.web.any'),
      listed: () => t('permissions.base.web.listed'),
    },
  };
  const LIST_LABELS = {
    allow: {
      title: () => t('permissions.allowTitle'),
      description: () => t('permissions.allowDescription'),
    },
    deny: {
      title: () => t('permissions.denyTitle'),
      description: () => t('permissions.denyDescription'),
    },
  };

  // One level's own Permission Rules (`.vorch/domain-maps/permissions.md`):
  // `level` is an Agent's (`agent`), a Team member's Project override
  // (`override`) or the shared default (`default`). `value` is the own rule
  // set (null: none, so the level inherits); `inherited` is what applies to
  // each part the level does not set, until the server's check answers with
  // it. The server checks every edit (`permissions.validate`) and previews
  // what new Sessions can reach; `agentId` names the Agent the preview is for,
  // `newAgent` an Agent being created (it inherits the shared default).
  // `compact` shows only the preset choice, for the creation dialog.
  let {
    value = null,
    inherited = null,
    level = PERMISSION_LEVEL_AGENT,
    agentId = '',
    newAgent = false,
    compact = false,
    disabled = false,
    idPrefix = 'permission-rules',
    onChange = noop,
  } = $props();

  let result = $state(null);
  let checkedKey = $state('');
  let preview = $state(null);
  let checking = $state(false);
  let checkError = $state('');
  let pasteOpen = $state(false);
  let pasteText = $state('');
  // Set when choosing a read base narrowed the own write base with it.
  let writeNarrowed = $state(false);
  let sequence = 0;

  let own = $derived(normalizePermissionRules(value));
  let requestKey = $derived(JSON.stringify([agentId, newAgent, own]));
  let parent = $derived(
    result
      ? normalizePermissionRules(result.inherited)
      : normalizePermissionRules(inherited),
  );
  let presets = $derived(result?.presets ?? {});
  // What applies to each base and which bases the level cannot choose; the
  // base rows wait for the server's first answer.
  let bases = $derived(result?.bases ?? {});
  let current = $derived(checkedKey === requestKey ? result : null);
  let errors = $derived(current?.errors ?? []);
  let complete = $derived(permissionRulesComplete(own));
  let presetValue = $derived(
    permissionPresetChoice(own, {
      level,
      preset: result?.preset,
      presets,
    }),
  );
  let presetOptions = $derived(
    buildPresetOptions(presets, parent, presetValue),
  );
  // Full access chosen over inherited limits: the level ignores them.
  let overridesInherited = $derived(
    level !== PERMISSION_LEVEL_DEFAULT &&
      presetValue === PERMISSION_PRESET_FULL_ACCESS &&
      parent !== null,
  );
  // Nothing limits the Agent: no rules apply, or the resulting ones restrict
  // nothing (the server then has no preview).
  let fullAccess = $derived(
    (own === null && parent === null) ||
      (current?.valid === true && !current?.effective),
  );

  // Every change of the rules (or the Agent) is checked once; the first
  // check runs at once, later ones after typing pauses.
  $effect(() => {
    const key = requestKey;
    const delay = untrack(() => result) ? VALIDATION_DELAY_MS : 0;
    const timer = setTimeout(() => void check(key), delay);
    return () => clearTimeout(timer);
  });

  async function check(key) {
    const [id, creating, rules] = JSON.parse(key);
    const ticket = ++sequence;
    checking = true;
    try {
      const response = await validatePermissionRules(rules, {
        agentId: id,
        newAgent: creating,
      });
      if (ticket !== sequence) return;
      result = response;
      checkedKey = key;
      checkError = '';
      if (response?.valid) preview = response.preview ?? null;
    } catch (error) {
      if (ticket !== sequence) return;
      checkError = error?.message || String(error);
    } finally {
      if (ticket === sequence) checking = false;
    }
  }

  function presetLabel(name) {
    return (PRESET_LABELS[name] ?? PRESET_LABELS.custom)();
  }

  // "Use the default (Work in the working directory)": what no own rules
  // means at this level.
  function inheritOptionLabel(parentRules) {
    const name =
      parentRules === null
        ? PERMISSION_PRESET_FULL_ACCESS
        : (result?.inherited_preset ?? PERMISSION_PRESET_CUSTOM);
    const preset =
      name === PERMISSION_PRESET_CUSTOM
        ? t('permissions.preset.customRules')
        : presetLabel(name);
    return level === PERMISSION_LEVEL_OVERRIDE
      ? t('permissions.preset.useInherited', { preset })
      : t('permissions.preset.useDefault', { preset });
  }

  function buildPresetOptions(definitions, parentRules, selected) {
    const options =
      level === PERMISSION_LEVEL_DEFAULT
        ? []
        : [
            {
              value: PERMISSION_PRESET_INHERIT,
              label: inheritOptionLabel(parentRules),
            },
          ];
    for (const name of Object.keys(definitions)) {
      options.push({ value: name, label: presetLabel(name) });
    }
    if (selected === PERMISSION_PRESET_CUSTOM) {
      options.push({
        value: PERMISSION_PRESET_CUSTOM,
        label: presetLabel(PERMISSION_PRESET_CUSTOM),
        disabled: true,
      });
    }
    return options;
  }

  function choosePreset(name) {
    // The shared default inherits nothing: Full access there is no default.
    if (
      name === PERMISSION_PRESET_INHERIT ||
      (level === PERMISSION_LEVEL_DEFAULT &&
        name === PERMISSION_PRESET_FULL_ACCESS)
    ) {
      onChange(null);
    } else if (Object.hasOwn(presets, name)) {
      onChange(applyPermissionPreset(own, presets[name]));
    }
  }

  function baseLabel(part, base) {
    return BASE_LABELS[part]?.[base]?.() ?? base;
  }

  // `base` is the server's `bases[part]`: what applies without an own value
  // (with its source), the choices, and those the level cannot choose.
  function baseOptions(part, base) {
    const inherit = base.inherit ?? {};
    const value = baseLabel(part, inherit.value);
    const excluded = base.excluded ?? [];
    return [
      {
        value: INHERIT,
        label:
          inherit.source === 'default'
            ? t('permissions.base.default', { value })
            : t('permissions.base.inherited', { value }),
      },
      ...(base.choices ?? []).map((choice) => {
        const narrowed = excluded.includes(choice);
        return {
          value: choice,
          label: baseLabel(part, choice),
          disabled: narrowed,
          tooltip: narrowed ? t('permissions.base.writeWithinRead') : '',
        };
      }),
    ];
  }

  function setBase(part, choice) {
    if (choice === INHERIT && own === null) return;
    const next = withPermissionBase(
      own,
      part,
      choice === INHERIT ? null : choice,
      bases,
    );
    if (part === 'read' || part === 'write') {
      writeNarrowed =
        part === 'read' &&
        own?.write === 'anywhere' &&
        next.write !== 'anywhere';
    }
    onChange(next);
  }

  function shownRules(effect) {
    return own?.[effect] ?? parent?.[effect] ?? [];
  }

  function ownsList(effect) {
    return Array.isArray(own?.[effect]);
  }

  function setRules(effect, list) {
    onChange(withPermissionRuleList(own, effect, list));
  }

  function setRule(effect, index, kind, pattern) {
    const list = [...shownRules(effect)];
    list[index] = joinPermissionRule(kind, pattern);
    setRules(effect, list);
  }

  function removeRule(effect, index) {
    setRules(
      effect,
      shownRules(effect).filter((_, position) => position !== index),
    );
  }

  function addRule(effect) {
    setRules(effect, [...shownRules(effect), joinPermissionRule('Read', '')]);
  }

  function kindOptions(kind) {
    const kinds = PERMISSION_RULE_KINDS.includes(kind)
      ? PERMISSION_RULE_KINDS
      : [...PERMISSION_RULE_KINDS, kind];
    return kinds.map((name) => ({ value: name, label: name }));
  }

  function rowProblems(effect, index, pattern) {
    // A rule being typed has no pattern yet; its problem waits until it does.
    if (!pattern.trim()) return [];
    return permissionProblems(errors, effect, index);
  }

  function addPasted(effect) {
    const pasted = parsePastedPermissionRules(pasteText, effect);
    onChange(withPastedPermissionRules(own, parent, pasted));
    pasteText = '';
    pasteOpen = false;
  }

  function previewText(values, anything) {
    if (values === null || values === undefined) return anything;
    return values.length > 0
      ? values.join(', ')
      : t('permissions.preview.nothing');
  }
</script>

{#snippet overrideNote()}
  {#if overridesInherited}
    <div class="s-row-desc" role="status">
      {level === PERMISSION_LEVEL_OVERRIDE
        ? t('permissions.preset.ignoresInherited')
        : t('permissions.preset.ignoresDefault')}
    </div>
  {/if}
{/snippet}

{#if compact}
  <div class="permission-rules permission-rules--compact" data-permission-rules>
    <Dropdown
      id={`${idPrefix}-preset`}
      value={presetValue}
      options={presetOptions}
      {disabled}
      ariaLabel={t('permissions.title')}
      onValueChange={choosePreset}
    />
    {@render overrideNote()}
    {#if checkError}
      <p class="s-row-error">
        {t('permissions.checkFailed', { message: checkError })}
      </p>
    {/if}
  </div>
{:else}
  <div class="permission-rules" data-permission-rules>
    <p class="s-row-desc permission-rules__limits">
      {t('permissions.limits')}
      <InfoHint text={t('permissions.limitsHelp')} />
    </p>
    <div class="s-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('permissions.preset')}
            <InfoHint text={t('permissions.presetHelp')} />
          </div>
          {@render overrideNote()}
        </div>
        <div class="s-row-control">
          <Dropdown
            id={`${idPrefix}-preset`}
            value={presetValue}
            options={presetOptions}
            {disabled}
            ariaLabel={t('permissions.preset')}
            onValueChange={choosePreset}
          />
        </div>
      </div>
      {#each Object.entries(bases) as [part, base] (part)}
        {@const problems = permissionProblems(errors, part)}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">
              {PART_LABELS[part]()}
              {#if part === 'read' || part === 'write'}
                <InfoHint text={t('permissions.base.workAreaHelp')} />
              {/if}
            </div>
            {#if part === 'write' && writeNarrowed}
              <div class="s-row-desc" role="status">
                {t('permissions.base.writeNarrowed')}
              </div>
            {/if}
            {#if problems.length > 0}
              <p class="s-row-error" id={`${idPrefix}-${part}-error`}>
                {problems.join(' ')}
              </p>
            {/if}
          </div>
          <div class="s-row-control">
            <Dropdown
              id={`${idPrefix}-${part}`}
              value={own?.[part] ?? INHERIT}
              options={baseOptions(part, base)}
              {disabled}
              invalid={problems.length > 0}
              ariaLabel={PART_LABELS[part]()}
              ariaDescribedby={problems.length > 0
                ? `${idPrefix}-${part}-error`
                : undefined}
              onValueChange={(choice) => setBase(part, choice)}
            />
          </div>
        </div>
      {/each}
    </div>

    {#each PERMISSION_RULE_EFFECTS as effect (effect)}
      {@const rules = shownRules(effect)}
      {@const editable = ownsList(effect)}
      <div class="s-subhead permission-rules__subhead">
        <div>
          <h4 class="s-subhead__title">
            {LIST_LABELS[effect].title()}
            <InfoHint
              text={t('permissions.rulesHelp', {
                workdir: '{workdir}',
                project: '{project}',
                scratch: '{scratch}',
                workspace: '{workspace}',
              })}
            />
          </h4>
          <p class="s-row-desc">
            {editable || rules.length === 0
              ? LIST_LABELS[effect].description()
              : t('permissions.rulesInherited')}
          </p>
        </div>
        {#if !editable && rules.length > 0}
          <Button
            variant="tertiary"
            {disabled}
            tooltip={t('permissions.rulesChangeHint')}
            onClick={() => setRules(effect, rules)}
            >{t('permissions.rulesChange')}</Button
          >
        {:else if editable && (parent?.[effect]?.length ?? 0) > 0}
          <Button
            variant="tertiary"
            {disabled}
            tooltip={t('permissions.rulesResetHint')}
            onClick={() => setRules(effect, null)}
            >{t('permissions.rulesReset')}</Button
          >
        {/if}
      </div>
      <ul class="permission-rules__list" data-permission-list={effect}>
        {#each rules as rule, index (`${effect}-${index}`)}
          {@const parts = splitPermissionRule(rule)}
          {@const problems = editable
            ? rowProblems(effect, index, parts.pattern)
            : []}
          {@const errorId = `${idPrefix}-${effect}-${index}-error`}
          <li class="permission-rules__rule">
            {#if editable}
              <div class="permission-rules__row">
                <Dropdown
                  id={`${idPrefix}-${effect}-${index}-kind`}
                  value={parts.kind}
                  options={kindOptions(parts.kind)}
                  {disabled}
                  triggerClass="permission-rules__kind"
                  ariaLabel={t('permissions.ruleKind', { index: index + 1 })}
                  onValueChange={(kind) =>
                    setRule(effect, index, kind, parts.pattern)}
                />
                <TextField
                  class="permission-rules__pattern"
                  code
                  value={parts.pattern}
                  {disabled}
                  invalid={problems.length > 0}
                  placeholder={t('permissions.rulePatternPlaceholder')}
                  ariaLabel={t('permissions.rulePattern', { index: index + 1 })}
                  aria-describedby={problems.length > 0 ? errorId : undefined}
                  onInput={(pattern) =>
                    setRule(effect, index, parts.kind, pattern)}
                />
                <Button
                  variant="tertiary"
                  icon
                  {disabled}
                  ariaLabel={t('permissions.ruleRemove', { rule })}
                  onClick={() => removeRule(effect, index)}
                  ><svg
                    width="16"
                    height="16"
                    viewBox="0 0 16 16"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.5"
                    stroke-linecap="round"
                    aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg
                  ></Button
                >
              </div>
              {#if problems.length > 0}
                <p class="s-row-error" id={errorId}>{problems.join(' ')}</p>
              {/if}
            {:else}
              <code class="permission-rules__inherited">{rule}</code>
            {/if}
          </li>
        {:else}
          <li class="s-row-desc">{t('permissions.rulesNone')}</li>
        {/each}
      </ul>
      <div class="permission-rules__actions">
        <Button variant="secondary" {disabled} onClick={() => addRule(effect)}
          >{t('permissions.ruleAdd')}</Button
        >
      </div>
    {/each}

    <div class="permission-rules__paste">
      {#if pasteOpen}
        <TextArea
          code
          rows={4}
          value={pasteText}
          {disabled}
          ariaLabel={t('permissions.pasteLabel')}
          onInput={(text) => (pasteText = text)}
        />
        <p class="s-row-desc">{t('permissions.pasteHelp')}</p>
        <div class="permission-rules__actions">
          <Button
            variant="secondary"
            disabled={disabled || !pasteText.trim()}
            onClick={() => addPasted('allow')}
            >{t('permissions.pasteAllow')}</Button
          >
          <Button
            variant="secondary"
            disabled={disabled || !pasteText.trim()}
            onClick={() => addPasted('deny')}
            >{t('permissions.pasteDeny')}</Button
          >
          <Button variant="tertiary" onClick={() => (pasteOpen = false)}
            >{t('permissions.pasteCancel')}</Button
          >
        </div>
      {:else}
        <Button variant="tertiary" {disabled} onClick={() => (pasteOpen = true)}
          >{t('permissions.paste')}</Button
        >
      {/if}
    </div>

    <div class="permission-rules__status" role="status">
      {#if !complete}
        <p class="s-row-desc">{t('permissions.incomplete')}</p>
      {/if}
      {#if checkError}
        <p class="s-row-error">
          {t('permissions.checkFailed', { message: checkError })}
        </p>
      {/if}
      {#each current?.notes ?? [] as note, index (index)}
        <p class="s-row-desc">{note}</p>
      {/each}
    </div>

    <div class="permission-rules__preview" aria-busy={checking}>
      <h4 class="s-subhead__title">{t('permissions.preview')}</h4>
      {#if fullAccess}
        <p class="s-row-desc">{t('permissions.preview.fullAccess')}</p>
      {:else if preview}
        <dl class="permission-rules__facts">
          <dt>{t('permissions.preview.read')}</dt>
          <dd>
            {previewText(preview.read, t('permissions.preview.anywhere'))}
          </dd>
          <dt>{t('permissions.preview.write')}</dt>
          <dd>
            {previewText(preview.write, t('permissions.preview.anywhere'))}
          </dd>
          <dt>{t('permissions.preview.commands')}</dt>
          <dd>
            {previewText(preview.commands, t('permissions.preview.anyCommand'))}
          </dd>
          <dt>{t('permissions.preview.web')}</dt>
          <dd>{previewText(preview.web, t('permissions.preview.anySite'))}</dd>
          {#if (preview.deny ?? []).length > 0}
            <dt>{t('permissions.preview.deny')}</dt>
            <dd>{preview.deny.join(', ')}</dd>
          {/if}
          {#if (preview.inactive ?? []).length > 0}
            <dt>
              {t('permissions.preview.inactive')}
              <InfoHint text={t('permissions.preview.inactiveHint')} />
            </dt>
            <dd>{preview.inactive.join(', ')}</dd>
          {/if}
        </dl>
        <p class="s-row-desc">
          {t('permissions.preview.scratch', { scratch: '{scratch}' })}
        </p>
      {:else if checking}
        <p class="s-row-desc">{t('permissions.checking')}</p>
      {/if}
    </div>
  </div>
{/if}

<style>
  .permission-rules {
    display: grid;
    gap: 10px;
    min-width: 0;
  }
  .permission-rules--compact {
    gap: 4px;
  }
  .permission-rules__limits {
    margin: 0;
  }
  .permission-rules__subhead {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }
  .permission-rules__list {
    display: grid;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .permission-rules__row {
    display: grid;
    grid-template-columns: minmax(6.5rem, auto) minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
  }
  .permission-rules__inherited {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }
  .permission-rules__actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .permission-rules__paste {
    display: grid;
    gap: 6px;
  }
  .permission-rules__status:empty {
    display: none;
  }
  .permission-rules__status p {
    margin: 0;
  }
  .permission-rules__preview {
    display: grid;
    gap: 6px;
    padding-top: 6px;
    border-top: 1px solid var(--border);
  }
  .permission-rules__facts {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 4px 12px;
    margin: 0;
    font-size: var(--fs-label-sm);
  }
  .permission-rules__facts dt {
    color: var(--text-lo);
  }
  .permission-rules__facts dd {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }
</style>
