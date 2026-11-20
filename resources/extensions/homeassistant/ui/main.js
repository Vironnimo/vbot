import { mount } from 'svelte';
import '@vbot/extension-sdk/styles';
import { registerCatalog } from '@vbot/extension-sdk/lib/i18n.js';
import catalog from './i18n.js';
import WakewordCommands from './WakewordCommands.svelte';

registerCatalog(catalog);
mount(WakewordCommands, { target: document.getElementById('app') });
