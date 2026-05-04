# Phase 2+ Goals

This file now tracks only unresolved cross-phase contract questions and future
product goals that are not implemented yet. Implemented behavior belongs in the
specs and project docs.

## Open Contract Questions

- **Fallback behavior**: exact automatic behavior for `fallback_model`
- **Provider-specific `reasoning_meta` resend after completed turns**

## Future Product Goals

### Accessor-local UI Restoration

- Accessors may remember the last selected agent locally and restore it on the
  next start/reload.
- This last-selected-agent memory is not part of the shared server/domain data
  model and should not be stored in the shared instance data directory.
- For WebUI and Desktop, this preference is low priority and may be implemented
  later in accessor-local storage.
