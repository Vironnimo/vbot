<script>
  import TextField from '../ui/TextField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import { t } from '$lib/i18n.js';
  let { command, onChange, label } = $props();
  const id = $props.id();
</script>

<div class="jev-field">
  <label for={`${id}-exe`}>{label}</label><TextField
    id={`${id}-exe`}
    value={command.argv[0]}
    placeholder={t('jev.executablePlaceholder', 'Executable, e.g. python')}
    onInput={(value) =>
      onChange({ ...command, argv: [value, ...command.argv.slice(1)] })}
  />
</div>
<div class="jev-field">
  <label for={`${id}-args`}
    >{t('jev.arguments', 'Arguments · one per line')}</label
  ><TextArea
    id={`${id}-args`}
    rows={3}
    code
    value={command.argv.slice(1).join('\n')}
    onInput={(value) =>
      onChange({
        ...command,
        argv:
          value === ''
            ? [command.argv[0]]
            : [command.argv[0], ...value.split('\n')],
      })}
  />
</div>
<div class="jev-field">
  <label for={`${id}-cwd`}
    >{t('jev.cwd', 'Working directory on the vBot host')}</label
  ><TextField
    id={`${id}-cwd`}
    value={command.cwd}
    onInput={(value) => onChange({ ...command, cwd: value })}
  />
</div>
