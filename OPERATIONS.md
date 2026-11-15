# Production operations

The project includes deployment tooling and systemd templates. Installation,
activation, upgrades, backups, and restores change host state and must follow an
approved maintenance procedure. The production installation uses a dedicated
`wow-armory` account, an
application release at `/opt/wow-forever-armory-bot/current`, and private state
under `/var/lib/wow-forever-armory-bot`.

## Installation preparation

For a new host or a separately approved reinstall, create a dedicated unprivileged
`wow-armory` user and group, install a reviewed release and its virtual
environment under `/opt/wow-forever-armory-bot`, and make release files
read-only to that account. The account should write only its state directory.
Create `/etc/wow-forever-armory-bot/config` with mode `0600` and owner
`root:root`; it contains only non-secret configuration, for example:

```dotenv
ARMORY_LOG_LEVEL=INFO
ARMORY_LOG_FORMAT=text
ARMORY_BACKUP_RETENTION=14
```

`ARMORY_ENVIRONMENT=development` and optional `DISCORD_TEST_GUILD_ID` are for
local development only; omit both from the production EnvironmentFile.

Place the token in `/etc/wow-forever-armory-bot/discord-token`, also mode
`0600` and root-owned. systemd passes it as a credential file. The stable
launcher at `/opt/wow-forever-armory-bot/bin/armory-launch` exports only the
credential file path as `DISCORD_BOT_TOKEN_FILE`; the application reads the
credential itself, so the value is not copied into the process environment.
This requires a release supporting `DISCORD_BOT_TOKEN_FILE`; earlier code that
does not support `DISCORD_BOT_TOKEN_FILE` is not a safe rollback target for
this launcher. The approved v0.2.0 base already supports the credential-file
interface.
Never put the token in unit arguments or logs. Install the service and timer templates only after
reviewing paths and account permissions. Run `systemd-analyze verify` on copies
in a staging environment. Do not enable either unit until a separate
production approval.

Future installation and activation commands below change server configuration;
they are documented only and were not run:

```bash
# Configuration-changing: install reviewed templates and reload systemd.
sudo install -o root -g root -m 0644 deploy/systemd/wow-forever-armory-bot.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/wow-forever-armory-backup.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/wow-forever-armory-backup.timer /etc/systemd/system/
sudo install -o root -g root -m 0755 deploy/systemd/armory-launch /opt/wow-forever-armory-bot/bin/armory-launch
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/wow-forever-armory-bot.service /etc/systemd/system/wow-forever-armory-backup.service /etc/systemd/system/wow-forever-armory-backup.timer

# Activating: requires separate production approval.
sudo systemctl enable --now wow-forever-armory-bot.service
sudo systemctl enable --now wow-forever-armory-backup.timer
```

### Code-only upgrade of an installed release

For a reviewed code-only update, stop and disable both the Armory service
and backup timer during an approved maintenance window. Verify that both
are inactive and disabled before invoking the installer. The installer
supports `--upgrade-existing`: it validates the
existing config and credential in place, requires the current link to point to
a complete managed release, requires the new commit to descend from that
release, and atomically switches `current`. It does not invoke the installed
startup database preflight against production state. Instead,
`production_db_snapshot.py` copies the database and relevant WAL through
read-only, no-follow descriptors into a private temporary directory and runs
SQLite integrity/schema checks only on that copy. It validates source metadata
before and after copying and fails closed if the source changes. The installer
does not migrate or write to the production database or its sidecars, and does
not rewrite the config, credential, systemd units, launcher, state directory,
or backup directory. The unit templates and launcher must be byte-identical to
the installed versions; runtime configuration changes need a separately
reviewed installation.

Production was restored to working release
`dcceeb85464e09e34b112e41b16a833226c72270` after the v1.1.4 candidate
`a1a6a2ad95302f7e3c4900f18e91691f43631e18` was rejected by the installer.
The v1.1.3 history includes commit
`c99e21f9afc6b9e4d5df0a8668a31f9ab9031dce`, but its production deployment
was safely blocked because snapshot verification still rejected zero guilds.
The v2.0.0 candidate is based on the complete v1.1.4 source at `a1a6a2a…`.
Any future installation requires a separately reviewed exact release pin and
approval.

Before installation, record the installed shared preflight's SHA-256 and
preserve a root-owned copy for recovery planning. The installer atomically
replaces `/opt/wow-forever-armory-bot/bin/armory-db-preflight` before switching
`current`; if installation later fails, the old application release can remain
active with the new zero-guild-compatible preflight. Keep the original backup
and its recorded hash. Do not alter other services or bots.

```bash
sudo sha256sum /opt/wow-forever-armory-bot/bin/armory-db-preflight
sudo install -o root -g root -m 0700 \
  /opt/wow-forever-armory-bot/bin/armory-db-preflight \
  /var/lib/wow-forever-armory-bot/backups/armory-db-preflight.v1.1.2
sudo sha256sum \
  /opt/wow-forever-armory-bot/bin/armory-db-preflight \
  /var/lib/wow-forever-armory-bot/backups/armory-db-preflight.v1.1.2
```

Application rollback and preflight rollback are separate operations. When
rolling the application pointer back to v1.1.2, retain the zero-guild-compatible
preflight. The v1.1.2 preflight rejects an empty guild table, so **never restore
the saved v1.1.2 preflight while the production database has zero guilds**.
The compatible preflight shipped from published base commit
`c99e21f9afc6b9e4d5df0a8668a31f9ab9031dce` has SHA-256
`c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803`. Verify
the installed script against that exact digest before starting either the
current or rolled-back application:

```bash
printf '%s  %s\n' \
  'c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803' \
  '/opt/wow-forever-armory-bot/bin/armory-db-preflight' \
  | sudo sha256sum --check -
```

If this verification fails or a compatible preflight cannot be retained, keep
the service stopped. Restore the saved v1.1.2 preflight only after the database
has been independently verified to contain at least one real guild row and the
operator has separately approved that preflight rollback.

```bash
# Configuration-changing: stages code and atomically switches the release link.
sudo /usr/bin/python3.14 deploy/production_install.py \
  --release-sha FULL_REVIEWED_RELEASE_SHA \
  --upgrade-existing
```

The release remains stopped and disabled. Before any approved start, verify
`current`, release marker, systemd units, database integrity, and backup
availability. Both the startup preflight and isolated installer snapshot
verification accept a correctly initialized database with zero guild rows,
members, and characters. They continue to reject an absent, zero-byte,
uninitialized, corrupt, or restore-gated database, and the snapshot verifier
retains its source ownership, permissions, WAL/SHM consistency, signature, and
isolation checks. No migration command is part of this upgrade path.

For a code rollback, keep both units stopped and disabled, preserve the new
release and all state/backups, and verify that the previous release supports
the installed systemd credential-file interface and current database schema.
Application pointer rollback does not roll back the shared preflight. Before
activation, verify that the compatible preflight still matches the SHA-256
above. Only then may an approved operator atomically repoint `current` and
separately authorize service activation. A pointer rollback alone does not
guarantee a working service. If compatibility is uncertain or fails, leave the
service stopped and disabled while preparing a corrected compatible release.
Never restore an older database snapshot merely to solve a code-startup failure.

### Read-only v2.0.0 production preflight

