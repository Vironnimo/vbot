import globals from 'globals';
import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';

export default [
  js.configs.recommended,
  ...svelte.configs.recommended,
  {
    files: [
      'src/**/*.svelte',
      'src/**/*.js',
      'scripts/**/*.{js,mjs}',
      'resources/extensions/*/ui/**/*.{js,svelte}',
      'tests/fixtures/extension-pages/*/ui/**/*.js',
      'webui/src/**/*.svelte',
      'webui/src/**/*.js',
      'webui/scripts/**/*.{js,mjs}',
    ],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
];
