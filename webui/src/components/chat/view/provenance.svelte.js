import { formatAgentAddress } from '$lib/agentAddress.js';
import {
  isReflectionSession,
  sessionDisplayName,
  sessionParentReference,
} from '$lib/sessionListView.js';
import { takeSessionInvalidations } from '$lib/sessionInvalidation.js';
import { untrack } from 'svelte';

// Resolve the displayed Session's immediate parent and invalidate only its lineage.
export function createSessionProvenance(context) {
  // Every derived Session resolves its immediate parent from the authoritative
  // Session-list projection: Sub-Agent Sessions use `subagent_parent`, while
  // Fork and Reflection Sessions use `fork_source`. The navigation target is
  // available from provenance alone; the Activity panel link waits for the
  // parent row so it can show the real Session name instead of a raw id.
  let subAgentParentTarget = $state(null);

  // A Reflection Session returns to the Session its review forked from.
  let reflectionSession = $state(false);
  let reflectionSourceTarget = $state(null);

  let sessionParentLink = $state(null);

  let sessionParentFetchKey = '';

  // Bumped when a Session invalidation names the displayed Session or its
  // resolved Parent Session (a title, provenance, or deletion change).
  let sessionParentRevision = $state(0);

  let sessionParentAddresses = [];

  let lastSessionInvalidationId = null;

  $effect(() => {
    const entries = context.sessionInvalidations;
    untrack(() => {
      const { targets, lastId, overflowed } = takeSessionInvalidations(
        entries,
        lastSessionInvalidationId,
      );
      lastSessionInvalidationId = lastId;
      if (overflowed || targets.some(namesSessionLineage)) {
        sessionParentRevision += 1;
      }
    });
  });

  function namesSessionLineage(target) {
    if (target.all) {
      return true;
    }
    if (target.renamedAgent) {
      return sessionParentAddresses.some(
        ({ agentAddress }) =>
          agentAddress === target.renamedAgent.oldAgentId ||
          agentAddress === target.renamedAgent.newAgentId,
      );
    }
    // Run completion and read acknowledgement leave titles and provenance
    // unchanged.
    if (target.runId || target.readRunId) {
      return false;
    }
    return sessionParentAddresses.some(
      ({ agentAddress, sessionId }) =>
        agentAddress === target.agentAddress && sessionId === target.sessionId,
    );
  }

  $effect(() => {
    const addressing = context.target.activeAddressing();
    const displayKey = context.target.displayedSessionKey();
    const fetchKey = `${displayKey}::${context.sessionsRefreshToken}::${sessionParentRevision}`;
    if (!displayKey || !addressing.agentAddress || !addressing.sessionId) {
      sessionParentFetchKey = '';
      sessionParentAddresses = [];
      clearParent();
      return;
    }
    if (fetchKey === sessionParentFetchKey) {
      return;
    }
    sessionParentFetchKey = fetchKey;
    sessionParentAddresses = [
      {
        agentAddress: addressing.agentAddress,
        sessionId: addressing.sessionId,
      },
    ];
    clearParent();
    untrack(() =>
      loadSessionParent(
        fetchKey,
        displayKey,
        addressing.agentAddress,
        addressing.sessionId,
      ),
    );
  });

  function clearParent() {
    subAgentParentTarget = null;
    reflectionSession = false;
    reflectionSourceTarget = null;
    sessionParentLink = null;
  }

  const loadSessionParent = async (
    fetchKey,
    displayKey,
    childAddress,
    childSessionId,
  ) => {
    if (!childAddress || !childSessionId) {
      return;
    }
    const superseded = () =>
      context.target.displayedSessionKey() !== displayKey ||
      sessionParentFetchKey !== fetchKey;
    try {
      const child = await context.chatController.getSession(
        childAddress,
        childSessionId,
      );
      // A newer navigation or Session invalidation may have superseded this
      // request mid-flight.
      if (superseded()) {
        return;
      }
      const parent = sessionParentReference(child?.session);
      const relation =
        parent?.kind === 'fork' && isReflectionSession(child?.session)
          ? 'reflection'
          : parent?.kind;
      reflectionSession = relation === 'reflection';
      if (!parent) {
        return;
      }
      const parentAgentId = parent.agent_id;
      const parentSessionId = parent.session_id;
      const parentProjectId = parent.project_id ?? '';
      // A vanished identity parent cannot be opened — keep the
      // return-to-current fallback (a project parent is not roster-checkable
      // and surfaces a load error instead of dead-ending).
      if (!parentProjectId && !context.target.agentById(parentAgentId)) {
        return;
      }
      const parentAgentAddress = formatAgentAddress(
        parentAgentId,
        parentProjectId,
      );
      sessionParentAddresses = [
        ...sessionParentAddresses,
        { agentAddress: parentAgentAddress, sessionId: parentSessionId },
      ];
      const target = {
        agentAddress: parentAgentAddress,
        sessionId: parentSessionId,
        isSubAgentSession: false,
      };
      if (parent.kind === 'subagent') {
        subAgentParentTarget = target;
      } else if (relation === 'reflection') {
        reflectionSourceTarget = target;
      }

      let parentSession = null;
      try {
        const parentRead = await context.chatController.getSession(
          parentAgentAddress,
          parentSessionId,
        );
        if (superseded()) {
          return;
        }
        parentSession = parentRead?.session ?? null;
      } catch {
        // Provenance still supports the existing Sub-Agent return path, but
        // Session info must not present an unresolved link.
        return;
      }
      if (!parentSession) {
        return;
      }
      target.isSubAgentSession =
        parentSession?.is_subagent_session === true ||
        sessionParentReference(parentSession)?.kind === 'subagent';
      if (parent.kind === 'subagent') {
        subAgentParentTarget = target;
      } else if (relation === 'reflection') {
        reflectionSourceTarget = target;
      }
      sessionParentLink = {
        displayName: sessionDisplayName(parentSession),
        relation,
        target,
      };
    } catch {
      // Best effort — the banner button falls back to return-to-current.
    }
  };

  return {
    get subAgentParentTarget() {
      return subAgentParentTarget;
    },
    get reflectionSession() {
      return reflectionSession;
    },
    get reflectionSourceTarget() {
      return reflectionSourceTarget;
    },
    get sessionParentLink() {
      return sessionParentLink;
    },
  };
}
