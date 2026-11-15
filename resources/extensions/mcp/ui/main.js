import { mount } from 'svelte';
import '@vbot/extension-sdk/styles';
import { registerCatalog } from '@vbot/extension-sdk/lib/i18n.js';
import catalog from './i18n.js';
import McpPage from './McpPage.svelte';

registerCatalog(catalog);
mount(McpPage, { target: document.getElementById('app') });
