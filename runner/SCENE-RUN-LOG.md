# Scene run log — runner/scene.sh against the live Worker + Artifacts

Run: 2026-10-12 ~05:21 UTC, on Oracle (ubuntu@147.224.38.131) via
~/workspace/ssh/osh.sh — git pushes to Artifacts run from Oracle
because the Muse VM's route to Artifacts git hangs; the CLI itself
is machine-agnostic (git runs wherever pp.py is invoked).
Worker: https://gitpp.casey-digennaro.workers.dev · project:
demo-624c3a (fresh random suffix) · wall time: ~44 s.

**Verdict: 26 passed, 0 failed.**

Run history, honestly: the first attempt (project demo-a11701)
scored 23/25. Both failures traced to one cause — Oracle's /tmp
tmpfs is quota-tight, so the scene's temp dir ate "Disk quota
exceeded": the four work files never got written, and one `pp
collect` clone flaked. Fix: scene.sh now pins TMPDIR under $HOME
(PP_TMPDIR to override) and asserts the work files are staged;
pp.py's completion summary now lists the files in each commit, so
the log below shows `work-*.txt` landing in the task repos. The
demo-a11701 repos from attempt 1 remain in the namespace alongside
this run's demo-624c3a set.

---

PASS — work files staged for all four agents
=== git.pp scene replay — project demo-624c3a (worker https://gitpp.casey-digennaro.workers.dev) ===
·· create project repo demo-624c3a
   {
     "created": {
       "defaultBranch": "main",
       "description": "judge replay scene (runner/scene.sh) \u2014 safe to delete",
       "id": "ug78ow4afz2d1xlo",
       "name": "demo-624c3a",
       "remote": "https://049ff5e84ecf636b53b162cbb580aae6.artifacts.cloudflare.net/git/default/demo-624c3a.git"
     }
   }
PASS — create project repo demo-624c3a
·· genesis record lands in the project repo
   completed genesis in demo-624c3a: kind=task parents=[] effects={"project": "demo-624c3a", "status": "open"} files=['record.json'] pushed to main
PASS — genesis record lands in the project repo
·· fork task-a
   {
     "forked": {
       "defaultBranch": "main",
       "description": null,
       "id": "9r0oy0nu9yuoopkm",
       "name": "demo-624c3a--task-a",
       "remote": "https://049ff5e84ecf636b53b162cbb580aae6.artifacts.cloudflare.net/git/default/demo-624c3a--task-a.git"
     },
     "source": "demo-624c3a"
   }
PASS — fork task-a
·· fork task-b
   {
     "forked": {
       "defaultBranch": "main",
       "description": null,
       "id": "a4za1lwtdqq0idno",
       "name": "demo-624c3a--task-b",
       "remote": "https://049ff5e84ecf636b53b162cbb580aae6.artifacts.cloudflare.net/git/default/demo-624c3a--task-b.git"
     },
     "source": "demo-624c3a"
   }
PASS — fork task-b
·· task-a completes: config.timeout=30, feature.cache=on
   completed task-a in demo-624c3a--task-a: kind=task parents=['genesis'] effects={"config.timeout": 30, "feature.cache": "on"} files=['record.json', 'work-a.txt'] pushed to main
PASS — task-a completes: config.timeout=30, feature.cache=on
·· task-b completes: config.timeout=60, feature.search=on
   completed task-b in demo-624c3a--task-b: kind=task parents=['genesis'] effects={"config.timeout": 60, "feature.search": "on"} files=['record.json', 'work-b.txt'] pushed to main
PASS — task-b completes: config.timeout=60, feature.search=on
PASS — witness observation taken (root disk 93% via df)
·· witness-1 committed to the project repo
   completed witness-1 in demo-624c3a: kind=witness parents=['genesis'] effects={"env.disk_pct": 93} files=['record.json'] pushed to main
PASS — witness-1 committed to the project repo
·· override-1 lands: config.timeout=45, with a reason
   completed override-1 in demo-624c3a: kind=override parents=['task-a', 'task-b'] effects={"config.timeout": 45} files=['record.json'] pushed to main
PASS — override-1 lands: config.timeout=45, with a reason
·· fork task-d
   {
     "forked": {
       "defaultBranch": "main",
       "description": null,
       "id": "wc6v30un5hrl61fq",
       "name": "demo-624c3a--task-d",
       "remote": "https://049ff5e84ecf636b53b162cbb580aae6.artifacts.cloudflare.net/git/default/demo-624c3a--task-d.git"
     },
     "source": "demo-624c3a"
   }
PASS — fork task-d
·· fork task-c
   {
     "forked": {
       "defaultBranch": "main",
       "description": null,
       "id": "ltz55b0vuspyyxka",
       "name": "demo-624c3a--task-c",
       "remote": "https://049ff5e84ecf636b53b162cbb580aae6.artifacts.cloudflare.net/git/default/demo-624c3a--task-c.git"
     },
     "source": "demo-624c3a"
   }
PASS — fork task-c
·· task-d completes: config.timeout=99, NOT descended from the override
   completed task-d in demo-624c3a--task-d: kind=task parents=['task-b'] effects={"config.timeout": 99} files=['record.json', 'work-d.txt'] pushed to main
PASS — task-d completes: config.timeout=99, NOT descended from the override
·· task-c completes: descended from override-1
   completed task-c in demo-624c3a--task-c: kind=task parents=['override-1'] effects={"feature.inherited": true} files=['record.json', 'work-c.txt'] pushed to main
PASS — task-c completes: descended from override-1
·· fetching derived state from the Worker
PASS — GET /api/state responds
PASS — override inherited: config.timeout == 45
PASS — conflict winner is override-1
PASS — losers include all three tasks (task-a, task-b, task-d)
PASS — witness key present with the observed value (93)
PASS — task-c completed normally: feature.inherited == true
PASS — both agents' features intact (cache + search on)
PASS — no orphans
PASS — records collected from the repos
determinism shuffle: 7 records x 50 permutations -> 1 distinct derived output(s)
PASS — derive is deterministic under 50 permutations
   ancestor-closure state at task-a: {"config.timeout": 30, "feature.cache": "on", "project": "demo-624c3a", "status": "open"}
   honest checkpoint: PASS
PASS — honest checkpoint at task-a verifies
   ancestor-closure state at task-a: {"config.timeout": 30, "feature.cache": "on", "project": "demo-624c3a", "status": "open"}
   honest checkpoint: PASS
   tamper check: PASS (flipped config.timeout; forged state rejected)
PASS — tampered checkpoint is rejected

=== scene verdict: 26 passed, 0 failed ===

Demo repos left in the namespace as evidence:
  demo-624c3a
  demo-624c3a--task-a
  demo-624c3a--task-b
  demo-624c3a--task-d
  demo-624c3a--task-c
Projection: https://gitpp.casey-digennaro.workers.dev/?project=demo-624c3a
Cleanup (deletes all five demo repos):
  export PP_ADMIN_KEY="$(cat "/home/ubuntu/gitpp-replay/submission/runner/../.admin-key.local")"
  curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" https://gitpp.casey-digennaro.workers.dev/api/repos/demo-624c3a
  curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" https://gitpp.casey-digennaro.workers.dev/api/repos/demo-624c3a--task-a
  curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" https://gitpp.casey-digennaro.workers.dev/api/repos/demo-624c3a--task-b
  curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" https://gitpp.casey-digennaro.workers.dev/api/repos/demo-624c3a--task-d
  curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" https://gitpp.casey-digennaro.workers.dev/api/repos/demo-624c3a--task-c
