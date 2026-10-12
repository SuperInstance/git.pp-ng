# git.pp — build & deployment notes

The working core of the Cloudflare "Next-Gen Git Platform" submission:
a Worker (`gitpp`) over Cloudflare Artifacts. Repos hold the record;
the Worker creates/forks repos through the Artifacts binding, reads
records back through the same binding (no git clone), derives current
state with a deterministic fold, and renders the projection.

**Live:** https://gitpp.casey-digennaro.workers.dev
Try: `/?project=poc-quilt-1` (projection) · `/api/state?project=poc-quilt-1`

## Layout

```
src/fold.ts        the fold (derive + verifyCheckpoint), refereed port
src/worker.ts      the front door (routes below)
conformance/       proof the TS fold == the Python referee (referee/fold.py)
wrangler.toml      deploy shape (reference form; actual deploy via API)
deploy/            the exact upload metadata + multipart spec used
.admin-key.local   the admin secret (mode 600, NEVER committed)
```

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
| `/api/events` | POST | — | manual event ingest to the same KV key (observability) |

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
immediately.

**Freshness model:** the projection is a pure function of the
record, derived fresh on every request. Events are observability and
a future optimisation path — never a correctness dependency. There
is no cache to invalidate.

## Deployment (as performed; reproducible)

1. Bundle: `bun build src/worker.ts --outfile dist/worker.js --format=esm --target=browser`
2. Create KV namespace `GITPP_EVENTS` (id in wrangler.toml).
3. Multipart-upload the script with **all bindings declared in the
   upload metadata from the start** (artifacts + KV):
   a multipart PUT to `/client/v4/accounts/<acct>/workers/scripts/gitpp` with the script body and ALL bindings declared in the metadata (bindings omitted from an upload are wiped), or `wrangler deploy`
   (Never upload without bindings metadata — it wipes non-secret
   bindings. No existing Worker is touched by any of this.)
4. Enable workers.dev: POST `.../scripts/gitpp/subdomain {"enabled":true}`
5. Set the secret LAST: PUT `.../scripts/gitpp/secrets`
   `{"name":"ADMIN_KEY","text":<value>,"type":"secret_text"}`
   (re-set it after any later re-upload).
6. Events: create queue `gitpp-events`; POST consumer
   `{"type":"worker","script_name":"gitpp"}`; POST subscription to
   `/event_subscriptions/subscriptions` (payload: source `artifacts.repo`, event `cf.artifacts.repo.pushed`).

`wrangler.toml` carries the equivalent CLI configuration.

## Conformance (the fold is proven, not trusted)

`referee/fold.py`, the Python reference implementation of the fold,
is the referee. `conformance/harness.py` generates 839 cases — the
property fixtures, a stress suite of 200 random DAGs in
multiple orderings, the live PoC record set, and adversarial edges
(cycles, duplicate ids, unknown kinds, bool/int value collisions,
unicode) — computes expected outputs with the referee at run time,
executes `src/fold.ts` under Node, and diffs derive outputs and 960
checkpoint-verification probes exactly.

Latest run: **839/839 cases identical, 960/960 probes equal.**
Re-run: `cd conformance && python3 harness.py`

## Data notes

Demo data is live on the account (namespace `default`): project
`poc-quilt-1` with task repos `--task-a/b/c/d` and witness repo
`--task-e`. Scratch repos created during verification (`slice-probe*`)
were deleted through the binding. The spike-era repos
(`spike-worker-1*`) predate this build and remain for the final
cleanup pass. witness-2's push also means `/api/state` now derives
8 records for poc-quilt-1 (it gained `env.oracle_load1_x100`).
