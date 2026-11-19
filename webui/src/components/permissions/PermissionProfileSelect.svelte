<script>
  import InfoHint from '../ui/InfoHint.svelte';
  import Dropdown from '../Dropdown.svelte';
  import { t } from '$lib/i18n.js';
  import { permissionProfileLabel } from '$lib/permissionRules.js';

  let { id, label, names = [], value = '', emptyLabel, onChange } = $props();
  const options = $derived([
    { value: '', label: emptyLabel },
    ...names.map((name) => ({
      value: name,
      label: permissionProfileLabel(name),
    })),
    ...(value && !names.includes(value)
      ? [
          {
            value,
            label: t('permissions.profile.missing', { name: value }),
            disabled: true,
          },
        ]
      : []),
  ]);
</script>

<Dropdown
  {id}
  ariaLabel={label}
  value={value ?? ''}
  {options}
  onValueChange={onChange}
/>
{#if value && value !== 'full_access'}
  <InfoHint
    ariaLabel={t('permissions.limits')}
    text={t('permissions.limitsHelp')}
  />
{/if}