Before a separately approved deployment window, an administrator may run the
candidate's standalone `deploy/production_preflight.py` to collect a read-only
report. First verify the candidate's reviewed commit and clean working tree.
The script requires an explicit expected candidate SHA and checks both the
branch ref and `HEAD` against it.

Do not run it as root directly from a developer-owned checkout. Root execution
is allowed only from a reviewed copy in a root-owned, non-group/world-writable
directory, and the candidate checkout it inspects must also be root-owned and
write-protected. The script checks those conditions before running Git or
reading production state. Git status and worktree inspection run only after
the candidate tree is verified as root-owned and the exact `HEAD` and branch
ref match the expected SHA; the script rejects local Git filter/include
settings before checking worktree cleanliness. Stage both copies only under a
separately approved administrator procedure; this documentation does not
authorize changing production paths.

For this reviewed candidate, the script SHA-256 is
`eb3d30daeae34cdcf627250532cdfcbb6fee37cde040bdade55ad99b06d89e65`. Compare
the staged copy's hash with this value before the root-controlled run.

```bash
git -C <ROOT_OWNED_CANDIDATE_CHECKOUT> status --short --branch
git -C <ROOT_OWNED_CANDIDATE_CHECKOUT> rev-parse HEAD
sha256sum <ROOT_OWNED_REVIEWED_COPY>/production_preflight.py
sudo /usr/bin/python3.14 -I -B \
  <ROOT_OWNED_REVIEWED_COPY>/production_preflight.py \
  --candidate <ROOT_OWNED_CANDIDATE_CHECKOUT> \
  --expected-candidate-sha <FULL_REVIEWED_CANDIDATE_SHA>
```

The report separates **Current production health** from **v2.0.0 deployment
prerequisites**. Missing v2.0.0 pins, checkout state, helper files, and target
artifacts are `UNKNOWN` while they have not been provisioned; they are not
reported as damage to the existing bot. Existing production health findings
such as a missing database or invalid current release remain `FAIL`.

Each check reports `PASS`, `FAIL`, or `UNKNOWN`. Exit code 0 means no failures
or unknowns; 1 means at least one failure; 2 means there is at least one
unknown and no failure. Any nonzero result is diagnostic only: the script
never repairs a problem. Do not treat a report as deployment authorization.

#### Administrator-controlled staging procedure (documentation only)

This procedure has **not** been run. It separates developer-side Git work
from administrator staging: the developer creates a Git bundle without
elevated privileges, then root copies and verifies that data file. Root never
runs Git in, or against, the developer-owned repository. The commands below
are documentation only; they do not authorize staging or preflight execution.

First, as `codex-dev`, create a bundle only from the reviewed, clean branch.
Use the expected commit shown in the review report. The commands disable Git
hooks and filesystem monitoring for these checks. If the output file already
exists, stop and choose a separately reviewed unused filename; do not replace
it. If you choose another filename, set `SOURCE_BUNDLE` to that same path in
the administrator commands below.

```bash
set -eu
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 GIT_TERMINAL_PROMPT=0
REPO='/home/codex-dev/projects/wow-forever-armory-bot-v2-candidate'
RELEASE_SHA='FINAL_REVIEWED_COMMIT'
BRANCH='release/v2.0.0'
BUNDLE='/home/codex-dev/wow-armory-v2.0.0.bundle'
GIT='/usr/bin/git'

test ! -e "$BUNDLE"
test ! -L "$BUNDLE"
test "$("$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" branch --show-current)" = "$BRANCH"
test "$("$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" rev-parse HEAD)" = "$RELEASE_SHA"
test "$("$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" rev-parse "refs/heads/$BRANCH")" = "$RELEASE_SHA"
WORKTREE_STATUS="$("$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" status --porcelain --untracked-files=all)"
test -z "$WORKTREE_STATUS"
"$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" bundle create "$BUNDLE" "refs/heads/$BRANCH"
"$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" bundle verify "$BUNDLE"
test "$("$GIT" bundle list-heads "$BUNDLE" "refs/heads/$BRANCH")" = "$RELEASE_SHA refs/heads/$BRANCH"
WORKTREE_STATUS="$("$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO" status --porcelain --untracked-files=all)"
test -z "$WORKTREE_STATUS"
/usr/bin/sha256sum "$BUNDLE"
```

Record the bundle SHA-256 from this output and provide it to the administrator
through the approved review channel. The administrator must compare it with
that independently recorded value before allowing the copied bundle into Git.

Then, from a trusted administrator terminal, set `BUNDLE_SHA256` to the
recorded 64-character bundle hash and `SCRIPT_SHA256` to the reviewed script
hash (`eb3d30daeae34cdcf627250532cdfcbb6fee37cde040bdade55ad99b06d89e65`).
If a command fails, stop. Do not remove or reuse the partial staging directory;
have an administrator inspect it and choose a new unused staging root.
The new repository starts on a separate `staging-import` branch. The bundle
ref is fetched under `refs/remotes/staging/` and only then used to create the
release branch, so the fetch never writes into a checked-out branch.

