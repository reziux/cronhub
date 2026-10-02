# cronhub: openclaw heartbeat correctness and automatic failback

- **Date:** 2026-10-02
- **Status:** approved, pending implementation
- **Component:** `/mnt/data/cronhub/bin/openclaw-heartbeat.sh` (42 lines)
- **Repo:** `/mnt/data/cronhub` (git-tracked, pushed hourly to `reziux/cronhub`)

Claims are tagged `[F]` fact, measured on 2026-10-02, or `[A]` inference.

## Problem

openclaw is the intended primary scheduler; hermes is standby. openclaw has
not been primary since 09:17 on 2026-10-02.

- `[F]` `state/openclaw.heartbeat` mtime frozen at `2026-10-02 09:17`.
- `[F]` `state/hermes.heartbeat` refreshed every minute (verified live).
- `[F]` `state/arbiter.log` contains seven consecutive
  `FLIP openclaw -> hermes (openclaw-stale)` entries.
- `[F]` `systemctl --user is-active` reports **both** `openclaw-gateway` and
  `hermes-gateway` as `active`.
- `[F]` The cron entry exists and runs every minute:
  `* * * * * bash .../cronhub-record.sh crh-openclaw-heartbeat -- bash .../openclaw-heartbeat.sh >/dev/null 2>&1`
- `[F]` `~/.openclaw/gateway.pid` records PID `3476375`; the live gateway is
  PID `3569044`.

So this is not a dead process. It is a dead signal.

## Root cause

`openclaw-heartbeat.sh` only touches the heartbeat inside
`if [ -n "$PID" ]`. `PID` is resolved by:

1. `pgrep -f '^openclaw-gateway$'` — an **anchored exact** match against the
   entire command line.
2. Fallback `ps -eo pid,comm,args | awk '$2 ~ /openclaw/ && /gateway/'` —
   tests `$2` (comm), not the args.

`[F]` The real process is:

```
3569044 node-MainThread /home/linuxbrew/.linuxbrew/opt/node/bin/node \
  --max-old-space-size=1500 /home/reziux/.local/lib/node_modules/openclaw/dist/index.js \
  gateway --port 18789
```

- Path 1 never matches: the command line is a `node ... index.js gateway`
  invocation, not the literal string `openclaw-gateway`.
- Path 2 never matches: `comm` is `node-MainThread`. The substring `openclaw`
  appears in the *args*, not in `$2`.

`[F]` Reproduced: running `bash openclaw-heartbeat.sh` by hand exits cleanly,
prints nothing, and leaves `openclaw.heartbeat` at `09:17`. The script
succeeds at doing nothing.

`[F]` The pidfile is frozen at the same failure point — it is only rewritten
when a PID is found, so it still points at a dead instance from before the
last gateway restart changed its command line.

`[A]` The trigger was resource exhaustion, not the script itself.
`state/arbiter.log` contains repeated
`OSError: [Errno 28] No space left on device` from `Path.write_text`.
`/mnt/data` had reached 100% and has since recovered to 11G free. A full card
prevented heartbeat writes; the missing writes made openclaw look stale.

### Why hermes always wins

`[F]` The arbiter auto-heartbeats hermes itself, and its election rule is
"if openclaw heartbeat fresh → openclaw; elif hermes fresh → hermes". hermes
is therefore unconditionally fresh, and openclaw's freshness depends entirely
on the broken external script.

**The arbiter is not at fault and requires no change.** Its rule is
stateless and prefers openclaw; the moment the heartbeat moves, the next
tick elects openclaw again. There is no latch to clear.

## Design

### Resolution order

1. `systemctl --user show openclaw-gateway --property=MainPID --value`,
   after exporting:
   - `XDG_RUNTIME_DIR=/run/user/$(id -u)`
   - `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus`
2. If the systemctl path is unreachable, fall back to the existing `pgrep`
   pattern, and record that the degraded path was used.
3. "Could not determine" is never collapsed into "gateway is down".

### The D-Bus bootstrap is mandatory, not optional

`[F]` Measured:

