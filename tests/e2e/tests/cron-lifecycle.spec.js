import { expect, test } from "@playwright/test";

test("a Scheduled Run persists through update, pause, and deletion", async ({
  page,
}) => {
  await page.goto("/#cron");

  const cron = page.getByRole("region", { name: "Scheduled Runs" });
  const cronListPane = cron.getByRole("complementary", {
    name: "Scheduled Runs",
  });
  await expect(
    cron.getByText("No scheduled runs yet", { exact: true }),
  ).toBeVisible();
  await cronListPane.getByRole("button", { exact: true, name: "Add" }).click();

  await expect(
    cron.getByText("Create Scheduled Run", { exact: true }),
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
