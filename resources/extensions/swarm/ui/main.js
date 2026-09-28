import { mount } from 'svelte';
import '../../../../webui/src/styles/app.css';
import { registerCatalog } from '../../../../webui/src/lib/i18n.js';
import swarmCatalog from './i18n.js';
import SwarmPage from './SwarmPage.svelte';

registerCatalog(swarmCatalog);
mount(SwarmPage, { target: document.getElementById('app') });
