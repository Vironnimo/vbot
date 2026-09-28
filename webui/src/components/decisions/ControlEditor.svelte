<script>
  import Button from '../ui/Button.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import CommandEditor from './CommandEditor.svelte';
  import { t } from '$lib/i18n.js';
  let { control, onChange } = $props();
  const id = $props.id();
  let keyError = $state('');
  const patch = (values) => onChange({ ...control, ...values });
  function changeAction(index, key, value, event) {
    const entries = Object.entries(control.actions);
    entries[index] = [key, value];
    if (new Set(entries.map(([name]) => name)).size !== entries.length) {
      keyError = t('jev.duplicateId');
      if (event) event.target.value = Object.keys(control.actions)[index];
      return;
    }
    keyError = '';
    patch({ actions: Object.fromEntries(entries) });
  }
  function addAction() {
    let n = Object.keys(control.actions).length + 1;
    while (Object.hasOwn(control.actions, `action_${n}`)) n++;
    patch({
      actions: {
        ...control.actions,
        [`action_${n}`]: {
          description: '',
          command: { argv: [''], cwd: control.observe.cwd },
        },
      },
    });
  }
</script>

{#if keyError}<p class="jev-error" role="alert">{keyError}</p>{/if}

<p class="jev-help">
  {t('jev.controlHelp')}
</p>
<div class="jev-field">
  <label for={`${id}-goal`}>{t('jev.goal')}</label><TextArea
    id={`${id}-goal`}
    value={control.instructions}
    rows={3}
    onInput={(value) => patch({ instructions: value })}
  />
</div>
<h3>{t('jev.observe')}</h3>
<p class="jev-help">
  {t('jev.observeHelp')}
</p>
<CommandEditor
  command={control.observe}
  label={t('jev.observerCommand')}
  onChange={(command) => patch({ observe: command })}
/>
<h3>{t('jev.actions')}</h3>
<p class="jev-help">
  {t('jev.actionsHelp')}
</p>
{#each Object.entries(control.actions) as [key, action], index (index)}
  <article class="jev-question">
    <div class="jev-row">
      <strong>{key}</strong><Button
        variant="tertiary"
        onClick={() =>
          patch({
            actions: Object.fromEntries(
              Object.entries(control.actions).filter((_, i) => i !== index),
            ),
          })}>{t('jev.remove')}</Button
      >
    </div>
    <TextField
      ariaLabel={t('jev.actionId')}
      value={key}
      onInput={(value, event) => changeAction(index, value, action, event)}
    />
    <div class="jev-field">
      <label for={`${id}-description-${index}`}
        >{t('jev.actionDescription')}</label
      ><TextArea
        id={`${id}-description-${index}`}
        rows={2}
        value={action.description}
        onInput={(value) =>
          changeAction(index, key, { ...action, description: value })}
      />
    </div>
    {#if action.command}
      <CommandEditor
        command={action.command}
        label={t('jev.actionCommand')}
        onChange={(command) => changeAction(index, key, { ...action, command })}
      />
      <Button
        variant="tertiary"
        onClick={() => changeAction(index, key, { ...action, command: null })}
        >{t('jev.makeNoop')}</Button
      >
    {:else}
      <p class="jev-help">
        {t('jev.noopHelp')}
      </p>
      <Button
        variant="tertiary"
        onClick={() =>
          changeAction(index, key, {
            ...action,
            command: { argv: [''], cwd: control.observe.cwd },
          })}>{t('jev.assignCommand')}</Button
      >
    {/if}
  </article>
{/each}
<Button onClick={addAction}>{t('jev.addAction')}</Button>
<h3>{t('jev.timing')}</h3>
{#each [{ key: 'interval_ms', label: t('jev.interval') }, { key: 'max_steps', label: t('jev.maxSteps') }, { key: 'timeout_seconds', label: t('jev.timeout') }] as field (field.key)}
  <div class="jev-field">
    <label for={`${id}-${field.key}`}>{field.label}</label><TextField
      id={`${id}-${field.key}`}
      type="number"
      value={control[field.key]}
      onInput={(value) =>
        patch({ [field.key]: value === '' ? '' : Number(value) })}
    />
  </div>
{/each}
<p class="jev-help">
  {t('jev.executionHelp')}
</p>
