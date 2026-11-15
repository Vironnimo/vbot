import { mount } from 'svelte';
import '@vbot/extension-sdk/styles';
import { registerCatalog } from '@vbot/extension-sdk/lib/i18n.js';
import factoryCatalog from './i18n.js';
import FactoryPage from './FactoryPage.svelte';

registerCatalog(factoryCatalog);
mount(FactoryPage, { target: document.getElementById('app') });