| Environment | Result |
|---|---|
| Normal shell | `3569044` |
| `env -i` (as cron sees it) | **empty** — `Failed to connect to user scope bus via local transport` |
| `env -i` + `XDG_RUNTIME_DIR` + `DBUS_SESSION_BUS_ADDRESS` | `3569044` |

The cron entry discards all output (`>/dev/null 2>&1`). Without the D-Bus
bootstrap the script would fail silently — reproducing the current symptom
with strictly worse diagnosability.

### States

Written to `state/openclaw.heartbeat.status` next to the existing touch:

| State | Condition | Touch? | Exit |
|---|---|---|---|
| `ok <pid>` | MainPID > 0 | yes | 0 |
| `down` | MainPID == 0, gateway genuinely stopped | no | 0 |
| `unknown <reason>` | systemctl unreachable, PID unresolvable, or `touch` failed | no | non-zero |

Only `ok` touches `openclaw.heartbeat`. The arbiter's existing contract is
preserved exactly: it reads heartbeat freshness and nothing else.

`down` and `unknown` are deliberately distinct. Conflating them is precisely
how a full card turned into an invisible scheduler failover.

### Fail loud

A failed `touch` writes `status=unknown touch-failed` and exits non-zero.
Today that failure is discarded by the cron redirect, which is the second
half of why the ENOSPC incident went unseen.

### Explicitly out of scope

- `bin/arbiter.sh` — no change. Its preference for openclaw is already correct.
- A disk-space guard. Declined; recorded below as remaining risk.
- Distinguishing measurement failure from death *inside the arbiter*. The
  three-state sidecar surfaces the distinction to the dashboard; the
  arbiter keeps its current freshness-only contract.

## Verification

Each step is executable, not an assertion.

1. `bash -n bin/openclaw-heartbeat.sh` — syntax clean.
2. Run by hand: `openclaw.heartbeat` mtime advances to now; status reads
   `ok 3569044`.
3. `~/.openclaw/gateway.pid` self-heals from `3476375` to `3569044`.
4. Cron-environment proof: `env -i bash bin/openclaw-heartbeat.sh` still
   yields `ok <pid>`. This is the check that would have caught the naive
   version of this fix.
5. Negative path: run with the unit name overridden to a nonexistent unit;
   expect `down`, no touch, exit 0.
6. After one arbiter tick: `claims/active_scheduler` returns to `openclaw`,
   `last_flip_reason` records the handover.
7. Next hourly cron run reports `cronhub: clean` with the heartbeat
   demonstrably moving.

## Rollback

`git -C /mnt/data/cronhub checkout HEAD -- bin/openclaw-heartbeat.sh`

The repo commits and pushes hourly, so a bad version does not linger.

## Known remaining risk (accepted, not fixed)

- `/etc/cron.d/cronhub` does not exist. `install_cronhub_arbiter.py` defaults
  to dry-run and needs root, so the arbiter's system-cron entry was never
  installed. The arbiter currently runs via some other path; this spec does
  not establish which.
- No disk-space guard. A full card can still cause a silent failover. The
  fail-loud change makes it *visible*; it does not prevent it.
- `openclaw/heartbeat` remains 0 bytes by design — it is a timestamp file
  read by mtime, never by content.

## Phase 2 — deliberately deferred

Different subsystem, different risk profile. These do not ride in on a
heartbeat patch and warrant their own spec:

- `[F]` Run history is 5 days deep (`runs/2026-09-28.jsonl` →
  `2026-10-02.jsonl`). No trend or seasonality analysis is possible.
- `[F]` A ghost job in `disabled-jobs.yaml` is `enabled: true`,
  `owner_script: []`, `log_file: None`, a duplicate of an active job — and
  still carries `last_status: ok`. A job that cannot run asserts success.
- `[F]` `registry.yaml` contains corrupted quoting:
  `schedule_expr: '''''*/30 * * * *'''''`.
- `[F]` The dashboard UI has no Svelte source, no `package.json`, no build
  config. `web/dist` is build output with manual patches that `npm run build`
  would destroy. UI work means patching artifacts, not editing components.
