import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import {
  chatTimeline,
  ensureEmptyChat,
  selectChatAgent,
  sendChatMessage,
} from "./chat-run-support.js";

const projectPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "fixtures",
  "mixed-project",
);
const PROJECT_NAME = "Mixed Source E2E Project";

async function removeProject(page) {
  await page.goto("/#projects");
  const projects = page.getByRole("region", { name: "Projects" });
  const projectButton = projects
    .getByRole("complementary", { name: "Projects" })
    .getByRole("button", { name: new RegExp(`^${PROJECT_NAME}(?:\\s|$)`) });
  await expect(projectButton).toBeVisible();
  await projectButton.click();
  const repository = projects.getByRole("region", { name: "Repository" });
  await repository.getByText("Repository management", { exact: true }).click();
  await repository.getByRole("button", { exact: true, name: "Remove" }).click();
  const removeDialog = page.getByRole("dialog");
  await expect(
    removeDialog.getByRole("heading", { name: "Remove project" }),
  ).toBeVisible();
  await expect(removeDialog).toContainText(PROJECT_NAME);
  await removeDialog
    .getByRole("button", { exact: true, name: "Remove" })
    .click();
  await expect(
    projects.getByText(/^Project moved to the Archive\./),
  ).toBeVisible();
  await expect(
    projects.getByText("No projects yet", { exact: true }),
  ).toBeVisible();
}

function teamMember(projects, agentId) {
  return projects
    .getByRole("region", { name: "Team" })
    .getByTestId(`project-team-member-${agentId}`);
}

function projectSkill(projects, skillName) {
  return projects
    .getByRole("region", { name: "Skills" })
    .getByRole("checkbox", { exact: true, name: `Toggle skill ${skillName}` });
}

async function startProjectChat(page, agentName) {
  await page.goto("/#chat");
  const chat = page.getByRole("region", { name: "Chat" });
  await selectChatAgent(page, chat, agentName, { projectName: PROJECT_NAME });
  return ensureEmptyChat(chat);
}

test("a Project mixes Sources and switches them independently through Provider context", async ({
  page,
}) => {
  test.setTimeout(60_000);
  let projectCreated = false;

  try {
    await page.goto("/#projects");
    const projects = page.getByRole("region", { name: "Projects" });
    const projectList = projects.getByRole("complementary", {
      name: "Projects",
    });
    await projectList.getByRole("button", { name: "Add project" }).click();

    const addDialog = page.getByRole("dialog", { name: "Add project" });
    await addDialog
      .getByRole("textbox", { name: "Repository path" })
      .fill(projectPath);
    await addDialog
      .getByRole("textbox", { name: "Display name" })
      .fill(PROJECT_NAME);

    await addDialog.getByRole("button", { name: "Add project" }).click();

    await expect(
      projects.getByText("Project added.", { exact: true }),
    ).toBeVisible();
    projectCreated = true;
    await expect(
      projects.getByRole("heading", { level: 2, name: PROJECT_NAME }),
    ).toBeVisible();
    await expect(teamMember(projects, "open-e2e-worker")).toBeVisible();
    await expect(teamMember(projects, "claude-e2e-reviewer")).toBeVisible();
    await expect(projectSkill(projects, "open-e2e-skill")).toBeChecked();
    await expect(projectSkill(projects, "claude-e2e-skill")).toBeChecked();

    let chat = await startProjectChat(page, "open-e2e-worker");
    await sendChatMessage(
      chat,
      "E2E_PROJECT_AGENT_CONTEXT Verify the selected repository Agent prompt",
    );
    await expect(
      chatTimeline(chat).getByText(
        "OpenCode Project Agent context reached the Provider.",
        {
          exact: true,
        },
      ),
    ).toBeVisible();

    await page.goto("/#projects");
    await projects
      .getByRole("complementary", { name: "Projects" })
      .getByRole("button", { name: new RegExp(`^${PROJECT_NAME}(?:\\s|$)`) })
      .click();
    const sources = projects.getByRole("region", { name: "Sources" });
    await sources.getByRole("switch", { exact: true, name: "Toggle OpenCode · Agents" }).click();

    await expect(teamMember(projects, "claude-e2e-reviewer")).toBeVisible();
    await expect(teamMember(projects, "open-e2e-worker")).toHaveCount(0);
    await expect(projectSkill(projects, "claude-e2e-skill")).toBeChecked();
    await expect(projectSkill(projects, "open-e2e-skill")).toBeChecked();

    chat = await startProjectChat(page, "claude-e2e-reviewer");
    await sendChatMessage(
      chat,
      "E2E_PROJECT_AGENT_CONTEXT Verify the switched repository Agent prompt",
    );
    await expect(
      chatTimeline(chat).getByText(
        "Claude Project Agent context reached the Provider.",
        {
          exact: true,
        },
      ),
    ).toBeVisible();
  } finally {
    if (projectCreated) {
      await removeProject(page);
    }
  }
});
