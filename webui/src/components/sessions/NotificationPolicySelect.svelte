<script module>
  import { t } from '$lib/i18n.js';

  const LABELS = {
    all: () => t('notifications.all'),
    errors: () => t('notifications.errors'),
    muted: () => t('notifications.muted'),
  };

  export function policyLabel(value) {
    return LABELS[value]?.() ?? value;
  }
</script>

<script>
  import Dropdown from '../Dropdown.svelte';
  let { value = 'all', onChange = () => {} } = $props();
  const options = $derived(
    Object.keys(LABELS).map((policy) => ({
      value: policy,
      label: policyLabel(policy),
    })),
  );
</script>

<Dropdown
  {value}
  {options}
  ariaLabel={t('notifications.label')}
  onValueChange={onChange}
/>
