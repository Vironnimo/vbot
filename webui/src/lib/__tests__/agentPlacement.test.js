import { describe, expect, it } from 'vitest';
import {
  agentDisplayOrder,
  agentDisplayName,
  automaticAgentTargets,
  canDeleteAgent,
  firstChatAgent,
  helperAgents,
  privateAgents,
  reorderAgentGroup,
} from '../agentPlacement.js';

const owner = { id: 'owner', name: 'Owner' };
const child = {
  id: 'child',
  name: 'Child',
  placement: { kind: 'private', owner_id: 'owner', orphaned: false },
};
const sibling = {
  id: 'sibling',
  placement: { kind: 'private', owner_id: 'owner', orphaned: false },
};
const orphan = {
  id: 'orphan',
  placement: { kind: 'private', owner_id: 'gone', orphaned: true },
};
const helper = { id: 'helper', placement: { kind: 'helper' } };
const other = { id: 'other' };
const roster = [helper, child, owner, orphan, other, sibling];

describe('Agent placement projection', () => {
  it('keeps private children with their owner and orphans in the Helper group', () => {
    expect(agentDisplayOrder(roster).map((agent) => agent.id)).toEqual([
      'owner',
      'child',
      'sibling',
      'other',
      'helper',
      'orphan',
    ]);
    expect(privateAgents(roster, 'owner')).toEqual([child, sibling]);
    expect(helperAgents(roster)).toEqual([helper, orphan]);
    expect(firstChatAgent(roster)).toBe(owner);
    expect(agentDisplayName(child, roster)).toBe('Owner / Child');
    expect(agentDisplayName(orphan, roster)).toBe('orphan');
    expect(canDeleteAgent(owner, [helper, child, owner])).toBe(false);
    expect(canDeleteAgent(helper, [helper, owner])).toBe(true);
  });
  it('keeps automatic targets to the owner and verified siblings', () => {
    expect(automaticAgentTargets(roster, owner)).toEqual([child, sibling]);
    expect(automaticAgentTargets(roster, child)).toEqual([sibling]);
    expect(automaticAgentTargets(roster, orphan)).toEqual([]);
    expect(automaticAgentTargets(roster, helper)).toEqual([]);
  });
  it('reorders one group inside its existing flat roster positions', () => {
    expect(reorderAgentGroup(roster, ['sibling', 'child'])).toEqual([
      'helper',
      'sibling',
      'owner',
      'orphan',
      'other',
      'child',
    ]);
    expect(reorderAgentGroup(roster, ['other', 'owner'])).toEqual([
      'helper',
      'child',
      'other',
      'orphan',
      'owner',
      'sibling',
    ]);
  });
});
