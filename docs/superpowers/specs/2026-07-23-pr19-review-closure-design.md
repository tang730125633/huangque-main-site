# PR #19 Review Closure Design

## 1. Purpose

This design closes every unresolved review concern on PR #19 before it is
marked Ready for review. It keeps PR 3-A read-only while making the database
ledger safe for later Phase 3-B writes, centralizing the still-to-voice
handoff decision on the server, aligning the OpenAPI error contract, and
recording real browser acceptance evidence.

The target pull request remains:

- repository: `LU-003/huangque-test-server`
- PR: `#19`
- branch: `codex/short-drama-phase3-voice-spec`
- base: latest `origin/main`

## 2. Scope

### In scope

1. Close reverse-update and identity-integrity gaps in the voice ledger.
2. Add a server-derived still-to-voice handoff blocker model.
3. Make the production frontend consume the server blocker instead of
   reconstructing the decision from one visible job.
4. Align OpenAPI 401/403/404 semantics with runtime anti-enumeration behavior.
5. Create an isolated local acceptance fixture and execute the nine browser
   checks required by the PR description.
6. Rebase, revalidate, update the existing PR, convert it from Draft to Ready,
   and watch GitHub CI.

### Out of scope

- TTS submission or voice generation.
- Point deduction through a new voice endpoint.
- Subtitle or timeline mutation.
- Voice version creation.
- Shot locking or `voice_review -> video_review` progression.
- Server deployment, SSH edits, or production data changes.
- Creating a replacement pull request.

## 3. Design decisions

### 3.1 Complete closure in PR #19

All review concerns are resolved in the existing PR instead of being deferred
to an issue or a second PR. This removes ambiguity about whether the Phase 3-A
foundation is safe to extend and gives reviewers one complete evidence set.

### 3.2 Server is the handoff authority

The frontend must not infer confirmation eligibility from the latest displayed
job. The server calculates a canonical handoff decision from every relevant
production job, charge attempt, refund state, and locked still.

The same decision logic is used by:

- the production read model shown by the frontend; and
- the `confirm_stage` mutation immediately before reconciliation/snapshot/CAS.

The mutation remains authoritative and rechecks inside its transaction.

### 3.3 Ledger identity is immutable after linkage

Voice quote, job, attempt, snapshot, and version identity fields may be set
when a row is created, but cannot later be moved across actors, projects,
shots, lines, quotes, or jobs after another ledger row references them.

The database enforces relational consistency. Login, canvas roles, and whether
an authenticated editor may perform an operation remain service-layer ACL
responsibilities.

### 3.4 Existing databases receive deterministic migrations

Trigger changes use explicit `DROP TRIGGER IF EXISTS` followed by canonical
recreation in `init_db`. Repeated initialization must be idempotent, and a
database containing the PR #19 legacy trigger definitions must be upgraded in
place.

## 4. Voice ledger integrity

### 4.1 Quote invariants

A quote is bound to:

- `username` as the authenticated billing actor;
- `project_id`;
- `voice_line_id`;
- `request_hash` and cost;
- optional consumed idempotency/job identity.

After a charge attempt references `quote_token`, updates must not split the
quote from that attempt's actor, project, or voice line. If both sides carry a
job identity, they must agree.

### 4.2 Job invariants

A voice job remains bound to one actor, project, shot, and voice line.
Changing `job_id` must not orphan a quote or attempt that still references the
old value. Actor/project/shot/line identity becomes immutable once linked.

### 4.3 Charge-attempt invariants

An attempt must match its referenced quote for actor, project, and voice line.
It must match its referenced job for actor, project, shot, line, and job ID.
When the quote has a non-null `consumed_job_id`, the attempt/job identity must
equal it.

### 4.4 Snapshot identity invariants

The following source identity cannot move after snapshot creation:

- voice shot: `shot_id`, `project_id`;
- voice line: `project_id`, `shot_id`, `dialogue_line_id`, `line_type`,
  `sort_order`, `character_key`, and `source_text`.

Editable copies and future generation metadata remain outside this immutable
identity set.

### 4.5 Version invariants

`short_drama_voice_versions.job_id` must resolve to a voice job whose
`voice_line_id` equals the version's `voice_line_id`. INSERT and identity
UPDATE paths receive equivalent checks.

## 5. Canonical handoff blocker model

### 5.1 Response contract

The production read model adds:

```json
{
  "handoff_blocked": true,
  "handoff_blockers": [
    {
      "code": "active_job",
      "shot_id": "shot-1",
      "message": "Still generation is still running"
    }
  ]
}
```

`handoff_blockers` is deterministic, contains no secret/internal payload, and
is ordered by blocker type and shot order so repeated reads are stable.

### 5.2 Blocker codes

The initial closed set is:

- `missing_locked_still` — a current shot has no locked valid still;
- `active_job` — any associated still job is `pending` or `running`, including
  an older job hidden by a newer terminal job;
- `refund_pending` — a failed/malformed result or charge requires refund
  settlement;
- `charge_attempt_pending` — an attempt remains `accepted`, `charged`, or
  `refund_pending`;
- `ledger_inconsistent` — an association required for safe reconciliation is
  missing or invalid.

The API may include multiple blockers. User-facing messages are display hints;
frontend logic keys only on `handoff_blocked` and `code`.

### 5.3 Transaction behavior

`confirm_stage` performs, under the existing paid-submission lock and
`BEGIN IMMEDIATE` transaction:

