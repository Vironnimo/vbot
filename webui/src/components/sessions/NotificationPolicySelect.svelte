<script>
  import Dropdown from '../Dropdown.svelte';
  import { t } from '$lib/i18n.js';
  let { value = 'all', onChange = () => {} } = $props();
  const options = $derived([
    { value: 'all', label: t('notifications.all') },
    { value: 'errors', label: t('notifications.errors') },
    { value: 'muted', label: t('notifications.muted') },
  ]);
</script>

<Dropdown
  {value}
  {options}
  ariaLabel={t('notifications.label')}
  onValueChange={onChange}
/>