```bash
set -euo pipefail
RELEASE_SHA='FINAL_REVIEWED_COMMIT'
BRANCH='release/v2.0.0'
BUNDLE_SHA256='RECORDED_BUNDLE_SHA256'
SCRIPT_SHA256='eb3d30daeae34cdcf627250532cdfcbb6fee37cde040bdade55ad99b06d89e65'
SOURCE_BUNDLE='/home/codex-dev/wow-armory-v2.0.0.bundle'
STAGE_ROOT='/var/lib/armory-preflight-staging'
STAGE="$STAGE_ROOT/$RELEASE_SHA"

[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$BUNDLE_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$SCRIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$(sudo /usr/bin/stat -c %U /var/lib)" = root ]]
sudo /usr/bin/test -d /var/lib
sudo /usr/bin/test ! -L /var/lib
LIB_MODE="$(sudo /usr/bin/stat -c %a /var/lib)"
(( (8#$LIB_MODE & 022) == 0 ))
sudo /usr/bin/test -d /var
sudo /usr/bin/test ! -L /var
[[ "$(sudo /usr/bin/stat -c %U /var)" = root ]]
VAR_MODE="$(sudo /usr/bin/stat -c %a /var)"
(( (8#$VAR_MODE & 022) == 0 ))

if sudo /usr/bin/test -e "$STAGE_ROOT"; then
  sudo /usr/bin/test -d "$STAGE_ROOT"
  sudo /usr/bin/test ! -L "$STAGE_ROOT"
  [[ "$(sudo /usr/bin/stat -c %U "$STAGE_ROOT")" = root ]]
  STAGE_MODE="$(sudo /usr/bin/stat -c %a "$STAGE_ROOT")"
  (( (8#$STAGE_MODE & 022) == 0 ))
else
  sudo /usr/bin/mkdir -m 0700 "$STAGE_ROOT"
fi
sudo /usr/bin/test ! -e "$STAGE"
sudo /usr/bin/test ! -L "$STAGE"
sudo /usr/bin/mkdir -m 0700 "$STAGE"
sudo /usr/bin/mkdir -m 0700 "$STAGE/tmp"

sudo /usr/bin/test -f "$SOURCE_BUNDLE"
sudo /usr/bin/test ! -L "$SOURCE_BUNDLE"
[[ "$(sudo /usr/bin/stat -c %U "$SOURCE_BUNDLE")" = codex-dev ]]
BUNDLE_MODE="$(sudo /usr/bin/stat -c %a "$SOURCE_BUNDLE")"
(( (8#$BUNDLE_MODE & 022) == 0 ))
sudo /usr/bin/test ! -e "$STAGE/source.bundle"
sudo /usr/bin/install -o root -g root -m 0400 "$SOURCE_BUNDLE" "$STAGE/source.bundle"
COPIED_BUNDLE_SHA256="$(sudo /usr/bin/sha256sum "$STAGE/source.bundle" | /usr/bin/cut -d ' ' -f 1)"
[[ "$COPIED_BUNDLE_SHA256" = "$BUNDLE_SHA256" ]]

root_git() {
  sudo /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root TMPDIR="$STAGE/tmp" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git -c core.hooksPath=/dev/null -c core.fsmonitor=false \
    -c credential.helper= "$@"
}

root_git_file_only() {
  sudo /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root TMPDIR="$STAGE/tmp" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 \
    GIT_ALLOW_PROTOCOL=file \
    /usr/bin/git -c core.hooksPath=/dev/null -c core.fsmonitor=false \
    -c credential.helper= "$@"
}

root_git init --initial-branch=staging-import "$STAGE/candidate"
root_git -C "$STAGE/candidate" config --local core.hooksPath /dev/null
root_git -C "$STAGE/candidate" config --local core.fsmonitor false
root_git -C "$STAGE/candidate" config --local core.symlinks false
root_git -C "$STAGE/candidate" config --local credential.helper ''
root_git -C "$STAGE/candidate" bundle verify "$STAGE/source.bundle"
root_git_file_only -C "$STAGE/candidate" fetch "$STAGE/source.bundle" \
  "refs/heads/$BRANCH:refs/remotes/staging/$BRANCH"
root_git -C "$STAGE/candidate" ls-tree -rz --full-tree -r "$RELEASE_SHA" |
  /usr/bin/python3 -I -B -c '
import sys

for record in sys.stdin.buffer.read().split(b"\0"):
    if not record:
        continue
    metadata, path = record.split(b"\t", 1)
    mode, kind, _object_id = metadata.split(b" ", 2)
    parts = path.split(b"/")
    if (
        path.startswith(b"/")
        or any(part in {b"", b".", b".."} for part in parts)
        or any(part.lower() == b".git" for part in parts)
    ):
        raise SystemExit("unsafe path in Git tree")
    if kind != b"blob" or mode not in {b"100644", b"100755"}:
        raise SystemExit("symlink or special file in Git tree")
'
root_git -C "$STAGE/candidate" switch --create "$BRANCH" \
  "refs/remotes/staging/$BRANCH"
root_git -C "$STAGE/candidate" remote add origin \
  https://github.com/PepeFeels/wow-forever-armory-bot.git
LOCAL_CONFIG_KEYS="$(root_git -C "$STAGE/candidate" config --local --name-only --list)"
while IFS= read -r key; do
  case "$key" in
    core.repositoryformatversion|core.filemode|core.bare|core.logallrefupdates|\
    core.hookspath|core.fsmonitor|core.symlinks|credential.helper|\
    remote.origin.url|remote.origin.fetch|\
    branch.release/v2.0.0.remote|branch.release/v2.0.0.merge)
      ;;
    *) printf 'Unexpected local Git setting: %s\n' "$key" >&2; exit 1 ;;
  esac
done <<< "$LOCAL_CONFIG_KEYS"
[[ "$(root_git -C "$STAGE/candidate" config --local --get core.hooksPath)" = /dev/null ]]
[[ "$(root_git -C "$STAGE/candidate" config --local --get core.fsmonitor)" = false ]]
[[ "$(root_git -C "$STAGE/candidate" config --local --get core.symlinks)" = false ]]
[[ -z "$(root_git -C "$STAGE/candidate" config --local --get-all credential.helper)" ]]
STAGED_HEAD="$(root_git -C "$STAGE/candidate" rev-parse HEAD)"
STAGED_BRANCH_HEAD="$(root_git -C "$STAGE/candidate" rev-parse "refs/heads/$BRANCH")"
STAGED_BRANCH="$(root_git -C "$STAGE/candidate" branch --show-current)"
STAGED_STATUS="$(root_git -C "$STAGE/candidate" status --porcelain --untracked-files=all)"
[[ "$STAGED_HEAD" = "$RELEASE_SHA" ]]
[[ "$STAGED_BRANCH_HEAD" = "$RELEASE_SHA" ]]
[[ "$STAGED_BRANCH" = "$BRANCH" ]]
[[ -z "$STAGED_STATUS" ]]
[[ "$(root_git -C "$STAGE/candidate" remote get-url origin)" = \
  'https://github.com/PepeFeels/wow-forever-armory-bot.git' ]]

SYMLINK_FOUND="$(sudo /usr/bin/find "$STAGE/candidate" -xdev -type l -print -quit)"
sudo /usr/bin/test -z "$SYMLINK_FOUND"
NONROOT_OWNER_FOUND="$(sudo /usr/bin/find "$STAGE/candidate" -xdev ! -user root -print -quit)"
sudo /usr/bin/test -z "$NONROOT_OWNER_FOUND"
sudo /usr/bin/chmod -R a-w "$STAGE/candidate"
WRITABLE_MODE_FOUND="$(sudo /usr/bin/find "$STAGE/candidate" -xdev -perm /022 -print -quit)"
sudo /usr/bin/test -z "$WRITABLE_MODE_FOUND"

sudo /usr/bin/test ! -e "$STAGE/production_preflight.py"
sudo /usr/bin/install -o root -g root -m 0444 \
  "$STAGE/candidate/deploy/production_preflight.py" \
  "$STAGE/production_preflight.py"
COPIED_SCRIPT_SHA256="$(sudo /usr/bin/sha256sum "$STAGE/production_preflight.py" | /usr/bin/cut -d ' ' -f 1)"
[[ "$COPIED_SCRIPT_SHA256" = "$SCRIPT_SHA256" ]]
[[ "$(sudo /usr/bin/stat -c %u:%g:%a "$STAGE")" = 0:0:700 ]]
[[ "$(sudo /usr/bin/stat -c %u:%g:%a "$STAGE/tmp")" = 0:0:700 ]]
[[ "$(sudo /usr/bin/stat -c %u:%g:%a "$STAGE/source.bundle")" = 0:0:400 ]]
[[ "$(sudo /usr/bin/stat -c %u:%g:%a "$STAGE/production_preflight.py")" = 0:0:444 ]]
sudo /usr/bin/stat -c '%U:%G %a %n' "$STAGE" "$STAGE/source.bundle" \
  "$STAGE/tmp" "$STAGE/candidate" "$STAGE/production_preflight.py"
```

All root-run Git commands above operate only in the new root-owned checkout or
on the root-owned copied bundle. The bundle is hash-checked before Git reads
it. Only the single bundle-fetch command receives `GIT_ALLOW_PROTOCOL=file`;
no `protocol.file.allow=always` setting is used and network/helper protocols
are not allowed for that fetch. Git runs with an empty environment,
system/global configuration disabled, hooks disabled, filesystem monitoring
disabled, and credential helpers cleared. Git temporary files are directed to
the new staging directory's private `tmp` subdirectory. The fresh repository's
local config keys are checked against a short allowlist, so it cannot contain
external filters or other developer-provided commands. No root command runs
Git in the developer repository.

