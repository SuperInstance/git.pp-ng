# git.pp — build & deployment notes

The working core of the Cloudflare "Next-Gen Git Platform" submission:
a Worker (`gitpp`) over Cloudflare Artifacts. Repos hold the record;
the Worker creates/forks repos through the Artifacts binding, reads
records back through the same binding (no git clone), derives current
state with a deterministic fold, and renders the projection.

**Live:** https://gitpp.superinstance.dev
Try: `/?project=poc-quilt-1` (projection) · `/api/state?project=poc-quilt-1`

## Layout

```
src/fold.ts        the fold (derive + verifyCheckpoint), refereed port
src/worker.ts      the front door (routes below)
conformance/       proof the TS fold == the Python referee (referee/fold.py)
wrangler.toml      deploy shape (reference form; actual deploy via API)
.admin-key.local   the admin secret (mode 600, NEVER committed)
```

The upload metadata and multipart spec used by the Deployment section
are printed inline there — save them as `deploy/metadata.json` and
`deploy/spec.json` locally; they are operator files, not source.

## Concepts in one paragraph

A project is an Artifacts repo. Its history is a record of task
completions: each commit carries one `record.json`
`{id, parents, kind: task|witness|override, effects, reason?}`.
Agents work in repos forked from the project (`<project>--<task>`);
a task repo's HEAD `record.json` is that task's completion record.
State is not stored — it is *derived*: the fold orders records by
ancestry, lets the strongest class win contested keys
(override > task > witness, absolutely), breaks remaining ties by
ancestry cleanliness (most intent, fewest witnesses, smallest id),
and preserves every loser in the conflict report. A captain's
override, once in the record, governs all later derivation on
branches that never saw it — inheritance by construction.

## Routes

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | — | HTML projection for `?project=` (default `poc-quilt-1`) |
| `/api/state` | GET | — | derive output JSON for `?project=` |
| `/api/repos` | GET | — | all repos in the namespace (cursor-paged) |
| `/api/repos` | POST | admin | create repo `{name}` (initial token stripped from response) |
| `/api/repos/<name>/fork` | POST | admin | fork repo to `{name}` (token stripped) |
| `/api/repos/<name>/token` | POST | admin | mint repo token `{scope?, ttl?}` → `{plaintext, expiresAt}` |
| `/api/repos/<name>` | DELETE | admin | delete repo |
| `/api/debug/log?repo=` | GET | admin | raw binding `log()` passthrough |
| `/api/last-event` | GET | admin | latest push-event envelope (KV `EVENTS`, key `last-push`) |

There is no event-ingest route: the only writer of the KV `last-push`
mirror is the Queue consumer, fed by Artifacts event subscriptions.
(A public `POST /api/events` existed in the demo build and was removed
in the production pass — finding F2.)

Admin auth: header `X-Admin-Key` must equal the `ADMIN_KEY` Worker
secret. Fails closed (401) when the secret is unset or the header is
wrong. Read routes are public by design: the projection is the demo's
face; nothing mutates without the key, and create/fork responses
never carry repo tokens.

## Push events

Artifacts emits `cf.artifacts.repo.pushed` (and lifecycle events)
through Cloudflare's event-subscriptions framework. Subscriptions
deliver to a **Queue**; this Worker consumes queue `gitpp-events`
and records the latest envelope in KV. Wiring (ids in wrangler.toml):
queue + consumer + one subscription
(`artifacts.repo` source for `poc-quilt-1--task-e`, `events:["pushed"]`)
were all created via the API and proven live: a witness record pushed
from a fleet machine appeared in KV ~5 s later, and in `/api/state`
immediately. Step 6 of the Deployment section is the exact recipe.

**Freshness model:** the projection is a pure function of the
record, derived fresh on every request. Events are observability and
a future optimisation path — never a correctness dependency. There
is no cache to invalidate.

## Authentication (read this first)

Every step of the deployment is a Cloudflare API call, and the cold
operator's first wall is auth, so it comes before the steps. Two
routes exist; the deployment below uses the first.

