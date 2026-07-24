import { expect, test } from "@playwright/test";

import { startIsolatedChat } from "./chat-run-support.js";
import {
  expectToolSucceeded,
  openToolRow,
  runToolScenario,
} from "./chat-tool-support.js";

test("the cron tool creates, pauses, resumes, lists, and deletes a Scheduled Run", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);

  await runToolScenario(chat, {
    prompt: "E2E_TOOL_CRON Exercise the full schedule lifecycle",
    finalText: "Schedule tool lifecycle completed.",
  });

  for (let index = 0; index < 5; index += 1) {
    await expectToolSucceeded(page, chat, "cron", index);
  }
  const list = await expectToolSucceeded(page, chat, "cron", 3);
  await openToolRow(list);
  await expect(list).toContainText("E2E tool-created schedule");

  await page.goto("/#cron");
  await expect(
    page
      .getByRole("region", { name: "Scheduled Runs" })
      .getByText("No scheduled runs yet", { exact: true }),
  ).toBeVisible();
});
