import { expect, test } from "@playwright/test";

test.setTimeout(180_000);

test("analyzes a repository, navigates evidence, and persists both themes", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: /see the structure/i })).toBeVisible();
  await expect(
    page.getByText("Static analysis · no repository execution"),
  ).toBeVisible();

  await page.evaluate(() => {
    localStorage.setItem("waypoint-theme", "light");
  });
  await page.reload();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await page.screenshot({
    path: "test-results/screenshots/welcome-light.png",
    fullPage: true,
  });

  await page.getByLabel("Local repository path").fill("backend/tests");
  const analysisResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/analysis") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /map the current repository/i }).click();
  const completedAnalysisResponse = await analysisResponse;
  expect(completedAnalysisResponse.status()).toBe(200);
  const localReport = await completedAnalysisResponse.json();
  const evidenceNodes = localReport.nodes.filter(
    (node: { span?: { path?: string } }) => node.span?.path,
  );
  const firstEvidenceNode = evidenceNodes[0];
  const secondEvidenceNode = evidenceNodes[1] ?? firstEvidenceNode;

  await expect(page.getByText("Repository", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/nodes · .*edges visible/i)).toBeVisible();
  await page.getByTitle(firstEvidenceNode.span.path, { exact: true }).click();
  await expect(page.locator(".source-heading strong")).toContainText(
    /\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|java)$/,
  );
  await expect(page.getByText("Inspector")).toBeVisible();
  const usageResponse = page.waitForResponse(
    (response) => response.url().includes("/usage") && response.request().method() === "GET",
  );
  await page.getByRole("tab", { name: "Usage", exact: true }).click();
  expect((await usageResponse).status()).toBe(200);
  await expect(page.locator(".usage-summary")).toBeVisible();
  await page.getByRole("tab", { name: "Details", exact: true }).click();
  await page.getByRole("button", { name: "File cards", exact: true }).click();
  await expect(page.locator(".file-graph-card").first()).toBeVisible();
  await expect(page.locator(".file-card-members button").first()).toBeVisible();
  await page.getByRole("button", { name: "Symbols", exact: true }).click();
  await page.screenshot({
    path: "test-results/screenshots/analysis-light.png",
    fullPage: true,
  });

  const repositorySidebar = page.locator(".sidebar");
  const repositoryWidth = (await repositorySidebar.boundingBox())?.width ?? 0;
  expect(repositoryWidth).toBeGreaterThan(200);
  await page
    .getByRole("button", { name: "Collapse repository sidebar" })
    .click();
  await expect
    .poll(async () => (await repositorySidebar.boundingBox())?.width ?? 0)
    .toBeLessThan(60);
  await page
    .getByRole("button", { name: "Expand repository sidebar" })
    .click();
  await expect
    .poll(async () => (await repositorySidebar.boundingBox())?.width ?? 0)
    .toBeGreaterThan(200);

  const inspectorSidebar = page.locator(".inspector");
  await page
    .getByRole("button", { name: "Collapse inspector sidebar" })
    .click();
  await expect
    .poll(async () => (await inspectorSidebar.boundingBox())?.width ?? 0)
    .toBeLessThan(60);
  await page
    .getByRole("button", { name: "Expand inspector sidebar" })
    .click();

  const sourcePane = page.locator(".source-pane");
  const sourceHeightBefore = (await sourcePane.boundingBox())?.height ?? 0;
  const splitterBox = await page
    .getByRole("separator", { name: "Resize source viewer" })
    .boundingBox();
  if (!splitterBox) throw new Error("Source viewer splitter was not rendered");
  await page.mouse.move(
    splitterBox.x + splitterBox.width / 2,
    splitterBox.y + splitterBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(
    splitterBox.x + splitterBox.width / 2,
    splitterBox.y - 80,
    { steps: 5 },
  );
  await page.mouse.up();
  await expect
    .poll(async () => (await sourcePane.boundingBox())?.height ?? 0)
    .toBeGreaterThan(sourceHeightBefore + 50);

  await page
    .getByRole("button", { name: "Open graph fullscreen" })
    .click();
  await expect(page.locator(".center-stage")).toHaveClass(/graph-fullscreen/);
  await expect(sourcePane).toBeHidden();
  const fullscreenBox = await page.locator(".center-stage").boundingBox();
  expect(fullscreenBox?.x).toBe(0);
  expect(fullscreenBox?.y).toBe(0);
  expect(fullscreenBox?.width).toBe(1440);
  expect(fullscreenBox?.height).toBe(1000);
  await page.screenshot({
    path: "test-results/screenshots/graph-fullscreen-light.png",
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await expect(page.locator(".center-stage")).not.toHaveClass(
    /graph-fullscreen/,
  );
  await expect(sourcePane).toBeVisible();

  let githubRequestBody: { repository_url?: string } = {};
  await page.route("**/api/v1/analysis/github", async (route) => {
    githubRequestBody = route.request().postDataJSON() as {
      repository_url?: string;
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(localReport),
    });
  });
  await page.getByLabel("Repository source").selectOption("github");
  await page
    .getByLabel("GitHub repository URL")
    .fill("https://github.com/pallets/flask");
  await page.getByRole("button", { name: "Analyze", exact: true }).click();
  await expect
    .poll(() => githubRequestBody.repository_url)
    .toBe("https://github.com/pallets/flask");
  await page.getByTitle(firstEvidenceNode.span.path, { exact: true }).click();
  await expect(page.locator(".source-heading strong")).toContainText(
    /\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|java)$/,
  );
  await page.unroute("**/api/v1/analysis/github");

  await page.route("**/api/v1/analyses/*/answer", async (route) => {
    const request = route.request().postDataJSON() as { question: string };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        question: request.question,
        answer: "## Repository overview\n\nThis answer is grounded in the analyzed test source.",
        citations: [{
          node_id: firstEvidenceNode.id,
          qualified_name: firstEvidenceNode.qualified_name,
          kind: firstEvidenceNode.kind,
          span: firstEvidenceNode.span,
          title: firstEvidenceNode.name,
          excerpt: "Source-backed acceptance evidence",
          relevance: "Supports the repository overview",
        }],
        refused: false,
        basis: "E2E source evidence",
        answer_type: "overview",
        summary: "A source-backed repository overview.",
        features: [],
        suggested_questions: [],
        provider: "e2e-fixture",
        conversation_id: "e2e-conversation",
        inspected_file_count: 1,
        tool_trace: [],
      }),
    });
  });
  const inspectorAnswerResponse = page.waitForResponse(
    (response) => response.url().includes("/answer") && response.request().method() === "POST",
  );
  await page.getByRole("tab", { name: "Ask AI", exact: true }).click();
  await page.getByRole("button", { name: "Ask with usage evidence" }).click();
  expect((await inspectorAnswerResponse).status()).toBe(200);
  await expect(page.locator(".symbol-chat-answer")).toContainText(
    "This answer is grounded",
  );
  await page.getByRole("button", { name: "ask", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: /understand the repository through its evidence/i,
    }),
  ).toBeVisible();
  const answerResponsePromise = page.waitForResponse(
    (response) =>
      response.url().includes("/answer") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("textbox", { name: "Repository question" })
    .fill("What does this repository contain?");
  await page
    .getByRole("button", { name: "Send repository question" })
    .click();
  expect((await answerResponsePromise).status()).toBe(200);
  await expect(page.locator(".assistant-message")).toBeVisible();
  await expect(page.getByText("Answer evidence")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Answer files" }))
    .not.toHaveCount(0);
  await expect(page.locator(".evidence-file-list > button").first()).toBeVisible();
  await expect(page.locator(".evidence-context code")).toContainText(/\.py/);
  await expect(page.locator(".evidence-source .monaco-editor")).toBeVisible();
  await page.screenshot({
    path: "test-results/screenshots/discovery-light.png",
    fullPage: true,
  });

  await page.getByRole("button", { name: "onboard", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /learn for the work you need to do/i }),
  ).toBeVisible();
  await page.getByRole("button", { name: /backend routes, services/i }).click();
  await page.route("**/api/v1/analyses/*/tour", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "e2e-tour",
        analysis_id: localReport.analysis_id,
        role: "backend",
        goal: "Understand backend routes and services",
        experience: "new",
        estimated_minutes: 15,
        provider: "e2e-fixture",
        planning_basis: ["Source spans and graph relationships"],
        steps: [firstEvidenceNode, secondEvidenceNode].map(
          (node: typeof firstEvidenceNode, index: number) => ({
            index: index + 1,
            title: index === 0 ? "Start with the test surface" : "Trace the behavior",
            node_id: node.id,
            node_kind: node.kind,
            objective: "Inspect source-backed behavior",
            explanation: "This file anchors the backend flow.",
            why_selected: "It is connected to the tested behavior.",
            evidence: node.span,
            files: [{
              path: node.span.path,
              start_line: node.span.start_line,
              end_line: node.span.end_line,
              node_id: node.id,
              reason: "Primary evidence for this step",
            }],
            challenge: index === 1 ? {
              id: "e2e-challenge",
              prompt: "Which node implements this step?",
              options: [{ node_id: node.id, label: node.name, kind: node.kind }],
              question_type: "multiple_choice",
            } : null,
          }),
        ),
      }),
    });
  });
  await page.route("**/api/v1/analyses/*/mission", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        analysis_id: localReport.analysis_id,
        title: "First contribution",
        risk: "low",
        target_node: firstEvidenceNode,
        rationale: "A bounded source-backed starter task.",
        suggested_files: [firstEvidenceNode.span.path],
        blast_radius_node_ids: [firstEvidenceNode.id],
        checklist: ["Inspect the source"],
        definition_of_done: ["Relevant tests pass"],
        provider: "e2e-fixture",
        confidence: 0.9,
        validation_checks: ["Run tests"],
        status: "proposed",
      }),
    });
  });
  await page.route("**/api/v1/analyses/*/tours/*/answers", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        correct: true,
        explanation: "The selected node matches the source evidence.",
        mastered_node_ids: [secondEvidenceNode.id],
        score: 1,
        mastered_concept_ids: [],
        remediation: null,
      }),
    });
  });
  const tourResponsePromise = page.waitForResponse(
    (response) =>
      response.url().includes("/tour") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /generate my route/i }).click();
  const tourResponse = await tourResponsePromise;
  expect(tourResponse.status()).toBe(200);
  const tour = (await tourResponse.json()) as {
    steps: Array<{
      node_id: string;
      challenge: { id: string } | null;
    }>;
  };
  await expect(
    page.getByRole("heading", { name: "First contribution", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Model-selected reading order", { exact: true })).toBeVisible();
  await expect(page.locator(".guided-source-viewer .monaco-editor")).toBeVisible();

  const challengedStepIndex = tour.steps.findIndex((step) => step.challenge);
  expect(challengedStepIndex).toBeGreaterThan(0);
  await page
    .getByRole("navigation", { name: "Tour steps" })
    .getByRole("button")
    .nth(challengedStepIndex)
    .click();
  await page
    .locator(`input[value="${tour.steps[challengedStepIndex].node_id}"]`)
    .check();
  await page.getByRole("button", { name: /check answer/i }).click();
  await expect(page.getByText("100%")).toBeVisible();
  await page.screenshot({
    path: "test-results/screenshots/onboarding-light.png",
    fullPage: true,
  });

  await page.getByRole("button", { name: "issues", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: /issues, history, and evidence-backed findings/i,
    }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "GitHub history" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Waypoint proposals" })).toBeVisible();

  const themeButton = page.getByRole("button", { name: /switch to dark mode/i });
  await themeButton.click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("waypoint-theme")))
    .toBe("dark");
  await page.screenshot({
    path: "test-results/screenshots/analysis-dark.png",
    fullPage: true,
  });

  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  expect(consoleErrors).toEqual([]);
});