1. **API token (the canonical route).** Create a token in the
   Cloudflare dashboard (My Profile → API Tokens → Create Token)
   scoped to the account you are deploying to, with account-level
   **Workers Scripts: Edit**, **Workers KV Storage: Edit**, and
   **Queues: Edit**. Send it on every call as the header
   `Authorization: Bearer <token>`. The commands below are plain
   `curl` against `https://api.cloudflare.com/client/v4` with the
   header `-H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"`.
   (This project's fleet also drives the same calls through a
   `cf_api.py` shorthand that attaches a stored credential; it is
   not a public tool, and nothing below depends on it.)
2. **wrangler.** `wrangler login` (OAuth in a browser) or the
   `CLOUDFLARE_API_TOKEN` environment variable. Only the appendix
   path needs it.

Two things that bite:

- **The account id is a variable, not a constant.** Every path below
  contains `$CF_ACCOUNT_ID` — set it to your own account's id, found
  in the dashboard (Workers & Pages → Overview, "Account ID" in the
  right-hand sidebar), via `curl -s
  https://api.cloudflare.com/client/v4/accounts -H "Authorization:
  Bearer $CLOUDFLARE_API_TOKEN"` (the `id` field of your account),
  or via `wrangler whoami`.
- **No credentials at all does not say "unauthorized" — it says
  9106/9107.** An unauthenticated API call returns errors
  `9106 Missing X-Auth-Email header` and
  `9107 Missing X-Auth-Key header`. Those headers belong to the
  legacy Global API Key route; a Bearer token replaces both. If you
  see 9106/9107, your token header never left the machine.

## Deployment (the canonical path — Cloudflare API, as performed)

Set `$CF_ACCOUNT_ID` first (see Authentication). Requires a Workers
Paid account (Artifacts is not on the free plan) and `bun` for the
bundle.

1. Bundle:
   `bun build src/worker.ts --outfile dist/worker.js --format=esm --target=browser`
2. Create the KV namespace the event mirror lives in:
   ```sh
   curl -s -X POST https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/storage/kv/namespaces \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"title":"GITPP_EVENTS"}'
   ```
   Take `result.id` from the response — it is `<kv-id>` in step 3 and
   the `id` in `wrangler.toml`'s `[[kv_namespaces]]` block.
3. Multipart-upload the script with **all bindings declared in the
   upload metadata from the start** (bindings omitted from an upload
   are wiped; only `secret_text` bindings survive one). Write
   `deploy/metadata.json`:
   ```json
   {"main_module":"worker.js","compatibility_date":"2025-09-01","bindings":[{"type":"artifacts","name":"ARTIFACTS","namespace":"default"},{"type":"kv_namespace","name":"EVENTS","namespace_id":"<kv-id>"}]}
   ```
   and `deploy/spec.json` (paths are relative to where you run the
   command):
   ```json
   {"files":{"metadata":{"path":"deploy/metadata.json","ctype":"application/json"},"worker.js":{"path":"dist/worker.js","ctype":"application/javascript+module","filename":"worker.js"}}}
   ```
   then upload (the `deploy/spec.json` above is the same multipart
   body in file form, for operators driving it with a helper):
   ```sh
   curl -s -X PUT https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/workers/scripts/gitpp \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     -F "metadata=@deploy/metadata.json;type=application/json" \
     -F "worker.js=@dist/worker.js;type=application/javascript+module"
   ```
4. Enable workers.dev:
   ```sh
   curl -s -X POST https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/workers/scripts/gitpp/subdomain \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"enabled":true}'
   ```
5. Set the secret LAST, and re-set it after any later re-upload. The
   payload is a **single JSON object** — the array form `[{...}]` is
   rejected by this endpoint (learned on the production deploy).
   Write it to a mode-600 file so the value stays out of shell
   history:
   ```json
   {"name":"ADMIN_KEY","text":"<value>","type":"secret_text"}
   ```
   ```sh
   curl -s -X PUT https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/workers/scripts/gitpp/secrets \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
     --data @secret.json
   ```
