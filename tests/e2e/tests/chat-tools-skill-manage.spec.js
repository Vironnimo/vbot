import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("an Agent can author, activate, and clean up a private Skill", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_SKILL_MANAGE Exercise private Skill authoring",
    finalText: "Skill authoring lifecycle completed.",
  });

  for (let index = 0; index < 11; index += 1) {
    await expectToolSucceeded(page, chat, "skill_manage", index);
  }
  const activation = await expectToolSucceeded(page, chat, "skill");
  await openToolRow(activation);
  await expect(activation).toContainText("e2e-authored");
  await expect(activation).toContainText("Updated instruction marker.");
  await expect(activation).toContainText("references/evidence.txt");

  const deletion = await expectToolSucceeded(page, chat, "skill_manage", 10);
  await openToolRow(deletion);
  await expect(deletion).toContainText("delete");
  await expect(deletion).toContainText("was archived and can be recovered");
});
