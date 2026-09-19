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
      keyError = t(
        'jev.duplicateId',
        'This ID is already in use. Choose a unique ID.',
      );
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
  {t(
    'jev.controlHelp',
    'Read the application state, let Jev select an action, then execute its assigned command. Commands run on the vBot host. Switching tabs or closing this page does not stop the control.',
  )}
</p>
<div class="jev-field">
  <label for={`${id}-goal`}>{t('jev.goal', 'What should Jev achieve?')}</label
  ><TextArea
    id={`${id}-goal`}
    value={control.instructions}
    rows={3}
    onInput={(value) => patch({ instructions: value })}
  />
</div>
<h3>{t('jev.observe', 'Read state')}</h3>
<p class="jev-help">
  {t(
    'jev.observeHelp',
    'Write UTF-8 JSON to stdout: {"state": your text or JSON, "done": false}. Return done: true when the application is finished. Each call must exit. Send diagnostic messages to stderr.',
  )}
</p>
<CommandEditor
  command={control.observe}
  label={t('jev.observerCommand', 'State-reading executable')}
  onChange={(command) => patch({ observe: command })}
/>
<h3>{t('jev.actions', 'Actions')}</h3>
<p class="jev-help">
  {t(
    'jev.actionsHelp',
    'Jev sees the action descriptions and chooses an ID. It cannot create or change commands. Arguments are literal; shell syntax requires an explicitly chosen shell executable.',
  )}
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
          })}>{t('jev.remove', 'Remove')}</Button
      >
    </div>
    <TextField
      ariaLabel={t('jev.actionId', 'Action ID')}
      value={key}
      onInput={(value, event) => changeAction(index, value, action, event)}
    />
    <div class="jev-field">
      <label for={`${id}-description-${index}`}
        >{t(
          'jev.actionDescription',
          'When should Jev choose this action?',
        )}</label
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
        label={t('jev.actionCommand', 'Action executable')}
        onChange={(command) => changeAction(index, key, { ...action, command })}
      />
      <Button
        variant="tertiary"
        onClick={() => changeAction(index, key, { ...action, command: null })}
        >{t('jev.makeNoop', 'Make this a no-op action')}</Button
      >
    {:else}
      <p class="jev-help">
        {t(
          'jev.noopHelp',
          'This action leaves the application unchanged, then reads a fresh state.',
        )}
      </p>
      <Button
        variant="tertiary"
        onClick={() =>
          changeAction(index, key, {
            ...action,
            command: { argv: [''], cwd: control.observe.cwd },
          })}>{t('jev.assignCommand', 'Assign a command')}</Button
      >
    {/if}
  </article>
{/each}
<Button onClick={addAction}>{t('jev.addAction', 'Add action')}</Button>
<h3>{t('jev.timing', 'Execution')}</h3>
{#each [{ key: 'interval_ms', label: t('jev.interval', 'Delay after each action (ms)') }, { key: 'max_steps', label: t('jev.maxSteps', 'Maximum steps (0 = until stopped)') }, { key: 'timeout_seconds', label: t('jev.timeout', 'Command timeout (seconds)') }] as field (field.key)}
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
  {t(
    'jev.executionHelp',
    'Steps run in sequence. Command errors, invalid state or Provider failures stop execution without retrying actions. Actual speed depends on observation, Jev and the action. Changes apply to the next start.',
  )}
</p>