Before checkout, a system Python command reads the Git tree listing and rejects
absolute or traversal paths, symlinks, submodules, and non-regular files. Git
also has `core.symlinks=false`, and the resulting checkout is scanned for
symlinks, non-root ownership, and group/world write permission. These checks
reject `.git` path components and confine Git checkout writes to the new
staging tree. Staging stops on any unexpected object or path.

The new staging directory is separate from the active release, production
checkout, database, backups, and audit journal. The procedure never deletes or
reuses an existing commit-specific staging directory. The checkout and script
are root-owned and write-protected against `codex-dev`. Preflight execution is
a separate action and is not part of staging.

The staging-flow checks are reproducible without elevated privileges:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/codex-dev/projects/wow-forever-armory-bot/.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_staging_procedure.py
```

The tests create all repositories, bundles, and staging paths inside temporary
directories. They verify clean import into a new repository, branch and commit
identity, origin URL, allowed Git configuration, and no execution of
developer-defined hooks, smudge filters, or credential helpers. They also
verify that a repeated target, bundle-hash mismatch, symlink object, and
hand-crafted `../` tree path are rejected without changing an existing staged
copy or writing outside the temporary directory. The root behavior is
simulated as an ordinary unprivileged user; the tests never invoke `sudo` or
write production paths.

Recorded result for the reviewed change: **5 passed**. Bash syntax validation
also passed for both staging procedure command blocks and the test command.
No privileged staging or
production execution was performed.

The script checks candidate and protected production checkout Git identity,
Python 3.14 and x86_64, the `wow-armory` account, pinned release identity,
`current` and the previous release marker, unit states, protected file
metadata, installed wrapper/installer/preflight/unit/launcher hashes, the
failed v1.1.4 journal and manifest, and whether the v2 target directory
already exists. It does not read the configuration or Discord credential
contents and does not print environment variables. It uses only `git` read
queries and `systemctl is-active`/`is-enabled` queries.

The database descriptor is opened with `O_RDONLY` and `O_NOFOLLOW`; SQLite
checks use that already-open descriptor and run as the `wow-armory` account.
The connection uses `mode=ro&immutable=1` and `PRAGMA query_only=ON` only when
the writer service is confirmed inactive and no sidecars are present. This
does not create SQLite shared-memory files. If the bot or backup service is an
active writer, or a database has WAL, SHM, or rollback-journal sidecars,
integrity is reported as `UNKNOWN`: the preflight will not inspect a changing
database, and immutable mode cannot include uncheckpointed WAL content safely.
The preflight does not copy, checkpoint, restore, or otherwise change database
or backup files. Resolve an unknown result with a separately reviewed
read-only procedure; do not change service or database state as part of this
preflight.

### Read-only preflight before manual recovery

The wrapper deliberately does not switch `current` back automatically after a
failure once shutdown has started. It leaves both managed units stopped and
disabled and records `manual_recovery_required`. Before an administrator
chooses a recovery action, run the checks below. They inspect the journal, pin,
manifest, current pointer, previous release, database signature, backup hash,
and preserved preflight. They do not clear or rewrite audit files, change the
pointer, restore a database, or change service state. The SQLite snapshot check
uses a temporary private copy under `/tmp` and removes only that temporary
copy.

Run this read-only metadata check as root. It accepts `current` pointing either
to the previous release or to v2.0.0, covering a failure after the installer has
already switched the pointer. It stops on any mismatch:

```bash
sudo /usr/bin/python3.14 -I -B - <<'PY'
import grp, hashlib, json, os, pwd, re, stat
from pathlib import Path
import sys

root = Path("/var/lib/armory-deployment-audit")
pin_path = Path("/etc/wow-forever-armory-bot/deployment-pin.json")
journal_path = root / "deployment-state.json"
base = Path("/opt/wow-forever-armory-bot")
releases = base / "releases"
current = base / "current"
state = Path("/var/lib/wow-forever-armory-bot")
database = state / "guild-armory.sqlite3"
backups = state / "backups"

def read_private_json(path, owner, mode):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == owner
        assert stat.S_IMODE(info.st_mode) == mode and info.st_size <= 65536
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    assert len(raw) <= 65536
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, "duplicate JSON key"
            result[key] = value
        return result
    return json.loads(raw.decode("ascii"), object_pairs_hook=unique)

def regular(path, owner, group, mode):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == owner
    assert info.st_gid == group and stat.S_IMODE(info.st_mode) == mode
    return info

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

audit_info = root.lstat()
assert stat.S_ISDIR(audit_info.st_mode) and audit_info.st_uid == 0
assert stat.S_IMODE(audit_info.st_mode) == 0o700

def service_has(mode, uid, gid, bit):
    if uid == service_uid:
        return bool(mode & (bit << 6))
    if gid == service_gid:
        return bool(mode & (bit << 3))
    return bool(mode & bit)

service_uid = pwd.getpwnam("wow-armory").pw_uid
service_gid = grp.getgrnam("wow-armory").gr_gid

def verify_release(path):
    assert path.is_dir() and not path.is_symlink()
    for directory, children, files in os.walk(path, followlinks=False):
        for item in [Path(directory), *(Path(directory) / name for name in children)]:
            if item.is_symlink():
                continue
            info = item.stat()
            assert info.st_uid == 0 and not info.st_mode & 0o022
            assert service_has(info.st_mode, service_uid, service_gid, 0o1)
        for name in files:
            item = Path(directory) / name
            if item.is_symlink():
                continue
            info = item.stat()
            assert info.st_uid == 0 and not info.st_mode & 0o022
            assert service_has(info.st_mode, service_uid, service_gid, 0o4)
            if info.st_mode & 0o111:
                assert service_has(info.st_mode, service_uid, service_gid, 0o1)
    interpreter = path / ".venv/bin/python"
    info = interpreter.stat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == 0
    assert not info.st_mode & 0o022
    assert service_has(info.st_mode, service_uid, service_gid, 0o4)
    assert service_has(info.st_mode, service_uid, service_gid, 0o1)

pin = read_private_json(pin_path, 0, 0o600)
journal = read_private_json(journal_path, 0, 0o600)
assert set(pin) == {"release_sha", "previous_sha", "installer_sha256"}
assert re.fullmatch(r"[0-9a-f]{40}", pin["release_sha"])
assert re.fullmatch(r"[0-9a-f]{40}", pin["previous_sha"])
assert re.fullmatch(r"[0-9a-f]{64}", pin["installer_sha256"])
approved = pin["release_sha"]
previous = pin["previous_sha"]
assert journal["approved_release_sha"] == approved
assert journal["previous_release_sha"] == previous
assert journal["phase"] in {
    "shutdown_started", "installer_started", "installer_returned",
    "release_verified", "state_restoration_started", "manual_recovery_required",
}
assert current.is_symlink(), "current is not an approved release symlink"
target = current.resolve(strict=True)
previous_path = releases / previous
approved_path = releases / approved
assert target in {previous_path, approved_path}, "current points elsewhere"
verify_release(previous_path)
if target != previous_path:
    verify_release(target)