6. Events — create the queue, register this Worker as its consumer,
   then subscribe:
   ```sh
   curl -s -X POST https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/queues \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"name":"gitpp-events"}'
   ```
   → take `result.queue_id` as `<queue-id>`.
   ```sh
   curl -s -X POST https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/queues/<queue-id>/consumers \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"type":"worker","script_name":"gitpp"}'
   curl -s -X POST https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/event_subscriptions/subscriptions \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
     -d '<body>'
   ```
   with body (one subscription per repo whose pushes you want
   mirrored):
   ```json
   {"name":"gitpp-<repo>-pushes","enabled":true,"source":{"type":"artifacts.repo","namespace":"default","repo_name":"<repo>"},"destination":{"type":"queues.queue","queue_id":"<queue-id>"},"events":["pushed"]}
   ```
   Two naming lessons, learned from API validation errors: the event
   name in `events` is `pushed` — `cf.artifacts.repo.pushed` is the
   envelope type that arrives, not what you subscribe with — and
   source properties are snake_case.

## Appendix — the wrangler equivalent (not the path used in production)

`wrangler.toml` carries the equivalent CLI configuration, for an
operator who prefers the CLI:

- KV: `wrangler kv namespace create GITPP_EVENTS` — current syntax
  is space-separated; the v1 colon form (`wrangler kv:namespace
  create`) no longer exists. Put the printed id in `wrangler.toml`.
- Queue: `wrangler queues create gitpp-events`; the consumer is the
  `[[queues.consumers]]` block already in `wrangler.toml`.
- Deploy: `wrangler deploy` (its `main` already points at the
  step-1 bundle, `dist/worker.js`).
- Secret: `wrangler secret put ADMIN_KEY`.
- The event subscription has no wrangler form — create it with the
  step-6 API call whichever deploy path you took.

### Delta reads (checkpoint + tail)

The checkpoint+tail read path (`docs/DELTA-READS.md`) adds **no routes and no
binding changes** — deploy the Worker exactly as above (re-set `ADMIN_KEY`
after any re-upload). Then create each project's checkpoint so `/api/state`
reads only the tail:

```
pp.py checkpoint poc-quilt-1
pp.py checkpoint poc-scale-1
pp.py checkpoint demo-race-1
```

`pp.py checkpoint` is a gated write (it mints a project-repo token and pushes
`checkpoint.json`); there is no unauthenticated creation route. Verify after:
`curl -sD- /api/state?project=poc-quilt-1` shows `x-gitpp-derivation: delta`
and a body byte-identical to the frozen golden; a project with no checkpoint
still shows `full`.

## Conformance (the fold is proven, not trusted)

`referee/fold.py`, the Python reference implementation of the fold,
is the referee. `conformance/harness.py` generates 863 cases — the
property fixtures, a stress suite of 200 random DAGs in
multiple orderings, the live PoC record set, and adversarial edges
(cycles, duplicate ids, unknown kinds, bool/int value collisions,
unicode) — computes expected outputs with the referee at run time,
executes `src/fold.ts` under Node, and diffs derive outputs and 1,029
checkpoint-verification probes exactly.

Latest run (2026-10-14): **863/863 cases identical, 1,029/1,029 probes equal** — plus 3,231 witness-freshness checks and 16,290 closure-root digests, all matching.
Re-run: `cd conformance && python3 harness.py`

## Data notes

Demo data is live on the account (namespace `default`): project
`poc-quilt-1` with task repos `--task-a/b/c/d` and witness repo
`--task-e`. Scratch repos created during verification (`slice-probe*`)
were deleted through the binding. The spike-era repos
(`spike-worker-1*`) predate this build and remain for the final
cleanup pass. witness-2's push also means `/api/state` now derives
8 records for poc-quilt-1 (it gained `env.oracle_load1_x100`).
