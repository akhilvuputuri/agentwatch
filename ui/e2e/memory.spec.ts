import { expect, test } from "@playwright/test";

test("memory list, an item's history and memory in a model call's context", async ({ page }) => {
  await page.goto("/#/memory");
  const rows = page.getByTestId("memory-row");
  await expect(rows.first()).toBeVisible();

  await page.getByRole("searchbox").fill("UTC");
  await expect(rows).toHaveCount(1);
  await rows.first().getByRole("link").click();
  await expect(page).toHaveURL(/#\/memory\/item\?/);
  await expect(page.getByRole("heading", { name: "timezone" })).toBeVisible();
  await expect(page.getByTestId("memory-changes").getByRole("listitem")).toHaveCount(1);
  const reads = page.getByTestId("memory-reads").getByRole("listitem");
  await expect(reads.first()).toBeVisible();

  // A read opens its trace at the read; its model calls list the items.
  await reads.first().getByRole("link").first().click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}\/[0-9a-f]{16}$/);
  await page.getByTestId("span-row").filter({ hasText: "chat" }).first().click();
  await expect(page.getByTestId("memory-in-context")).toContainText("does not mean the model used");
  await page.getByRole("link", { name: "timezone" }).click();
  await expect(page.getByRole("heading", { name: "timezone" })).toBeVisible();
});
