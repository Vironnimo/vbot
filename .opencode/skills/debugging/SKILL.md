---
name: debugging
description: Structured debugging methodology — reproduce, hypothesize, isolate, fix root cause. Trigger when investigating bugs, crashes, unexpected behavior, or failing tests where the cause is unclear. Examples: "debug X", "why does Y crash", "this test fails and I don't know why", "X returns the wrong result". Do NOT trigger for known issues with obvious fixes.
---

# Debugging

Debugging is investigation, not guessing. Don't jump to fixes — find the root cause first.

The most common debugging failure is pattern-matching on symptoms and applying a "probably this" fix without understanding what's actually wrong. This leads to patches that mask the real problem or break something else.

## Step 1: Reproduce

Make the bug reproducible before doing anything else.

- Find the exact input, state, or sequence that triggers the bug
- Reduce to the simplest reproduction case
- If the bug is intermittent: identify the conditions that make it more/less likely

If you cannot reproduce it: document what you tried and report back. Do not guess-fix an unreproducible bug.

## Step 2: Hypothesize

Form hypotheses — explicitly, not just in your reasoning.

```markdown
## Hypotheses
1. [Most likely cause] — test: [how to verify/falsify]
2. [Second candidate] — test: [how to verify/falsify]
3. [Less likely but possible] — test: [how to verify/falsify]
```

Order by likelihood. Each hypothesis must have a concrete test — something you can check that would confirm or rule it out. Start with the most likely one.

## Step 3: Isolate

Narrow down systematically. For each hypothesis:

1. Test it — read code, add logging, run with specific inputs
2. Result: **confirmed** → go to Step 4. **Ruled out** → next hypothesis.
3. If all hypotheses are ruled out: form new ones based on what you learned

Track what you tried:

```markdown
## Investigation Log
- Hypothesis 1: [description] → ❌ Ruled out because [evidence]
- Hypothesis 2: [description] → ✅ Confirmed: [evidence]
```

Do not skip this. "I think it's probably X" is not isolation.

## Step 4: Fix the root cause

Fix the actual cause, not the symptom.

- **Root cause fix:** the bug cannot recur in any form
- **Symptom fix:** the specific manifestation is suppressed, but the underlying issue remains

If the root cause is outside your file scope: report back to the Orchestrator with the root cause and which files need to change.

## Step 5: Verify

1. Confirm the original reproduction case no longer triggers the bug
2. Write a regression test that would have caught this bug
3. Run the tests for the area you fixed plus the new regression test — the fix must not break anything else in scope

## Rules

- Never guess-fix — if you don't understand the cause, you haven't debugged it
- One fix at a time — don't change multiple things and hope one of them helps
- Don't clean up unrelated code while debugging — stay focused
- If debugging takes too long without progress, report back with your investigation log rather than continuing blindly
