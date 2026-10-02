# cronhub Phase 2: registry data repair, reader guard, invariants

- **Date:** 2026-10-02
- **Status:** approved in principle, pending spec review
- **Repo:** `/mnt/data/cronhub`
- **Targets:** `bin/yaml_io.py`, `bin/yaml_io_test_runtime.py`, `registry.yaml`
- **Depends on:** nothing. Independent of the Phase 1 heartbeat fix.

Claims tagged `[F]` (measured 2026-10-02) or `[A]` (inference).

## Correction to an earlier finding

An earlier draft of this spec proposed "fix the write path, then repair the
data", on the theory that `registry.yaml` was being corrupted by ad-hoc text
editing. **That theory is wrong.**

- `[F]` `python3 bin/yaml_io_test_runtime.py` → 126 jobs in, 126 out,
  **0 mismatches**. `yaml_io.write()` round-trips the current file exactly.
- `[F]` `yaml_io.py:41` escapes newlines (`s.replace('\n', '\\n')`), so the
  serializer never emits a raw multi-line value that could split into phantom
  keys.
- `[F]` The `''''` artifacts are present in the stored data itself. Both
  `yaml_io`'s reader and PyYAML parse `"''''Job: X.''''"` to the same
  four-apostrophe string; the serializer preserves them faithfully.

**The serializer is not the problem. The existing round-trip test passes on a
file full of corruption, because phantom keys are parsed as ordinary sub-keys
and written back as ordinary sub-keys.** The test proves the writer is
consistent, not that the data is sane. That gap is the real finding.

## What is actually damaged

`registry.yaml` — 78,363 bytes, 2,340 lines, 126 jobs, parses cleanly.

| # | Defect | Count | Evidence |
|---|---|---|---|
| D1 | Jobs with phantom keys from split multi-line descriptions | 12 | 11 are `obs-*` observers; 1 is `syscron-host-sampler.py` |
| D2 | Lines carrying `''''` quote artifacts | 80 | scan for 3+ consecutive apostrophes |
| D3 | `discord_channel` stored as the string `'None'` instead of null | ≥6 | `repr()` shows `'None'` |
| D4 | Functionally invalid `schedule_expr` | ≥1 | `openclaw-backup` = `''@every 3600000ms''` |
| D5 | Ghost jobs: `enabled: true` with `owner_script: []` | 9 | 6 of them assert `last_status: ok` |
| D6 | `consecutive_errors: 0` on all 126 jobs | 126 | uniformly zero, including jobs reporting `unknown` |
| D7 | `last_status: None` | 89 of 126 | only 23 report `ok` |

D1 is the most serious: **11 of the 12 damaged records are observer jobs** —
the ones that alert when a monitored job goes quiet. Their Discord message
templates are fragmented across invented keys:

```
obs-aed7 opus-degradation-observer
  '(last seen'               = "<age>)''. Do not post anything if the last run is fresh.'"
  'the message tool. Format' = "''''''''⚠️ opus-degradation-monitor did not run on schedule"
```

`syscron-host-sampler.py` carries a raw shell redirect as a YAML value:
`'>/dev/null 2>&1 # cronhub'`.

### D5 disposition (decided)

`[F]` 8 of the 9 ghosts are demonstrably running from system cron via
`~/.openclaw/workspace/scripts/cronhub-wraps/*.wrap.sh`, while the registry
claims ownership via `source: openclaw.cron.json`. Only
`opus-degradation-monitor` has no crontab line.

**Decision: set `enabled: false` on all 9 and append them to
`disabled-jobs.yaml`, preserving the audit entry.** The work continues to run
from cron. The registry stops claiming ownership it does not have. No cron
entry is modified or removed.

## Design

### Part 1 — data repair, as a one-shot transform

The repair is a single script that **reads the damaged dict, transforms it in
memory, and writes it back through `yaml_io.write()`**. Because the serializer
is correct, the output is automatically canonical: no `''''` survives, because
they are stripped from the data before it is ever serialised.

`bin/registry_repair.py`, one-shot, kept for audit. Before running, it copies
`registry.yaml` to `registry.yaml.pre-repair-<UTC timestamp>` — the repo
already keeps 10 rotating `registry.yaml.bak-*` files via
`cronhub-rotate-baks.sh`, but an explicit pre-repair snapshot is taken anyway
so the exact input is recoverable.

