import { expect, test } from "@playwright/test";

test("lists demo traces and opens a span tree", async ({ page }) => {
  await page.goto("/");
  const rows = page.getByTestId("trace-row");
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBe(50);

  await page.getByRole("button", { name: "Load older traces" }).click();
  await expect(rows).toHaveCount(100);

  await rows.first().getByRole("link").click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}$/);
  const spans = page.getByTestId("span-row");
  await expect(spans.first()).toBeVisible();

  const chat = spans.filter({ hasText: "chat" }).first();
  await chat.click();
  const detail = page.getByTestId("span-detail");
  await expect(detail).toContainText("Input tokens");
  await expect(detail).toContainText("gen_ai.request.model");

  await page.getByRole("link", { name: "← All traces" }).click();
  await expect(rows.first()).toBeVisible();
});

test("filters to traces with errors", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Errors only").check();
  const rows = page.getByTestId("trace-row");
  await expect(rows.filter({ hasNotText: "error" })).toHaveCount(0);
  await expect(rows.first()).toBeVisible();
});
