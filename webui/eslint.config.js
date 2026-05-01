import globals from 'globals';
import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';

export default [
  js.configs.recommended,
  ...svelte.configs.recommended,
  {
    files: ['**/*.svelte', '**/*.js'],
    languageOptions: {
      globals: {
        ...globals.browser,
      },
    },
  },
];
