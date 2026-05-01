export default {
  plugins: ['prettier-plugin-svelte'],
  overrides: [
    {
      files: '*.svelte',
      options: {
        parser: 'svelte',
      },
    },
  ],
  singleQuote: true,
  semi: true,
  tabWidth: 2,
};
