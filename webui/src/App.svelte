<script>
  import AppShell from './components/AppShell.svelte';
  import PlaceholderView from './components/PlaceholderView.svelte';
  import './styles/app.css';

  const navigationItems = [
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
      descriptionKey: 'placeholders.chat.description',
      descriptionFallback:
        'Agent chat will appear here once the chat view is wired.',
    },
    {
      id: 'agents',
      labelKey: 'navigation.agents',
      labelFallback: 'Agents',
      descriptionKey: 'placeholders.agents.description',
      descriptionFallback:
        'Agent creation, editing, and deletion controls are coming next.',
    },
    {
      id: 'system-prompt',
      labelKey: 'navigation.systemPrompt',
      labelFallback: 'System Prompt',
      descriptionKey: 'placeholders.systemPrompt.description',
      descriptionFallback:
        'Editable prompt pieces will be managed from this space later.',
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
      descriptionKey: 'placeholders.settings.description',
      descriptionFallback:
        'Runtime and WebUI settings placeholders live here for now.',
    },
  ];

  let activeViewId = $state(navigationItems[0].id);

  const selectView = (viewId) => {
    activeViewId = viewId;
  };

  let activeView = $derived(
    navigationItems.find((item) => item.id === activeViewId) ??
      navigationItems[0],
  );
</script>

<AppShell items={navigationItems} {activeViewId} onSelectView={selectView}>
  <PlaceholderView view={activeView} />
</AppShell>