previous_marker = regular(previous_path / ".installation-complete", 0, 0, 0o444)
assert (previous_path / ".installation-complete").read_text("ascii") == previous
regular(target / ".installation-complete", 0, 0, 0o444)
assert (target / ".installation-complete").read_text("ascii") == target.name

db_info = regular(database, service_uid, service_gid, 0o600)
signature = [db_info.st_dev, db_info.st_ino, db_info.st_size,
             db_info.st_mtime_ns, db_info.st_ctime_ns, db_info.st_uid,
             db_info.st_gid, db_info.st_mode]
assert signature == journal["database_signature"], "database changed since shutdown"

backup = Path(journal["database_backup"])
assert backup.parent == backups and backup.name.startswith("guild-armory-")
assert backup.suffix == ".sqlite3"
regular(backup, service_uid, service_gid, 0o600)
assert digest(backup) == journal["database_backup_sha256"]

manifest_path = Path(journal["manifest"])
assert manifest_path == root / f"deployment-{approved}.json"
manifest = read_private_json(manifest_path, 0, 0o600)
assert manifest["approved_release_sha"] == approved
assert manifest["previous_release_sha"] == previous
assert manifest["database_backup"] == str(backup)
assert manifest["database_backup_sha256"] == journal["database_backup_sha256"]

saved_preflight = Path(journal["shared_preflight_backup"])
assert saved_preflight == root / f"armory-db-preflight.{previous}.py"
regular(saved_preflight, 0, 0, 0o700)
assert digest(saved_preflight) == journal["shared_preflight_sha256"]
assert manifest["shared_preflight_backup"] == str(saved_preflight)
assert manifest["shared_preflight_sha256"] == journal["shared_preflight_sha256"]

sys.path.insert(0, "/opt/wow-forever-armory-bot/deployment-checkout")
from deploy.production_db_snapshot import verify_database_snapshot
verify_database_snapshot(database, expected_owner=service_uid, expected_group=service_gid)
verify_database_snapshot(backup, expected_owner=service_uid, expected_group=service_gid)
print(f"Metadata preflight passed; phase={journal['phase']}; current={target.name}; previous={previous}")
PY
```

Then use the previous release's installed Python to run its read-only SQLite
integrity check against both the live database and the verified backup. Replace
`<PREVIOUS_SHA>` with the value checked against the pin above:

```bash
sudo -u wow-armory /opt/wow-forever-armory-bot/releases/<PREVIOUS_SHA>/.venv/bin/python -I -B -m wow_forever_armory.operations check --database /var/lib/wow-forever-armory-bot/guild-armory.sqlite3
sudo -u wow-armory /opt/wow-forever-armory-bot/releases/<PREVIOUS_SHA>/.venv/bin/python -I -B -m wow_forever_armory.operations check --database /var/lib/wow-forever-armory-bot/backups/<BACKUP_NAME>
sudo systemctl is-active wow-forever-armory-bot.service
sudo systemctl is-enabled wow-forever-armory-bot.service
sudo systemctl is-active wow-forever-armory-backup.timer
sudo systemctl is-enabled wow-forever-armory-backup.timer
sudo systemctl is-active wow-forever-armory-backup.service
```

The service and timer must already be inactive and disabled, and the backup
service inactive. These `systemctl` commands only query state. If any check
fails, do not change `current`, restore a database, clear the journal, or delete
evidence; keep the system stopped and investigate the mismatch.

For the recorded installer rejection, also verify the journal says
`phase=manual_recovery_required`, `interrupted_phase=installer_started`,
`failure_class=InstallerExecutionFailure`, and an installer failure with
`category=installer_rejected`, `exit_code=1`, and
`outcome=installer_rejected`. Require `fail_closed_verified` to be exactly
`true` and `fail_closed_error` to be absent or null. These fields explain the
recorded failure; they do not replace checking the units' live states.

#### Recovery verification record

Complete the checks in this order and record each as `PASS`, `FAIL`, or
`UNKNOWN`; do not issue an acknowledgment if any result is not `PASS`:

1. Record the pinned previous and approved release SHAs. Confirm `current`
   resolves to one of those exact release directories, its installation marker
   contains that directory's SHA, and the release tree and interpreter have the
   required owner and permissions.
2. Compare the live database's device, inode, size, modification/change times,
   owner, group, and mode with the journal signature. Verify database
   structure/integrity and confirm the backup's owner, mode, recorded SHA-256,
   and SQLite integrity. Confirm the manifest repeats the release identities,
   backup path/hash, and saved-preflight path/hash from the journal. Verify the
   saved preflight file's SHA-256.
3. Record exact queried unit states. For manual recovery, the bot and backup
   timer must be `inactive/disabled`; the backup service must be `inactive`
   (`static` or `disabled` enablement). Do not infer state from
   `fail_closed_verified` alone.
4. Preserve a result record with the journal and manifest hashes, observed
   release target and marker, database signature and integrity result, backup
   hash and integrity result, preflight hash, each unit's active/enabled state,
   UTC completion time, administrator identity, approval reference, and final
   decision. The `approval_reference` in the acknowledgment must point to this
   separately approved record.

The metadata comparison and `systemctl is-active`/`is-enabled` queries are
read-only, but require a separately authorized administrator review session.
SQLite integrity checks against the live database require the bot and backup
service stopped, the backup timer disabled, and no SQLite sidecar files; those
state changes are production changes and need separate explicit approval.
Creating the archived journal and acknowledgment modifies audit files and
requires a separate recovery approval. Switching `current`, restoring a
database, or starting/enabling services each requires its own production
approval. This task performed none of these actions.

If every check passes and a separate recovery approval is given, preserve the
journal, manifest, backup, both release directories, and the compatible shared
preflight. If `current` still points to v2.0.0, an administrator may atomically
repoint it to the validated previous release with a new temporary symlink and
same-filesystem rename. After separately approved authorization, and only after
confirming the temporary name is unused:

```bash
sudo ln -s releases/<PREVIOUS_SHA> \
  /opt/wow-forever-armory-bot/.current.recovery
sudo mv -T /opt/wow-forever-armory-bot/.current.recovery \
  /opt/wow-forever-armory-bot/current
