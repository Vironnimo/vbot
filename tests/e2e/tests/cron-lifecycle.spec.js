import { expect, test } from "@playwright/test";

import { parkPointer, startIsolatedChat } from "./chat-run-support.js";
import { rpc } from "./rpc-support.js";

const SCHEDULED_JOB_NAME = "E2E One-time Delivery";
const SCHEDULED_PROMPT = "E2E_SCHEDULED_RUN report back";

test("a Scheduled Run persists through update, pause, and deletion", async ({
  page,
}) => {
  await page.goto("/#cron");

  const cron = page.getByRole("region", { name: "Schedules" });
  const cronListPane = cron.getByRole("complementary", {
    name: "Schedules",
  });
  await expect(
    cron.getByText("No scheduled runs yet", { exact: true }),
  ).toBeVisible();
  await cronListPane.getByRole("button", { name: "Create schedule" }).click();

  await expect(
    cron.getByText("Create schedule", { exact: true }),
  ).toBeVisible();
  await cron
    .getByRole("textbox", { exact: true, name: "Name" })
    .fill("E2E Morning Schedule");
  await cron
    .getByRole("textbox", { name: "Prompt" })
    .fill("E2E scheduled prompt");
  await cron.getByPlaceholder("0 9 * * 1-5").fill("0 6 * * *");
  await cron.getByRole("button", { exact: true, name: "Save" }).click();

  const jobs = cron.getByRole("list", { name: "Scheduled Runs" });
  let job = jobs.getByRole("button", {
    name: /^E2E Morning Schedule Active\b/,
  });
  await expect(job).toBeVisible();
  await expect(
    cron.locator('.cron-summary[aria-label="Schedule summary"]'),
  ).toContainText("0 6 * * *");

  await cron
    .getByRole("textbox", { exact: true, name: "Name" })
    .fill("E2E Updated Schedule");
  await cron
    .getByRole("textbox", { name: "Prompt" })
    .fill("E2E updated prompt");
  await cron.getByPlaceholder("0 9 * * 1-5").fill("30 7 * * *");
  await cron.getByRole("button", { exact: true, name: "Save" }).click();

  job = jobs.getByRole("button", {
    name: /^E2E Updated Schedule Active\b/,
  });
  await expect(job).toBeVisible();
  await expect(
    cron.locator('.cron-summary[aria-label="Schedule summary"]'),
  ).toContainText("30 7 * * *");
  await expect(cron.getByRole("textbox", { name: "Prompt" })).toHaveValue(
    "E2E updated prompt",
  );

  await page.reload();
  job = jobs.getByRole("button", {
    name: /^E2E Updated Schedule Active\b/,
  });
  await expect(job).toBeVisible();
  await job.click();
  await expect(
    cron.getByRole("textbox", { exact: true, name: "Name" }),
  ).toHaveValue("E2E Updated Schedule");

  const disableJob = cron.getByRole("switch", { name: /^Disable job / });
  await expect(disableJob).toBeChecked();
  await disableJob.click();

  await expect(
    jobs.getByRole("button", {
      name: /^E2E Updated Schedule Paused\b/,
    }),
  ).toBeVisible();
  await expect(
    cron.getByRole("switch", { name: /^Enable job / }),
  ).not.toBeChecked();

  await cron.getByText("Technical details", { exact: true }).click();
  await cron.getByRole("button", { name: /^Delete job / }).click();
  const deleteDialog = page.getByRole("dialog", {
    name: "Delete Scheduled Run",
  });
  await expect(deleteDialog).toContainText("Delete this job permanently?");
  await deleteDialog
    .getByRole("button", { exact: true, name: "Delete" })
    .click();

  await expect(
    cron.getByText("No scheduled runs yet", { exact: true }),
  ).toBeVisible();
  await expect(jobs).toHaveCount(0);
});

test("a one-time Scheduled Run fires and its result reaches the open Chat", async ({
  page,
  request,
}) => {
  test.setTimeout(60_000);
  let jobId = "";
  try {
    const chat = await startIsolatedChat(page, { agentName: "Main" });
    const drawer = chat.getByRole("complementary", { name: "Sessions" });
    await chat.getByRole("button", { exact: true, name: "Sessions" }).click();
    await expect(drawer).toBeVisible();
    // Scheduled Runs keep their own Sessions, hidden from the list by default.
    await drawer.getByRole("button", { name: "Session list filters" }).click();
    await page
      .getByRole("menu")
      .getByRole("switch", { name: "Cron runs" })
      .click();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(drawer).toBeVisible();
    const scheduledSession = drawer
      .getByRole("listitem")
      .filter({ hasText: SCHEDULED_PROMPT });
    await expect(scheduledSession).toHaveCount(0);

    // A one-time schedule a few seconds ahead; the browser form only offers
    // minute precision, and the UI creation path is covered above.
    const job = await rpc(request, "cron.create", {
      agent_id: "main",
      name: SCHEDULED_JOB_NAME,
      prompt: SCHEDULED_PROMPT,
      schedule_type: "once",
      run_at: new Date(Date.now() + 4_000).toISOString(),
    });
    jobId = job.id;

    // The fired Run's fresh Session appears live in the already open Chat.
    await expect(scheduledSession).toBeVisible({ timeout: 30_000 });
    await parkPointer(page);
    await scheduledSession.locator("button.session-row__select").click();
    await expect(
      chat.getByText("Scheduled Run delivered 4471.", { exact: true }),
    ).toBeVisible();

    await page.goto("/#cron");
    const cron = page.getByRole("region", { name: "Schedules" });
    const completedJob = cron
      .getByRole("list", { name: "Scheduled Runs" })
      .getByRole("button", {
        name: new RegExp(`^${SCHEDULED_JOB_NAME} Completed\\b`),
      });
    await expect(completedJob).toBeVisible();
    await completedJob.click();
    await expect(
      cron.locator('.cron-summary[aria-label="Schedule summary"]'),
    ).toContainText("Succeeded");
  } finally {
    if (jobId) {
      await rpc(request, "cron.delete", { id: jobId });
    }
  }
});
