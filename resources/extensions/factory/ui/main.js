import { mount } from 'svelte';
import '../../../../webui/src/styles/app.css';
import { registerCatalog } from '../../../../webui/src/lib/i18n.js';
import factoryCatalog from './i18n.js';
import FactoryPage from './FactoryPage.svelte';

registerCatalog(factoryCatalog);
mount(FactoryPage, { target: document.getElementById('app') });
