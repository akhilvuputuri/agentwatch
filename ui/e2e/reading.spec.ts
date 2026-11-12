import { expect, type APIRequestContext, test } from "@playwright/test";

interface Listed {
  trace_id: string;
  error_count: number;
  tool_calls: number;
}

/** A demo trace in which a tool call failed, so there is a first error with a result to read. */
async function failedToolTrace(request: APIRequestContext): Promise<{ traceId: string; tool: string }> {
  const listed = (await (await request.get("/api/traces?limit=200&errors=true")).json()) as { traces: Listed[] };
  for (const each of listed.traces) {
    const detail = (await (await request.get(`/api/traces/${each.trace_id}`)).json()) as {
      spans: { status_code: number; genai: { operation: string | null; tool_name: string | null } }[];
    };
    const failed = detail.spans.filter((span) => span.status_code === 2 && span.genai.operation !== "invoke_agent");
    if (failed[0]?.genai.operation === "execute_tool") return { traceId: each.trace_id, tool: failed[0].genai.tool_name! };
  }
  throw new Error("the demo data has no trace whose first failure is a tool call");
}

test("a trace opens on what was asked, what was replied and the first error", async ({ page, request }) => {
  const { traceId, tool } = await failedToolTrace(request);
  await page.goto(`/#/trace/${traceId}`);
  const summary = page.getByTestId("turn-summary");
  await expect(summary.getByRole("heading", { name: "Asked" })).toBeVisible();
  await expect(summary).toContainText(/\?|\./); // the request's own words, not JSON
  await expect(summary).not.toContainText('"parts"');
  const first = page.getByTestId("first-error");
  await expect(first).toContainText(`execute_tool ${tool}`);
  await expect(first).toContainText("The demo service did not answer in time.");

  // The failed span is the one selected, without a click.
  const detail = page.getByTestId("span-detail");
  await expect(detail.getByRole("heading", { level: 2 })).toContainText(tool);
  await expect(page.getByTestId("span-output")).toContainText('"error": "The demo service did not answer in time."');
  await expect(page.getByTestId("span-input")).toContainText('"');

  // Picking a span puts it in the address without adding a history entry.
  await page.getByTestId("span-row").filter({ hasText: "chat" }).first().click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}\/[0-9a-f]{16}$/);
  await expect(page.getByTestId("span-input").getByText("system", { exact: true })).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByTestId("span-row").nth(await selectedIndex(page))).toBeFocused();

  // Steps reads each tool's input and output in place, and the choice is remembered.
  await page.getByRole("button", { name: "Steps" }).click();
  const step = page.getByTestId("step").filter({ has: page.locator(".name", { hasText: tool }) }).first();
  await expect(step).toContainText("Input");
  await expect(step).toContainText("Output");
  await page.reload();
  await expect(page.getByRole("button", { name: "Steps" })).toHaveAttribute("aria-pressed", "true");
  await page.goBack();
  await expect(page).not.toHaveURL(/#\/trace\//);
});

async function selectedIndex(page: import("@playwright/test").Page): Promise<number> {
  return page.getByTestId("span-row").evaluateAll((rows) => rows.findIndex((row) => row.getAttribute("aria-selected") === "true"));
}

test("a missing trace says so and offers another try", async ({ page }) => {
  await page.goto(`/#/trace/${"0".repeat(32)}`);
  await expect(page.getByRole("heading", { name: "This trace is not here" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  await page.getByRole("link", { name: "← All traces" }).click();
  await expect(page.getByTestId("trace-row").first()).toBeVisible();
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("the error is on the first screen and a span opens over the page", async ({ page, request }) => {
    const { traceId, tool } = await failedToolTrace(request);
    await page.goto(`/#/trace/${traceId}`);
    const first = page.getByTestId("first-error");
    await expect(first).toBeVisible();
    await expect(first).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);

    // Steps by default; nothing covers the page until a span is opened.
    await expect(page.getByRole("button", { name: "Steps" })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const failed = page.getByTestId("step").filter({ has: page.locator(".name", { hasText: tool }) }).filter({ hasText: "error" }).first();
    await expect(failed.locator(".name")).toHaveText(tool); // the name in full, not cut to a letter

    await first.getByRole("button").first().click();
    const sheet = page.getByRole("dialog");
    await expect(sheet).toBeVisible();
    await expect(sheet.getByTestId("span-output")).toBeInViewport();
    await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}\/[0-9a-f]{16}$/);

    // Next and previous move between spans without adding history; Back closes the span.
    await sheet.getByRole("button", { name: "Next span" }).click();
    await sheet.getByRole("button", { name: "Previous span" }).click();
    await expect(sheet.getByRole("heading", { level: 2 })).toContainText(tool);
    await page.goBack();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}$`));

    await first.getByRole("button").first().click();
    await page.getByRole("button", { name: "← Back to trace" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}$`));
  });

  test("a link to a span opens that span, and closing it shows the trace", async ({ page, request }) => {
    const { traceId } = await failedToolTrace(request);
    const detail = (await (await request.get(`/api/traces/${traceId}`)).json()) as { spans: { span_id: string }[] };
    await page.goto(`/#/trace/${traceId}/${detail.spans[1]!.span_id}`);
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "← Back to trace" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByTestId("turn-summary")).toBeVisible();
  });
});
