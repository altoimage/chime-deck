/**
 * Query-string surgery for the one-place URL rewrite this app does: dropping a
 * one-shot flag (`?setup=done`) without losing the others (`?kiosk=1`).
 *
 * Boundary: string in, string out. It must never throw before the first paint,
 * so garbage in a hand-typed URL is treated as "no usable params".
 */

/**
 * Remove every value of `name` from `search`, keeping all other params in
 * their original order.
 *
 * @returns the remaining query string, with a leading `?` only when at least
 *   one param survives; `''` when none do, so a caller can assign it straight
 *   to `location.search`.
 */
export function stripParam(search: string, name: string): string {
  const trimmed = search.startsWith('?') ? search.slice(1) : search;
  if (trimmed === '') {
    return '';
  }
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(trimmed);
  } catch {
    return '';
  }
  params.delete(name);
  const rest = params.toString();
  return rest === '' ? '' : `?${rest}`;
}
