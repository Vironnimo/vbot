# Archived Google Antigravity subscription Provider

The retired `antigravity:subscription` Connection used the local headless CLI as the Model backend for a vBot Agent. After the transport began resuming native conversations, a live Hydrogen Run completed 29 file reads across six Model steps without a persisted Assistant reply. The user requested removal of this implementation. Historical Session records still identify their original route.

`archive/antigravity-provider.zip` contains the original Provider, Integration files, tests and documentation at commit `b9b5e0735`. Restore in a separate worktree and reconcile shared files individually. The Google AI Studio Provider has its own API credentials and billing.

Google documents ACP for clients hosting its interactive Agent. It excludes embedding the headless CLI as a backend for third-party coding agents on personal plans. See [Google's ACP client contract](https://antigravity.google/docs/ide/extensions/acp/) and [Google's FAQ](https://antigravity.google/docs/faq).
