import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

/**
 * Unit-test configuration (HSD-o7kp).
 *
 * The Svelte plugin is here for the `.svelte.ts` modules: `$state` is compiled
 * by Svelte, so `stores.svelte.ts` cannot be imported without it. The default
 * environment stays `node` — the suite is offline by contract (the only socket
 * it opens is loopback, see `ws.realsocket.test.ts`). DOM tests opt in per file
 * with a `// @vitest-environment happy-dom` docblock; happy-dom is a local
 * dependency, so no test downloads a browser.
 */
export default defineConfig({
  plugins: [svelte()],
  /*
   * The `svelte` package has a server and a client entry behind the `browser`
   * condition. Without it, a happy-dom test would import the server build and
   * `mount()` would throw `lifecycle_function_unavailable`.
   */
  resolve: {
    conditions: ['browser']
  },
  test: {
    environment: 'node',
    /*
     * The second glob is the one node-script test, `check-dist-stale.test.mjs`:
     * its failure message is acceptance-relevant, so it is asserted rather than
     * checked by hand. The sibling scripts have no tests and are covered only by
     * invocation.
     */
    include: ['src/**/*.test.ts', 'scripts/**/*.test.mjs']
  }
});