**Part 1a — mechanical, safe to automate:**
- Strip leading/trailing runs of ≥2 apostrophes from every string value (D2).
  This is a heuristic; a legitimate value beginning with an apostrophe is
  implausible in this schema, but every changed value is printed so it can be
  eyeballed rather than trusted.
- `discord_channel == 'None'` → `None` (D3).
- Fix `schedule_expr` values that parse to an invalid interval after quote
  stripping, e.g. `''@every 3600000ms''` → `@every 3600000ms` (D4).

**Part 1b — the 12 phantom-key records: reconstruct and review, do not
auto-apply.** For each, fragments under invented keys are rejoined into a
single `description` and the invented keys are dropped. **The reconstructed
templates are printed and approved individually before being written.** These
are alert messages; inventing their wording silently would be worse than
leaving them damaged. If a record's fragments cannot be confidently rejoined,
it is left damaged and reported rather than guessed at.

**Part 1c — the 9 ghosts:** set `enabled: false`, append structured entries to
`disabled-jobs.yaml` with `_meta.reason` recording that the work runs from
system cron under a `cronhub-wraps/*.wrap.sh`, and the crontab line found.

### Part 2 — make the reader loud

`yaml_io.read()` currently **silently skips lines it does not recognise**
(line 283: *"or lines we don't recognise in our schema"*). That is the mechanism
by which this damage stayed invisible for however long it has existed.

- Unrecognised non-blank, non-comment lines raise a `RegistryParseError`
  carrying the line number and content.
- Comments and blank lines continue to be skipped silently.
- The lossy coercion at line 268 — a value of `''` becoming an empty **list** —
  is removed. `''` is a string, not a list declaration. Line 278's
  `parsed_v == "''" → ""` coercion is removed for the same reason.

This is the only behavioural change to `yaml_io.py`. The writer is untouched.

### Part 3 — invariants test

`bin/yaml_io_test_runtime.py` keeps its round-trip check and gains an
invariants check that fails loudly on each of D1–D7:

- no job contains a key outside the canonical schema
- no string value contains a run of ≥2 leading or trailing apostrophes
- no value equals the string `'None'`
- no `schedule_expr` is non-empty and fails to parse as cron or `@every`
- no job has `enabled: true` with an empty `owner_script`
- `consecutive_errors` is not uniformly zero across the whole registry
- every `enabled` job's `last_status` is one of the known statuses, not `None`

D6 and D7 may legitimately fail on first run — they describe the state of the
data, and the repair is not expected to fix every job's status. They are
included so that the *change* is visible, and the spec does not claim they will
pass immediately.

## Verification

1. `python3 bin/yaml_io_test_runtime.py` — round-trip still 0 mismatches after
   the Part 2 reader change.
2. `python3 bin/registry_repair.py --dry-run` — prints every proposed change;
   no file written.
3. Re-run the independent PyYAML analysis (`phase2_recon.py` equivalent) and
   confirm D1–D5 counts drop to zero, and the job count is still **126** —
   no records lost, only repaired.
4. Confirm `disabled-jobs.yaml` gained 9 entries and no crontab line changed:
   `crontab -l` diff must be empty.
5. Confirm the 8 `cronhub-wraps` jobs still appear in `runs/` afterwards —
   disabling the registry entry must not stop the work.
6. Confirm `cronhub-rotate-baks` still finds the file parseable.

## Rollback

- Data: `cp registry.yaml.pre-repair-<ts> registry.yaml`, or
  `git -C /mnt/data/cronhub checkout HEAD -- registry.yaml` (it is tracked).
- Code: `git -C /mnt/data/cronhub checkout HEAD -- bin/yaml_io.py bin/yaml_io_test_runtime.py`.

## Out of scope

- Rewriting `yaml_io.write()` to use PyYAML. It round-trips correctly; the
  "avoids pyyaml dependency" choice is deliberate and not worth churning.
- The 748 `started` records with no terminal status, despite
  `cronhub-record.sh` documenting "terminal only, no separate `started`
  marker". A real inconsistency, but a separate investigation.
- `consecutive_errors` being dead (D6) and `last_status: None` for 89 jobs
  (D7) — these need a writer-side fix, not a data repair.
- Run-history retention. **Not a defect**: `cronhub-rotate-runs` is scheduled
  daily at 06:17 with a 30-day cap. The current 5 days is a floor left by the
  destructive rebuild noted in that script's own header, and it fills in on
  its own.
- Dashboard UI work. `web/dist` is build output with no Svelte source.
