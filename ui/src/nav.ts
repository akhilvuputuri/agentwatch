/** Changing the address from code, for views whose state lives in it. */

/**
 * Change the address without leaving the page, and tell the router at once. The router listens
 * for `hashchange`, which the History API does not send, and which the browser sends only a
 * moment after `location.hash` is set: a control that reads its state from the address (a
 * checked box, a pressed button, an open span) would show the old state until then.
 *
 * `push` adds a history entry, so Back undoes the change; `replace` does not. `state` is the
 * entry's history state, kept as it was unless given.
 */
export function go(hash: string, how: "push" | "replace", state: unknown = window.history.state): void {
  if (how === "push") window.history.pushState(state, "", hash);
  else window.history.replaceState(state, "", hash);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

/** Go to a route as a link would: a new history entry with no state of its own. */
export function navigate(hash: string): void {
  go(hash, "push", null);
}