sudo readlink -e /opt/wow-forever-armory-bot/current
```

For new pointers, use a relative target (`releases/<PREVIOUS_SHA>`) so the
release tree can be relocated as one unit. The read-only preflight accepts
both this canonical relative form and the exact canonical absolute form used
by the existing installation. It rejects traversal, other paths, and
non-release names. Existing pointers are not rewritten by preflight.

If `current` already points to the previous release, do not switch it again.
Do not replace the database with the backup for a code-only failure. Keep
services stopped until a separate activation decision.
The pending journal is evidence and continues to block another deployment.
Never edit its JSON contents. After a separate recovery approval and all
checks above, preserve the journal byte-for-byte. Compute its SHA-256 and move
it to the unused name
`deployment-state-<journal-sha256>.json` in the audit directory. Verify its
hash after the move. Keep the manifest, backup, release directories, and
preflight copy.

Create a separate root-owned `0600` file named
`deployment-recovery-ack-<journal-sha256>.json`. Its JSON object must contain
exactly these fields: `schema_version` (integer `1`), `journal_sha256`,
`manifest_sha256`, `approved_release_sha`, `previous_release_sha`, `decision`,
`approved_by`, `approval_reference`, and `approved_at_utc`. For a manual
recovery, `decision` is `manual_recovery_verified`; for a completed deployment,
it is `deployment_completion_verified`. Bind the hashes to the archived
journal and its manifest. `approved_by` must identify the approving
administrator, `approval_reference` must identify the separately approved
record, and `approved_at_utc` must be an ISO-8601 UTC time. This root-owned
record is an administrative attestation, not a digital signature.

Preflight and the deployment wrapper now verify every archived journal,
matching manifest, and acknowledgment. An orphan manifest, archive, or
acknowledgment blocks deployment. A first installation may proceed through
this check only when the protected audit directory exists and contains no
deployment records. A missing audit directory fails closed because previous
history cannot be established. Never remove or overwrite prior artifacts. If
any check or hash comparison fails, leave the active journal in place and keep
the services stopped. No acknowledgment is created by this review.

Both verifiers open each audit JSON with symlink following disabled, require a
single-link regular file with the expected owner and exact private mode, and
read a bounded byte sequence through that descriptor. They compare descriptor
metadata before and after reading with the current directory entry, then parse
JSON and calculate SHA-256 from that same byte sequence. Read errors, short
reads, metadata changes, and path replacement stop verification.

To deactivate in an approved rollback, stop and disable both units while
retaining the database and backups:

```bash
# Operationally changes service state; not run by this task.
sudo systemctl disable --now wow-forever-armory-backup.timer
sudo systemctl disable --now wow-forever-armory-bot.service
```

The bot owns an advisory lock at
`<ARMORY_DATABASE_PATH>.runtime.lock` and an inode lock on the database file
for its entire lifetime. Locking the inode also catches hard-link aliases, and
sets the database file mode to `0600`. A second updated bot process using the
same database fails closed. Restore takes both locks and refuses to proceed
while the bot is running. Advisory locks coordinate these programs only; an
administrator must not bypass them by copying database files while the service
is active.

The pre-TASK-016 manually started bot does not hold this new lock. During the
first separately approved migration, the operator must verify and stop that
older process before starting the systemd unit or running restore. This work
does not inspect, stop, or replace that process.

### Legacy database import required before first service start

The service template uses
`/var/lib/wow-forever-armory-bot/guild-armory.sqlite3`. If the legacy process
uses a different path, the unit will otherwise initialize an empty database.
Do not start it until the existing database has been imported and verified at
the configured StateDirectory path. For the current legacy setup, confirm the
source path from the running process's filtered inventory and launch record;
never infer it from a similarly named file.

In an approved migration, create a verified online SQLite backup while the
legacy bot is still running, using a private staging directory writable by its
user. This is the pre-upgrade data checkpoint. Then stop the old process and
verify it exited; install the verified backup as the new StateDirectory
database with owner `wow-armory:wow-armory` and mode `0600`. Run
`operations check` and compare only aggregate guild/member/character counts
before starting systemd. Keep both the original database and migration backup
until panel recovery and member rows have been checked. The original DB and
`data/` directory may need restrictive permissions after the old bot stops;
do not change them during preparation.

Do not configure the service to use a database under `/home`: the unit uses
`ProtectHome=true`, and the dedicated service account must not inherit access
to the developer home directory. Do not create a new empty StateDirectory DB
and then copy characters into it manually.

For that approved migration only, identify the old process by command and
working directory without printing its environment (which may contain the
token). Compare its configured database path with the intended service path
using the launch record. If identity or database path is uncertain, do not
start the service or run restore:

```bash
# Read-only process identification; does not print environment variables.
pgrep -f '[w]ow_forever_armory.discord_bot'
ps -o pid=,user=,comm= -p "$BOT_PID"
readlink -f "/proc/$BOT_PID/cwd"

# Stops the old process gracefully; only in an approved maintenance window.
kill -INT "$BOT_PID"
for _ in $(seq 1 30); do
  if ! kill -0 "$BOT_PID" 2>/dev/null; then break; fi
  sleep 1
done
if kill -0 "$BOT_PID" 2>/dev/null; then
  echo 'Old bot process did not stop; do not start the service.' >&2
  exit 1
fi
```

Verify no old process remains before starting the unit. The restore utility
cannot identify an older process that does not honor the new lock; this manual
migration gate is required before first deployment.

Backup and restore also share `<ARMORY_DATABASE_PATH>.backup.lock`; backup
publication and retention additionally serialize through `.backup.lock` inside
the selected backup directory. Online backups do not take the bot runtime lock,
so SQLite can continue serving the application while the snapshot is made.

## Safe read-only checks

These commands inspect state and do not start or change the service:

```bash
systemctl status wow-forever-armory-bot.service
journalctl -u wow-forever-armory-bot.service --since today
journalctl -u wow-forever-armory-backup.service --since today
df -h /var/lib/wow-forever-armory-bot
sudo -u wow-armory /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations check
```

`check` runs SQLite `PRAGMA integrity_check`. The backup job emits neutral
completion/failure events to journald and does not log paths or character data.
For a crash loop, inspect status, the latest bot journal entries, disk space,
and the integrity check before taking action. Discord connection and restored
guild rows can only be confirmed through the bot's startup/recovery logs and
the separately approved controlled Discord environment.

## Backup and retention

The backup utility uses SQLite's online backup API, so it can take a consistent
copy while SQLite is open. It briefly holds SQLite's `BEGIN IMMEDIATE` writer
reservation during the snapshot so later commits cannot repeatedly restart the
online backup; existing readers continue, while application writes wait for the
copy to finish. It writes a mode `0600` temporary file in the backup directory,
checks the copy with `integrity_check`, fsyncs it, then atomically publishes
it. Backup jobs use nonblocking locks to prevent overlap with another backup or
restore. The timer service has a 30-minute execution limit. If interrupted
before publication, a later run removes only matching unpublished temporary
files while holding the backup locks; completed backups are not treated as
temporary files. The default retention is 14 verified daily copies; set
`ARMORY_BACKUP_RETENTION` to a positive integer. Retention runs only after a
new verified backup has been published and always keeps that latest copy.
Keep an additional off-host backup according to the operator's recovery
objectives; this implementation does not provide off-host replication.

Run an explicit manual backup (this writes a backup file):

```bash
sudo -u wow-armory env ARMORY_DATABASE_PATH=/var/lib/wow-forever-armory-bot/guild-armory.sqlite3 \
  ARMORY_BACKUP_DIR=/var/lib/wow-forever-armory-bot/backups \
  /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations backup
