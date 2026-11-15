import { mount } from 'svelte';
import '@vbot/extension-sdk/styles';
import { registerCatalog } from '@vbot/extension-sdk/lib/i18n.js';
import swarmCatalog from './i18n.js';
import SwarmPage from './SwarmPage.svelte';

registerCatalog(swarmCatalog);
mount(SwarmPage, { target: document.getElementById('app') });
