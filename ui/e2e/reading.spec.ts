import { expect, type APIRequestContext, type Page, test } from "@playwright/test";

interface ApiSpan {
  span_id: string;
  parent_span_id: string | null;
  status_code: number;
  attributes: Record<string, string>;
  genai: { operation: string | null; tool_name: string | null };
}

interface Found {
  traceId: string;
  spans: ApiSpan[];
  /** What the demo user asked, as plain words. */
  asked: string;
}

/** A demo trace chosen by its spans, with what was asked in it. */
async function traceWhere(request: APIRequestContext, wanted: (spans: ApiSpan[]) => boolean): Promise<Found> {
  const listed = (await (await request.get("/api/traces?limit=200&errors=true")).json()) as { traces: { trace_id: string }[] };
  for (const each of listed.traces) {
    const detail = (await (await request.get(`/api/traces/${each.trace_id}`)).json()) as { spans: ApiSpan[] };
    if (!wanted(detail.spans)) continue;
    const root = detail.spans.find((span) => span.parent_span_id === null)!;
    const input = JSON.parse(root.attributes["gen_ai.input.messages"]!) as { parts: { content: string }[] }[];
    return { traceId: each.trace_id, spans: detail.spans, asked: input[0]!.parts[0]!.content };
  }
  throw new Error("the demo data has no such trace");
}

const failed = (spans: ApiSpan[]) => spans.filter((span) => span.status_code === 2 && span.genai.operation !== "invoke_agent");
/** The turn's first failure is a tool call, so there is a result to read. */
const toolFailedFirst = (spans: ApiSpan[]) => failed(spans)[0]?.genai.operation === "execute_tool";

function selectedIndex(page: Page): Promise<number> {
  return page.getByTestId("span-row").evaluateAll((rows) => rows.findIndex((row) => row.getAttribute("aria-selected") === "true"));
}

