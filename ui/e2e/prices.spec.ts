import { expect, test } from "@playwright/test";

test("a model price costs calls that reported none", async ({ page }) => {
  await page.goto("/#/prices");
  await expect(page.getByText("No prices yet.")).toBeVisible();
  const form = page.getByRole("form", { name: "Add price" });
  await form.getByLabel("Model pattern").fill("example/fast-chat");
  await form.getByLabel("Input / 1M").fill("1");
  await form.getByLabel("Output / 1M").fill("2");
  await form.getByRole("button", { name: "Add price" }).click();
  await expect(page.getByTestId("price-table").getByRole("row", { name: /example\/fast-chat/ })).toBeVisible();

  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Delete price example/fast-chat" }).click();
  await expect(page.getByText("No prices yet.")).toBeVisible();
});
