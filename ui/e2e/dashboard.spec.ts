import { expect, test } from "@playwright/test";

test("dashboard shows figures, charts and breakdowns for the demo data", async ({ page }) => {
  await page.goto("/#/dashboard");
  await page.getByRole("button", { name: "All time" }).click();
  const tiles = page.getByTestId("tile");
  await expect(tiles).toHaveCount(8);
  await expect(tiles.filter({ has: page.getByText("Model calls", { exact: true }) })).toContainText(/\d+/);
  await expect(page.getByRole("img", { name: "Cost per time bucket" })).toBeVisible();
  await expect(page.getByRole("img", { name: /Model call latency/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "By model" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "example/large-reasoner" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "researcher" })).toBeVisible();

  // Every chart has its numbers available as a table too.
  await page.getByText("Data table").first().click();
  await expect(page.getByRole("columnheader", { name: "Bucket start" }).first()).toBeVisible();

  await page.getByRole("link", { name: "Traces" }).click();
  await expect(page.getByTestId("trace-row").first()).toBeVisible();
});
