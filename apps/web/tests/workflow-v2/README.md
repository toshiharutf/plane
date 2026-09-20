# Local delivery UI checks

These fixtures import the production progress, evidence and decision components. They replace services in the test bundle only; they are not product routes and never contact a Plane instance or VPS.

From the fork root, after workspace dependencies and shared packages have been built:

```bash
TSX_TSCONFIG_PATH=apps/web/tsconfig.json node --import ./packages/i18n/node_modules/tsx/dist/loader.mjs apps/web/tests/workflow-v2/presentation.test.tsx
apps/web/node_modules/.bin/vite build --config apps/web/tests/workflow-v2/vite.config.mjs
python3 -m http.server 4317 --bind 127.0.0.1 --directory /tmp/plane-delivery-fixture-build
```

In separate terminals, start an isolated browser and run the acceptance checks:

```bash
google-chrome --headless=new --no-sandbox --disable-gpu --disable-background-networking --disable-component-update --no-first-run --remote-debugging-port=9317 --user-data-dir=/tmp/plane-c4-c5-ui-browser about:blank
node apps/web/tests/workflow-v2/browser.mjs
```

The `--no-sandbox` flag is needed only when the local container cannot launch Chrome's own sandbox. Use a sandboxed browser when supported. The browser talks only to the local fixture. Stop both processes after testing.

Presentation checks cover unknown and zero amounts, empty scope, distinct membership, revoked/expired/closed decisions, exact payload display, terminal and code-defect retry exclusion, saved tab preferences and the actual projection wire shape. Browser checks cover contributor inspection, Escape/focus-managed drawer, scope switching, explicit approval and expiry, multiline input, stale projections, concurrent-version refusal and mobile overflow. Backend tests remain responsible for enforcing authority and transition guards; these fixtures do not prove backend authorization.

The final integration checks additionally exercise two simultaneous typed work questions: answering one retains Awaiting Human, withdrawing the requester's obsolete second question records evidence and returns Todo with the original reviewer continuation. Presentation checks also ensure manual verification is available only for active human work with the matching next action. Cycle budget views use the server's deduplicated union of all approved cycle scopes and show each scope envelope separately.
