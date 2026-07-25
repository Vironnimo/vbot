import { expect, test } from "@playwright/test";

import { sendChatMessage, startIsolatedChat } from "./chat-run-support.js";

const EARLIER_SESSION_TITLE = "E2E Earlier Session Navigation";
const EARLIER_SESSION_SEED_MESSAGE =
  "E2E_STREAM Seed the earlier Session before navigation";
const EARLIER_SESSION_MESSAGE =
  "E2E_STREAM Continue the selected earlier Session";

test("Sessions can be selected and continued without a past-session warning", async ({
  page,
}) => {
  const chat = await startIsolatedChat(page);
  const sessionDrawer = chat.getByRole("complementary", { name: "Sessions" });
  const sessionList = sessionDrawer.getByRole("list");
  const selectedSession = sessionDrawer.locator(
    "button.session-row__select--active",
  );
  await expect(selectedSession).toHaveCount(1);

  await sendChatMessage(chat, `/rename ${EARLIER_SESSION_TITLE}`);
  const earlierSession = sessionList
    .getByRole("listitem")
    .filter({ hasText: EARLIER_SESSION_TITLE });
  await expect(earlierSession).toBeVisible();
  await sendChatMessage(chat, EARLIER_SESSION_SEED_MESSAGE);
  await expect(
    chat.getByText("Fake provider streaming response.", { exact: true }),
  ).toBeVisible();
  const previousCount = await sessionList.getByRole("listitem").count();

  await chat.getByRole("button", { exact: true, name: "New session" }).click();

  await expect(sessionList.getByRole("listitem")).toHaveCount(
    previousCount + 1,
  );
  await expect(selectedSession).toHaveCount(1);
  await expect(
    chat.getByText("No messages yet", { exact: true }).first(),
  ).toBeVisible();

  await earlierSession.locator("button.session-row__select").click();
  await expect(
    earlierSession.locator("button.session-row__select"),
  ).toHaveClass(/session-row__select--active/);
  await expect(
    chat.getByText("Viewing a past session", { exact: true }),
  ).toHaveCount(0);
  await expect(
    chat.getByRole("button", {
      exact: true,
      name: "Return to current session",
    }),
  ).toHaveCount(0);

  const providerResponses = chat.getByText(
    "Fake provider streaming response.",
    { exact: true },
  );
  const previousResponseCount = await providerResponses.count();
  await sendChatMessage(chat, EARLIER_SESSION_MESSAGE);
  await expect(
    chat.getByText(EARLIER_SESSION_MESSAGE, { exact: true }),
  ).toBeVisible();
  await expect(providerResponses).toHaveCount(previousResponseCount + 1);
});