1. authentication, ownership/editor access, stage, and revision checks;
2. reconciliation of terminal Phase 2 jobs;
3. canonical blocker calculation;
4. durable refund-intent commit-and-reject when reconciliation requires it;
5. rejection when any blocker remains;
6. voice snapshot creation;
7. stage/revision compare-and-swap;
8. commit.

Unexpected failures roll back reconciliation, snapshot creation, and stage
change together. Idempotent replay and single-winner confirmation behavior are
preserved.

### 5.4 Frontend behavior

The confirmation button is disabled whenever `handoff_blocked` is true.
The inspector renders escaped blocker messages. The frontend does not derive
eligibility from `shot.job` or the most recent job ID. A stale tab may still
submit, but the server recheck returns a domain conflict without mutation.

## 6. HTTP and OpenAPI semantics

The voice read endpoint uses these externally observable errors:

- `400`: missing or malformed request parameters;
- `401`: missing/invalid authentication;
- `403`: account-level restriction, including mandatory initial-password
  change or absence of the base canvas capability;
- `404`: project does not exist or the authenticated user is not allowed to
  discover/read it.

Project ACL failures deliberately share the 404 response with missing
projects. OpenAPI descriptions, examples, and contract tests must not claim
that a project-level authorization failure returns 403.

The OpenAPI voice response retains explicit required shot and line fields,
including `timeline_revision`, immutable source fields, editable copies,
settings, timing, versions, and current-job data.

## 7. Browser acceptance fixture

### 7.1 Isolation

Acceptance uses a temporary local database and generated fixture data outside
tracked paths. It must not read or modify production/test-server databases.

The fixture contains:

- one six-shot project in `voice_review`;
- normal dialogue, a narrator whose display name is not literally “旁白”, and
  at least one silent shot;
- stable voice-line IDs and default voice settings;
- an owner, an authorized viewer, and an unauthorized user;
- a canvas/board access relationship for the owner and viewer.

The fixture may be generated by a test helper, but database/media outputs are
never committed.

### 7.2 Nine checks

The PR records project ID, account roles, result, and evidence for:

1. the voice workspace replaces the still workspace;
2. six shots appear in storyboard order;
3. dialogue, character, voice key, speed, pitch, and volume match the snapshot;
4. narrator displays an explicit narration badge;
5. silent shots display the silent state;
6. generate/save/lock/advance controls are absent or disabled;
7. refresh preserves voice-line IDs and snapshot text;
8. viewer can read but receives no write controls;
9. unauthorized user cannot discover or read the project.

Any failed check blocks Ready status until fixed and revalidated.

## 8. Testing strategy

### Backend

- legacy-trigger migration and repeated initialization;
- editor actor positive path;
- quote/job/attempt actor and project mismatch INSERT/UPDATE rejection;
- reverse-reference UPDATE rejection;
- snapshot identity immutability;
- version-to-job/line consistency;
- old-running/new-done job blocker;
- refund and unresolved attempt blockers;
- reconciliation, rollback, concurrent confirmation, and idempotent replay.

### Frontend

- server blocker disables confirmation regardless of visible latest job;
- escaped blocker messages;
- no confirmation POST while blocked;
- voice narration, silent, viewer, escaping, and lifecycle regressions.

### Contract and repository gates

- OpenAPI voice path, parameters, required fields, and 400/401/403/404 meaning;
- asset stamp update/check;
- `ci_validate.py`;
- Python compileall and relevant Node syntax checks;
- targeted suites followed by the full repository test suite;
- final diff, scope, temporary-file, and secret scans.

## 9. PR workflow

PR #19 follows `本地修改与PR提交优化流程.docx`:

1. fetch and rebase latest `origin/main`;
2. rerun stamp, base gates, compile, syntax, relevant and full tests;
3. inspect `origin/main...HEAD` file list and verify task-only scope;
4. update the existing remote branch with `--force-with-lease` after rebase;
5. update the PR body with exact commands, counts, fixture project ID, roles,
   and all nine results;
6. convert the existing PR from Draft to Ready for review;
7. watch GitHub CI to completion;
8. fix only concrete CI/review failures in the same branch and PR.

No merge or deployment is performed by this workflow.

## 10. Completion criteria

PR #19 may be marked Ready only when:

- all ledger and handoff invariants above have regression tests;
- OpenAPI and runtime error semantics match;
- all nine browser checks pass and are recorded in the PR;
- the branch is rebased on latest `origin/main`;
- cache stamps, validation, compilation, syntax, targeted tests, full tests,
  final diff, and sensitive-file checks pass;
- the PR body contains the exact scope, verification, acceptance fixture, and
  non-goals.

It may be considered mergeable only after GitHub CI is green and review has no
open Critical or Important findings. Release remains a separate decision and
is not performed by PR #19.

## 11. Risks and mitigations

- **Trigger migration locks or breaks an existing database:** keep migration
  idempotent, cover legacy definitions, and perform it inside initialization.
- **Frontend and server blocker logic diverge:** expose one server-derived
  blocker contract and keep mutation-side recheck authoritative.
- **Refund reconciliation is committed but stage confirmation fails:** commit
  only the durable refund intent path intentionally; all ordinary failures
  roll back.
- **Acceptance fixture leaks data:** use temporary paths and synthetic users,
  and scan the final diff for databases, cookies, tokens, and generated media.
- **Rebase changes validated behavior:** run the complete gate sequence again
  after rebase and before Ready status.