test("a trace opens on what was asked, what was replied and the first error", async ({ page, request }) => {
  const { traceId, spans, asked } = await traceWhere(request, toolFailedFirst);
  const tool = failed(spans)[0]!.genai.tool_name!;
  await page.goto(`/#/trace/${traceId}`);
  const summary = page.getByTestId("turn-summary");
  // The request's own words, not the JSON that carries them.
  await expect(summary.locator(".turn-part").first()).toHaveText(`Asked${asked}`);
  const first = page.getByTestId("first-error");
  await expect(first).toContainText(`execute_tool ${tool}`);
  await expect(first).toContainText("The demo service did not answer in time.");

  // The failed span is the one selected, without a click.
  const detail = page.getByTestId("span-detail");
  await expect(detail.getByRole("heading", { level: 2 })).toContainText(tool);
  await expect(page.getByTestId("span-output")).toContainText('"error": "The demo service did not answer in time."');
  await expect(page.getByTestId("span-input").locator("pre")).toHaveText(/^\{\n {2}"/); // indented JSON

  // Picking a span puts it in the address without adding a history entry.
  await page.getByTestId("span-row").filter({ hasText: "chat" }).first().click();
  await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}\/[0-9a-f]{16}$/);
  await expect(page.getByTestId("span-input").getByText("system", { exact: true })).toBeVisible();
  const before = await selectedIndex(page);
  await page.keyboard.press("ArrowDown");
  await expect.poll(() => selectedIndex(page)).toBe(before + 1);
  await expect(page.getByTestId("span-row").nth(before + 1)).toBeFocused();
  await page.keyboard.press("Shift+ArrowUp"); // a modified key is not ours to take
  expect(await selectedIndex(page)).toBe(before + 1);

  // Steps reads each tool's input and output in place, and the choice is remembered.
  await page.getByRole("button", { name: "Steps" }).click();
  const step = page.getByTestId("step").filter({ has: page.locator(".name", { hasText: tool }) }).first();
  await expect(step.locator(".io-label")).toHaveText(["Input", "Output"]);
  await page.reload();
  await expect(page.getByRole("button", { name: "Steps" })).toHaveAttribute("aria-pressed", "true");
  await page.goBack();
  await expect(page).not.toHaveURL(/#\/trace\//);
});

test("a turn that failed before replying says so, not what its last tool was asked", async ({ page, request }) => {
  // The root failed and has no output; the last thing a model call said was a request for tools.
  const { traceId } = await traceWhere(request, (spans) => {
    const root = spans.find((span) => span.parent_span_id === null);
    return root?.status_code === 2 && !root.attributes["gen_ai.output.messages"] && spans.some((span) => span.genai.operation === "execute_tool");
  });
  await page.goto(`/#/trace/${traceId}`);
  const summary = page.getByTestId("turn-summary");
  await expect(summary).toContainText("No reply was captured. The last model output called ");
  await expect(summary).not.toContainText("through");
});

test("a missing trace says so and offers another try", async ({ page }) => {
  await page.goto(`/#/trace/${"0".repeat(32)}`);
  await expect(page.getByRole("heading", { name: "This trace is not here" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  await page.getByRole("link", { name: "← All traces" }).click();
  await expect(page.getByTestId("trace-row").first()).toBeVisible();
});

test("a span picked on a wide screen is the one open after the window narrows", async ({ page, request }) => {
  const { traceId } = await traceWhere(request, toolFailedFirst);
  await page.goto("/");
  await page.goto(`/#/trace/${traceId}`);
  const chat = page.getByTestId("span-row").filter({ hasText: "chat" }).first();
  await chat.click();
  const picked = await chat.getAttribute("data-span");
  await page.setViewportSize({ width: 390, height: 844 });
  // The address names the span, so it is open; nothing is left to tap that would do nothing.
  const sheet = page.getByRole("dialog");
  await expect(sheet).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}/${picked}$`));
  await page.getByRole("button", { name: "← Back to trace" }).click();
  await expect(sheet).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}$`));
  // One entry for the trace: Back leaves it.
  await page.goBack();
  await expect(page).not.toHaveURL(/#\/trace\//);
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("the error is on the first screen and a span opens over the page", async ({ page, request }) => {
    const { traceId, spans } = await traceWhere(request, toolFailedFirst);
    const tool = failed(spans)[0]!.genai.tool_name!;
    await page.goto("/");
    await page.goto(`/#/trace/${traceId}`);
    const first = page.getByTestId("first-error");
    await expect(first).toBeVisible();
    await expect(first).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);

    // Steps by default; nothing covers the page until a span is opened.
    await expect(page.getByRole("button", { name: "Steps" })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const step = page.getByTestId("step").filter({ has: page.locator(".name", { hasText: tool }) }).filter({ hasText: "error" }).first();
    await expect(step.locator(".name")).toHaveText(tool); // the name in full, not cut to a letter

    await first.getByRole("button").first().click();
    const sheet = page.getByRole("dialog");
    await expect(sheet).toBeVisible();
    await expect(sheet.getByTestId("span-output")).toBeInViewport();
    await expect(page).toHaveURL(/#\/trace\/[0-9a-f]{32}\/[0-9a-f]{16}$/);

    // Next and previous move between spans without adding history; Back closes the span.
    await sheet.getByRole("button", { name: "Next span" }).click();
    await expect(sheet.getByRole("heading", { level: 2 })).not.toContainText(tool);
    await sheet.getByRole("button", { name: "Previous span" }).click();
    await expect(sheet.getByRole("heading", { level: 2 })).toContainText(tool);
    await page.goBack();
    await expect(sheet).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}$`));

    // The button and Escape close it too, and the focus returns to the span's row.
    await step.getByRole("button").first().click();
    await expect(sheet).toBeVisible();
    await page.getByRole("button", { name: "← Back to trace" }).click();
    await expect(sheet).toHaveCount(0);
    await expect(step.getByRole("button").first()).toBeFocused();
    await step.getByRole("button").first().click();
    await sheet.getByRole("heading", { level: 2 }).click(); // the focus leaves the panel's controls
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}$`));

    // Opening and closing left one entry for the trace.
    await page.goBack();
    await expect(page).not.toHaveURL(/#\/trace\//);
  });

  test("closing after a reload still goes back over the entry the tap added", async ({ page, request }) => {
    const { traceId } = await traceWhere(request, toolFailedFirst);
    await page.goto("/");
    await page.goto(`/#/trace/${traceId}`);
    await page.getByTestId("first-error").getByRole("button").first().click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.reload();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "← Back to trace" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`#/trace/${traceId}$`));
    await page.goBack();
    await expect(page).not.toHaveURL(/#\/trace\//);
  });

  test("a link to a span opens that span; one to a span the trace lacks opens the trace", async ({ page, request }) => {
    const { traceId, spans } = await traceWhere(request, toolFailedFirst);
    await page.goto("/");
    await page.goto(`/#/trace/${traceId}/${spans[1]!.span_id}`);
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "← Back to trace" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByTestId("turn-summary")).toBeVisible();
    await page.goBack();
    await expect(page).not.toHaveURL(/#\/trace\//);

    await page.goto(`/#/trace/${traceId}/${"f".repeat(16)}`);
    await expect(page.getByTestId("turn-summary")).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
});
