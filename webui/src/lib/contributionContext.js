import { getContext, setContext } from 'svelte';
const CONTEXT = Symbol('ui-contributions');
export const provideContributions = (value) => setContext(CONTEXT, value);
export const useContributions = () => getContext(CONTEXT) ?? null;
