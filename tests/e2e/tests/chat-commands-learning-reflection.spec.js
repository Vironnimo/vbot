import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

test("learn authors a private Skill and reflect creates a review Fork", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page, { agentName: "Main" });
  const drawer = chat.getByRole("complementary", { name: "Sessions" });

  await sendChatMessage(
    chat,
    "/learn E2E_LEARN_COMMAND author the deterministic test Skill",
  );
  await expect(
    chat
      .locator(".chat-view__command-toast")
      .getByText("Created private Skill e2e-learned-command.", {
        exact: true,
      }),
  ).toBeVisible();

  await sendChatMessage(
    chat,
    "E2E_CLEAN_LEARNED_COMMAND remove the test Skill",
  );
  await expect(
    chat.getByText("Learned command Skill cleaned up.", { exact: true }),
  ).toBeVisible();
  await expect(chat.getByText("· Running", { exact: true })).toHaveCount(0);

  await drawer
    .getByRole("button", { name: "Session list filters" })
    .click();
  await page
    .getByRole("menu")
    .getByRole("switch", { name: "Skill reflections" })
    .click();
  const previousSessionCount = await drawer.getByRole("listitem").count();
  await sendChatMessage(chat, "/reflect E2E reflection focus");
  await expect(
    chat
      .locator(".chat-view__command-toast")
      .getByText("E2E reflection completed.", { exact: true }),
  ).toBeVisible();
  await expect(drawer.getByRole("listitem")).toHaveCount(
    previousSessionCount + 1,
  );
  const reflectionMarker = drawer.locator(
    '[data-session-marker="reflection"]',
  );
  await expect(reflectionMarker).toBeVisible();
  await expect(reflectionMarker).toHaveAttribute("aria-label", "Reflection");
});
