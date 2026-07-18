import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("memory mutations are cleaned up and a bundled Skill can be loaded", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_MEMORY Exercise temporary pinned Memory",
    finalText: "Memory tool lifecycle completed.",
  });
  await expectToolSucceeded(page, chat, "memory", 0);
  const memoryList = await expectToolSucceeded(page, chat, "memory", 1);
  await expectToolSucceeded(page, chat, "memory", 2);
  await openToolRow(memoryList);
  await expect(memoryList).toContainText("E2E temporary memory entry");

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_SKILL Load the deterministic bundled Skill",
    finalText: "Skill tool completed.",
  });
  const skill = await expectToolSucceeded(page, chat, "skill");
  await openToolRow(skill);
  await expect(skill).toContainText("weather");
  await expect(skill).toContainText("loaded");
});
