import { expect, test } from "@playwright/test";

test("two runs compare with verdicts and changed items", async ({ page }) => {
  await page.goto("/#/datasets");
  await page.getByTestId("dataset-row").filter({ hasText: "Demo requests" }).getByRole("link").click();
  await page.getByLabel("Baseline baseline").check();
  await page.getByLabel("Candidate prompt v2").check();
  await page.getByRole("link", { name: "Compare runs" }).click();
  await expect(page.getByRole("heading", { name: /baseline vs prompt v2/ })).toBeVisible();
  const cards = page.getByTestId("compare-card");
  const card = (name: string) => cards.filter({ has: page.getByText(name, { exact: true }) });
  await expect(card("exact")).toBeVisible();
  // Six items cannot settle anything.
  await expect(card("exact").getByTestId("verdict")).toContainText("underpowered");
  await expect(card("latency")).toBeVisible();
  await page.getByLabel("Only items that changed").uncheck();
  await expect(page.getByTestId("compare-items").getByRole("row")).toHaveCount(7);
});