```

The included timer is an example only. A separately approved operator would
install and enable `wow-forever-armory-backup.timer`; do not enable both this
timer and an external scheduler for the same database.

## Restore

Restore is an intentionally explicit, writing operation. First capture the
current service state and identify a known-good backup. Stop the bot through
the approved service procedure, then run `operations check` on the selected
backup if desired. Restore itself verifies the backup, requires
`--confirm-replace`, and acquires the same runtime lock. It makes and verifies
a timestamped pre-restore rollback copy before replacing the database. SQLite's
backup API incorporates committed WAL data in that rollback copy. Any residual
`-wal` and `-shm` sidecars are moved aside with timestamped names before the
replacement; they are never blindly applied to the restored database. If a
restore fails before replacement, moved sidecars are put back where possible.
Do not delete the rollback or quarantined sidecars until the recovered service
has been checked.

If the active database fails SQLite integrity checking, restore preserves its
raw bytes and any residual WAL/SHM files as
`*.pre-restore-corrupt-*.sqlite3*`, then installs the verified backup. This raw
copy is forensic evidence and is **not** a verified or directly restorable
SQLite rollback. The valid pre-restore rollback path is created only when the
existing database passes integrity checking.

Restore writes a mode `0600` `*.restore-in-progress` marker before the
multi-file swap. Startup refuses to run while this marker exists. If a host
failure leaves the marker, inspect the database, `*.pre-restore-*.sqlite3`
rollback copy, and quarantined sidecars while the service remains stopped. Run
`operations check` on candidate databases; choose and atomically install the
known-good file only under the approved recovery procedure. Remove the marker
only after the active database and any sidecars have been reconciled and
verified. If the correct state is uncertain, keep the marker and escalate;
starting the service with unresolved WAL/SHM state is unsafe.

Example command (destructive to the active database; requires separate
approval and a stopped bot):

```bash
BACKUP_FILE='/var/lib/wow-forever-armory-bot/backups/guild-armory-YYYYMMDDTHHMMSS.ffffffZ.sqlite3'
sudo -u wow-armory env ARMORY_DATABASE_PATH=/var/lib/wow-forever-armory-bot/guild-armory.sqlite3 \
  /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations restore \
  --backup "$BACKUP_FILE" \
  --confirm-replace
```

After restoring, run the integrity check, start the service only with approval,
then verify startup recovery, panel/member rows, and Discord connection. If
health checks fail, stop the service and restore the preserved
`*.pre-restore-*.sqlite3` copy using the same explicit procedure. A rollback is
also a database replacement and must not be performed while the bot holds its
runtime lock.

## Upgrade and rollback runbook

Every step below is for a future separately approved maintenance window; this
task did not perform it.

1. Read the installed version and reviewed release SHA. Check service status,
   recent journald output, disk space, and current SQLite integrity.
2. Create a verified online backup and confirm a recent backup is readable.
3. Stage the new release beside the current release; do not overwrite the
   current release in place. Preserve the previous release directory/symlink.
4. Stop and disable both `wow-forever-armory-bot.service` and
   `wow-forever-armory-backup.timer`. Verify both are inactive and disabled.
   Confirm no Armory process remains before installation.
5. From the clean, approved release checkout, run the documented
   `--upgrade-existing` installer. Do not manually replace the stable launcher
   or systemd units. The installer verifies a database snapshot, stages the
   release, and atomically switches `current` without starting either unit.
6. Verify the release marker, `current`, database integrity, backup availability,
   and systemd configuration. Only after separate approval, activate the bot
   and backup timer and verify Discord connectivity, startup recovery,
   panel/member behavior, and service health.
7. If health checks fail, stop and disable both units and preserve the current
   database, backups, and release artifacts. Verify credential-file and schema
   compatibility before any application pointer rollback. Application rollback
   and preflight rollback are separate operations: while the database has zero
   guilds, retain the compatible preflight and verify its exact SHA-256 above;
   never restore the saved v1.1.2 preflight. If a compatible preflight cannot be
   retained and verified, keep the service stopped. An approved operator may
   atomically restore the compatible previous application pointer and separately
   authorize activation. Database restoration is a separate, explicitly
   approved recovery operation, never an automatic response to startup failure.

The v1.0.0 code has no schema migration framework. Keep upgrades
backward-compatible with its current SQLite schema until a migration strategy
is designed and reviewed. File-level SQLite backups do not protect against
host loss; copy verified backups off-host through a separately reviewed
mechanism.

## Security limits and unresolved observations

The unit templates use systemd credentials, a dedicated account, restrictive
umask, a private state directory, read-only system paths, and restricted
address families. These settings need validation against the target Ubuntu and
systemd version before installation. Backup files contain Discord IDs,
character paths, and public profile snapshots; keep the state directory private
and encrypt off-host copies. SQLite corruption requires restoring a verified
copy; do not use ad-hoc edits or copy a live database with ordinary `cp`.

The process lock protects against duplicate updated bot instances on one local
filesystem using flock. Canonical paths and hard-link aliases are covered by
the sidecar and database-inode locks. Multi-host filesystems may not provide
the required flock semantics and are not supported. The service unit does not
manage unrelated/manual bot processes that ignore these locks.

TASK 013C's observation that normal-runtime Armory INFO output was not yet
verified remains open until journald output is inspected on the production
host. Offline tests confirm the project handler emits INFO, but tests do not
prove normal service capture. discord.py/Python 3.14 deprecation warnings also
remain and should be reviewed when the upstream APIs are updated.

## TASK 016B review record

The independent hardening pass identified and addressed these implementation
gaps:

- A path-only process lock could be bypassed through database path aliases.
  Startup and restore now combine a canonical sidecar lock with `flock` on the
  database inode; tests cover symlink paths and a second process using a hard
  link. The database file is restricted to mode `0600`.
- The pre-existing manual bot does not acquire either new lock. The migration
  runbook now requires identifying it without printing its environment,
  matching its database path to the launch record, stopping it gracefully,
  and verifying it exited before starting the service or restoring.
- SQLite online backup may repeatedly restart if writes commit during the
  snapshot. Backup now takes a short `BEGIN IMMEDIATE` reservation, allowing
  readers while later writes wait. A real SQLite writer-thread test verifies
  that backup completes with consistent data while a writer is active.
- Opening a missing SQLite path for a lock could create an empty file before
  backup. Backup now checks source existence first, with a regression test.
- Restore previously could not proceed when the current database was corrupt.
  It now preserves raw database and residual sidecar bytes as a clearly named
  forensic copy, then installs only a verified backup. Healthy databases get
  a verified SQLite rollback copy that tests restore and integrity-check.
- Interrupted backup/restore attempts could leave private temporary files.
  Under the relevant locks, the next run removes only the tool's strict temp
  filename patterns; a restore marker still blocks automatic cleanup and bot
  startup where state is uncertain.
- The credential-file path previously accepted broadly readable files.
  Startup now requires a bounded, regular file with no group/other permissions
  and rejects symlinks where supported. Tests verify mode rejection. Backup,
  raw preservation, rollback, lock, and temp files are covered with restrictive
  permissions or scoped ownership.

Systemd templates passed syntax verification on temporary copies. The test
substituted the future release interpreter path because that path is not
installed in this development environment. Full verification against the
target Ubuntu host, actual account ownership, and credential delivery remains
part of the approved deployment review.

## v2.0.0 release installation gate

`deploy/production_install.py` is an administrator-only, fail-closed installer.
It requires the full approved release SHA and checks that it exactly matches
the clean checkout's HEAD on `release/v2.0.0`. It also verifies the release
descends from the reviewed v1.1.4 graphics checkpoint
`a1a6a2ad95302f7e3c4900f18e91691f43631e18`. It builds
the versioned release from that exact SHA. For an existing installation,
`--upgrade-existing` validates the installed config and credential without
rewriting them, requires installed runtime files to match the checkout, verifies
a read-only database snapshot, and atomically switches `current`. It does not
migrate or write the database, reinstall systemd files, or enable, start, or
restart services. It refuses to replace an unmanaged or unexpected `current`
release and fails if the target units are not stopped and disabled.

The version-controlled wrapper source is `deploy/production_wrapper.py`. It
loads `release_sha`, `previous_sha`, and `installer_sha256` from
`/etc/wow-forever-armory-bot/deployment-pin.json`, which must be a root-owned
regular file with mode `0600`. Create that pin only after the v2.0.0 commit and
the installed helper hash have been independently reviewed. Do not put the
Discord token or other secrets in the pin. The wrapper sets `umask 077` for
its audit files; the installer temporarily uses `umask 022` only while building
the release and restores `077` before returning.

The project owner confirmed PROD Guild ID `1549012880215638086` for initial
deployment verification. The runtime, installer, and systemd preflight are not
pinned to that guild. Production configuration must omit
`DISCORD_TEST_GUILD_ID`; production mode globally syncs application commands
even if a legacy test-guild variable remains in the process environment,
allowing every authorized guild where the application is installed
to use them. Global command changes may take time to propagate. The config source must be a
root-owned private regular file. The credential source must be root-owned mode
`0600`; the installer copies it without displaying its contents. Do not store
the token in the EnvironmentFile. The systemd credential is passed through the
stable launcher as `DISCORD_BOT_TOKEN_FILE`; the token value is not copied into
the bot process environment. A rollback candidate must support this file-based
credential interface; do not roll back to a launcher/application combination
that expects only `DISCORD_BOT_TOKEN`.

The release locks runtime and build dependencies in
`deploy/requirements-runtime.lock` and `deploy/requirements-build.lock` with
artifact hashes. They target CPython 3.14 on Linux x86_64. The installer checks
the interpreter and platform before creating a release, then installs only
hash-verified dependencies and builds the package without an unpinned isolated
build environment. Do not widen the supported Python/platform target without
generating and reviewing matching artifact hashes.

The installer validates Python sources by parsing them with `-I -B`; it must
not use `compileall` in an immutable release because that command writes
bytecode caches. The bot and backup unit set
`PYTHONDONTWRITEBYTECODE=1`. The protected deployment checkout must contain no
`__pycache__`, `.pyc`, or `.pyo` files; both preflight and the deployment
wrapper reject such files even when Git ignores them. Run Python commands that read or import checkout modules
with `-B` (or `PYTHONDONTWRITEBYTECODE=1`) so a diagnostic command cannot
dirty the protected checkout.

The official logo is optional and separately provisioned. If its rights are
confirmed, install it at
`/etc/wow-forever-armory-bot/wow_forever_logo.png`, owned by `root:wow-armory`
with mode `0640`, and set `ARMORY_LOGO_PATH` in the non-secret config. The
installer checks the path, ownership, mode, and service-account readability.
If the path is omitted, rendering leaves the logo region blank.

The installer refuses an existing versioned release directory without a valid
completion marker. It preserves that directory for operator review; inspect it
and quarantine it manually under an approved maintenance procedure before any
retry. It never recursively removes incomplete release content.

The unit runs `armory-db-preflight` (installed from
`deploy/systemd/armory-db-preflight.py`) with systemd's `+` privilege prefix
before the launcher. This short, root-owned helper needs host process visibility to
detect the old manual bot; it prints no environment values. It refuses to
continue if another Armory module process exists or process inspection fails.
It opens the configured database read-only and refuses a missing, zero-byte, invalid, uninitialized, or
restore-gated database. It requires the Guild Armory tables and at least one
registered guild. This prevents first start from creating an empty database
in place of migrated data.

Example invocation (configuration-changing; not run):

```bash
sudo /usr/bin/python3 \
  /home/codex-dev/projects/wow-forever-armory-bot/deploy/production_install.py \
  --release-sha <approved-full-commit-sha> \
  --config-source /run/armory-install/config \
  --credential-source /run/armory-install/discord-token
