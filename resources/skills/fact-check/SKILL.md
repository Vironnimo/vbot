---
name: fact-check
description: Check factual claims in text, linked articles and screenshots. Use when the user asks whether a claim is true, misleading or supported by evidence; not for ordinary research, summaries or media-authenticity checks.
compatibility: Web search and page reading for external verification; image reading for screenshots.
---

# Fact check

Produce an evidence-backed assessment of the claims the user asks you to check, with source links and remaining uncertainty. Check statements in text, linked articles and screenshots; image, video and audio authenticity are outside this workflow. Research and report in the conversation; contacting people or publishing a correction requires the user's request.

## Steps

1. Identify the claims that determine the answer.
   Read the supplied material and separate independently checkable statements, preserving qualifications, dates, places and quantities.
   A screenshot supplies claims to check, not proof of who published them.
   If material is inaccessible or illegible, check the readable claims and ask for the missing text when it prevents an assessment.
   Separate factual premises from opinions, satire and predictions; assess those premises without presenting the opinion or future outcome as a verified fact.
   Done when the scope and wording of each material claim are clear; ask a question only if different interpretations would change the answer.

2. Trace the evidence behind each claim.
   Use available search and page-reading Tools to locate and read the original document, recording, dataset or study and its relevant context.
   Search snippets, generated summaries and existing fact-checks are leads; read the evidence they point to before relying on their conclusion.
   If the original is unavailable, use accessible reporting that identifies its evidence and state the limitation.
   If external research is unavailable, distinguish what the supplied material establishes from what remains externally unverified.
   Done when each claim has relevant evidence you actually read or an identified evidence gap.

3. Challenge the initial interpretation.
   Search for evidence that could contradict it, using neutral terms and relevant original-language sources where useful.
   Check source competence, proximity to the event, interests and methods: an official statement establishes what its issuer said, not automatically that the statement is true.
   Count independent evidence, not the number of pages repeating it.
   For quotes and events, check the original wording, publication date and event date; for numbers, check the denominator, period, units and comparison.
   For scientific or causal claims, check study design and the wider evidence, because one study or a correlation does not establish a general causal conclusion.
   Resolve material contradictions by comparing the underlying evidence; apply the same standard regardless of the claimant's politics or whether the conclusion pleases the user.
   Done when you have checked the plausible counterevidence and the context that could change the assessment.

4. Decide whether more research can change the answer.
   Finish a claim when the evidence directly addresses its wording and material counterevidence has been resolved, or when the remaining gap is clear.
   If a search finds nothing useful, try a materially different query or a relevant original source; if it still leaves the same gap, report it instead of repeating searches.
   A failed search is not evidence that a claim is false.
   If the search service fails or rate-limits you, stop rephrasing against that failing service and read known relevant pages directly or report the gap.
   If a page is inaccessible, try an accessible original or independently reported source; distinguish an access failure from an absence of evidence.
   Done when every material claim has an assessment or a specific reason it remains unresolved.

5. Assign an assessment to each material claim.
   Before choosing the assessment, match the claim's exact wording to a supporting or contradicting passage from a source you opened, or identify the missing evidence.
   If you have only search snippets or remembered facts, read a relevant source with a page-reading Tool before issuing a factual verdict; if that fails, mark the claim unresolved and explain what remains unverified.
   Use these meanings, translated into the user's language:
   - Supported: the evidence supports the claim at its stated scope and time.
   - Refuted: evidence contradicts the claim, rather than merely failing to confirm it.
   - Misleading: a factual element is accurate, but omitted context or an unsupported inference changes its meaning.
   - Unresolved: available evidence is insufficient, inaccessible or materially conflicting; name which applies.
   A rule forbidding an act does not prove that it never happened; an alleged exception or individual event needs evidence about that event.
   For mixed claims, assess their parts separately rather than labeling the whole article false.
   State non-checkable opinions or predictions as such, without forcing them into a factual verdict.
   Explain uncertainty through the evidence and its limits, without invented confidence percentages or claims about deceptive intent.
   Done when every assessment follows from the evidence you read, with your inference distinguished from the source's statements.

6. Answer in the user's language, leading with the result.
   For one claim, give the assessment, the decisive reason and direct links beside the claims they support; for several, use a short list or table by claim.
   Include the date of the check, relevant event or data dates and any gap that could change the conclusion.
   Cite pages you actually read and check that the cited passage supports your wording; mark indirect evidence as indirect.
   Keep the first answer focused on the decisive evidence and uncertainties; expand when the user asks for depth.
   Done when the user can see what was checked, why the assessment follows and what remains unknown.

## Answer pattern

Replace the angle-bracket placeholders with the actual findings and omit an empty limitation:

```text
<Assessment>: <precise claim and decisive reason>. [<Source title>](<source-url>)

<Remaining uncertainty or missing context, if any.> Checked on <date>.
```

For several claims, repeat the assessment and evidence for each and give the check date once.
