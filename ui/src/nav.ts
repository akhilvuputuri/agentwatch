/** Moving between routes from code, for views whose controls are driven by the address. */

const listeners = new Set<() => void>();

/** Calls `listener` whenever `navigate` moves; returns how to stop. */
export function onNavigate(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Go to a route, adding a history entry. The browser reports a hash change only a moment later,
 * and a control that reads its state from the address (a checked box, a pressed button) would
 * show the old state until then, so the router is told at once.
 */
export function navigate(hash: string): void {
  window.location.hash = hash;
  for (const listener of listeners) listener();
}