```

Stage the non-secret config from `deploy/systemd/armory-config.example`. It
contains no guild pin. Read the token through a hidden prompt; never
put it in a command argument, shell history, or log:

```bash
sudo install -d -o root -g root -m 0700 /run/armory-install
sudo install -o root -g root -m 0600 deploy/systemd/armory-config.example /run/armory-install/config
sudo /bin/bash -c 'umask 077; read -r -s -p "Discord token: " token; printf "\n" >&2; printf "%s\n" "$token" > /run/armory-install/discord-token; unset token'
```

After verifying installation, remove the temporary credential source from
`/run/armory-install`. The installed root-owned credential remains at
`/etc/wow-forever-armory-bot/discord-token` for systemd `LoadCredential`.

The script installs systemd files and runs `daemon-reload`, but intentionally
does not enable or start the service or timer. Verify installed paths and
ownership afterward. The source checkout must be clean and its HEAD must equal
`--release-sha`; use the full SHA from the reviewed and pushed checkpoint.
These scripts require a separately approved installation before they can be
run.

### Legacy process inspection and database import

Before the maintenance window, record the legacy process identity, working
directory and database path while it is running. The read-only command below
prints the selected database path and whether a credential variable exists.
It reports whether the running process has a test-guild override and whether
it matches an optional expected value; it never prints environment contents or
token values. The expected-guild comparison is observational only and is not
an installation or startup gate:

```bash
/home/codex-dev/projects/wow-forever-armory-bot/.venv/bin/python \
  /home/codex-dev/projects/wow-forever-armory-bot/deploy/production_migration.py \
  --inspect-legacy --expected-guild-id 1549012880215638086
```

After a separately approved graceful stop, run the migration script as root
with both the recorded source path and the same path explicitly confirmed. It
refuses to proceed if an Armory module process is running, the paths differ,
the source is invalid, or the StateDirectory database already exists. It holds
the updated bot's runtime lock and leaves the startup restore marker throughout
the import. It creates a final online SQLite backup in a dedicated migration
backup directory, checks integrity and aggregate guild/member/character
counts, then atomically publishes the verified copy. The original database
and pre-existing backups remain in place. A failed or interrupted import
leaves the marker so startup remains blocked pending operator review.

Example migration command (writes StateDirectory and backup files; not run):

```bash
sudo /opt/wow-forever-armory-bot/current/.venv/bin/python \
  /home/codex-dev/projects/wow-forever-armory-bot/deploy/production_migration.py \
  --source-database /home/codex-dev/projects/wow-forever-armory-bot/data/guild-armory.sqlite3 \
  --expected-source-database /home/codex-dev/projects/wow-forever-armory-bot/data/guild-armory.sqlite3
```

The expected source path must come from pre-stop inspection, not a guess. Keep
the service disabled and stopped until migration output, database ownership,
permissions and preflight have been checked. Do not remove a restore marker or
replace the StateDirectory database automatically after a failed import.

For rollback, stop the new unit, preserve its current database and backup,
switch the release pointer back, and use the current DB if its schema is
compatible. Do not restore the pre-migration snapshot over registrations made
after cutover unless that data loss is explicitly accepted. For a v1.1.4
zero-guild code rollback, follow the code/preflight rollback instructions above;
the legacy migration rollback procedure does not authorize restoring an older
startup preflight.
