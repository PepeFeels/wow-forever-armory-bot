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

Do not place Discord command-sync options or approval values in the production
EnvironmentFile. Normal startup never synchronizes global commands.

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
availability. The isolated installer snapshot verification accepts a
correctly initialized database with zero guild rows, members, and characters;
it rejects an absent, zero-byte, uninitialized, corrupt, or restore-gated
database and retains its source ownership, permissions, WAL/SHM consistency,
signature, and isolation checks. The systemd startup gate checks database file
and sidecar metadata plus the restore marker only; it never opens SQLite or
reads WAL/SHM contents. Database integrity must be checked by the isolated
snapshot/recovery procedure before an approved activation. No migration
command is part of this upgrade path.

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

For the reviewed source in this checkout, the standalone preflight SHA-256 is
`4d525f3c61cb28467c738b2fc985fe3b242c8366b248a3f47167916fc9407bac`. Compare
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

If the shared development checkout contains untracked review artifacts, do
not delete or move them to satisfy the clean-worktree check. Create a fresh
clone of the reviewed branch first. A local clone copies committed Git data,
not untracked files. Keep the clone and bundle until review and staging finish.

```bash
set -eu
umask 077
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 GIT_TERMINAL_PROMPT=0
SOURCE_REPO='/home/codex-dev/projects/wow-forever-armory-bot-v2-candidate'
CLEAN_REPO='/tmp/wow-armory-v2-bundle-source'
RELEASE_SHA='FINAL_REVIEWED_COMMIT'
BRANCH='release/v2.0.0'
BUNDLE='/home/codex-dev/wow-armory-v2.0.0.bundle'
GIT='/usr/bin/git'

test ! -e "$CLEAN_REPO"
test ! -L "$CLEAN_REPO"
test ! -e "$BUNDLE"
test ! -L "$BUNDLE"
"$GIT" -c core.fsmonitor=false -c core.hooksPath=/dev/null \
  clone --no-local --single-branch --branch "$BRANCH" "$SOURCE_REPO" "$CLEAN_REPO"
REPO="$CLEAN_REPO"
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
hash (`4d525f3c61cb28467c738b2fc985fe3b242c8366b248a3f47167916fc9407bac`).
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
SCRIPT_SHA256='4d525f3c61cb28467c738b2fc985fe3b242c8366b248a3f47167916fc9407bac'
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

#### Install a staged, cache-free deployment checkout

The bundle staging commands above prepare a preflight candidate under
`/var/lib/armory-preflight-staging`; they do not replace the fixed deployment
checkout. The v2.0.0 preflight and wrapper require
`/opt/wow-forever-armory-bot/deployment-checkout` itself to be root-owned,
write-protected, clean, and free of symlinks and Python bytecode caches. Keep
the existing v1.1.4 checkout, including its `.pyc` files, as evidence. Do not
run this replacement until the incident has been reviewed and acknowledged,
the staged bundle and candidate have passed their checks, and a separate
administrator change approval is in place. Do not run any wrapper during the
two-rename interval.

Copy the verified candidate to a temporary directory on the `/opt` filesystem.
Verify that temporary checkout before moving the existing checkout. The
versioned archive name must be unused. `mv -n` prevents replacement; every move
is followed by checks that detect a skipped move. If a command fails, stop and
preserve both directories for administrator review.

```bash
set -euo pipefail
RELEASE_SHA='FINAL_REVIEWED_COMMIT'
BASE='/opt/wow-forever-armory-bot'
STAGED="/var/lib/armory-preflight-staging/$RELEASE_SHA/candidate"
NEW="$BASE/.deployment-checkout-v2-$RELEASE_SHA"
OLD="$BASE/deployment-checkout"
ARCHIVE="$BASE/deployment-checkout-v1.1.4-a1a6a2ad95302f7e3c4900f18e91691f43631e18"

sudo /usr/bin/test -d "$STAGED"
sudo /usr/bin/test ! -L "$STAGED"
sudo /usr/bin/test ! -e "$NEW"
sudo /usr/bin/test ! -L "$NEW"
sudo /usr/bin/test ! -e "$ARCHIVE"
sudo /usr/bin/test ! -L "$ARCHIVE"
sudo /usr/bin/test -d "$OLD"
sudo /usr/bin/test ! -L "$OLD"
sudo /usr/bin/cp -R --no-preserve=ownership "$STAGED" "$NEW"
sudo /usr/bin/chown -R root:root "$NEW"
sudo /usr/bin/chmod -R a-w "$NEW"

root_git() {
  REPO_PATH="$1"
  shift
  sudo /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git -c safe.directory="$REPO_PATH" -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c credential.helper= \
    -C "$REPO_PATH" "$@"
}

test -z "$(sudo /usr/bin/find "$NEW" -xdev -type l -print -quit)"
test -z "$(sudo /usr/bin/find "$NEW" -xdev \
  \( -type d -name __pycache__ -o \
     -type f \( -name '*.pyc' -o -name '*.pyo' \) \) -print -quit)"
test -z "$(sudo /usr/bin/find "$NEW" -xdev ! -user root -print -quit)"
test -z "$(sudo /usr/bin/find "$NEW" -xdev -perm /022 -print -quit)"
[[ "$(root_git "$NEW" branch --show-current)" = release/v2.0.0 ]]
[[ "$(root_git "$NEW" rev-parse HEAD)" = "$RELEASE_SHA" ]]
test -z "$(root_git "$NEW" status --porcelain --untracked-files=all)"
[[ "$(root_git "$OLD" branch --show-current)" = release/v1.1.4 ]]
[[ "$(root_git "$OLD" rev-parse HEAD)" = \
  a1a6a2ad95302f7e3c4900f18e91691f43631e18 ]]

sudo /usr/bin/mv -T -n -- "$OLD" "$ARCHIVE"
sudo /usr/bin/test ! -e "$OLD"
sudo /usr/bin/test -d "$ARCHIVE"
sudo /usr/bin/mv -T -n -- "$NEW" "$OLD"
sudo /usr/bin/test ! -e "$NEW"
sudo /usr/bin/test -d "$OLD"
[[ "$(root_git "$OLD" rev-parse HEAD)" = "$RELEASE_SHA" ]]
test -z "$(root_git "$OLD" status --porcelain --untracked-files=all)"
```

The old checkout is retained byte-for-byte at `ARCHIVE`; its cache files are
not copied into the v2.0.0 checkout and are not deleted. This replacement
changes a production path and disables the old wrapper's expected checkout
identity. It requires root and a distinct provisioning approval. If either
rename is interrupted, leave services alone and have an administrator restore
the path arrangement after checking both directories and their Git identities.

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

The deployment wrapper records the database signature after managed services
have stopped and before the installer starts. If recovery later finds changed
`mtime_ns` or `ctime_ns`, preserve the original incident signature; do not edit
the journal. A timestamp difference alone does not identify changed rows. A
`member_image_updated` log event is emitted after `set_row_message` updates a
member row, so an event at the database-change time is evidence of a likely
legitimate application write. It is not a row-level audit or proof that every
change was legitimate.

When the database and its `-wal`/`-shm` sidecars exist, do not run SQLite
integrity checks directly against the original files. SQLite may recover or
update sidecar state. Use the hash-verified `production_db_snapshot.py` reader:
it opens the originals read-only with `O_NOATIME` and without following
symlinks, copies the main database and WAL into a private temporary directory,
validates the WAL, runs SQLite integrity/schema checks on that copy, checks
that the original sources did not change, then removes only the temporary
copy. It refuses to run on a platform without `O_NOATIME`. This includes
committed WAL data without changing source contents or access times. A
standalone copy of only the main database is not sufficient evidence while a
WAL exists.

The previously staged and reviewed snapshot source had SHA-256
`f99f098ff369b70a5b543f53795166fb7f9e12dd0af7fbb999c11f64e89d4a4b`. The
TASK-198X development hardening adds mandatory `O_NOATIME` and currently has
SHA-256 `95fe1ba3d9c9aaae3c3aa4459fa5e1185afded05bd6f5f5bacf7a7db287fb538`.
These are different source versions. Do not execute the new source on a
production host until its change is independently reviewed and copied to a
root-owned protected path with that exact hash. The existing reviewed copy
does not provide the new no-atime guarantee.

If the current snapshot passes integrity, preserve the current database as the
database of record for a code-only installer failure. Do not restore the older
incident backup merely to return to pre-deployment rows; that could discard
legitimate later updates. Compare only aggregate table counts or a separately
reviewed privacy-preserving difference report. Do not put character names,
user identifiers, profile data, or row values in the incident report.

The original journal signature remains immutable. Record the new database and
sidecar signatures and SHA-256 values in a separate root-owned, no-overwrite
recovery assessment bound to the journal SHA-256. Include snapshot integrity,
backup hash/integrity, the relevant log event reference, UTC time, administrator,
and approval reference. An independent administrator must review the addendum
before recovery. The current acknowledgment implementation requires the live
database signature to equal the original journal signature; a changed-signature
incident therefore remains blocked until a separately reviewed implementation
can validate such an addendum. Never edit the original journal or manifest to
make their saved signature match the current database.

### Read-only preflight before manual recovery

**TASK 289:** the recovery text below describes the earlier generic workflow.
For the archived TASK 267 incident and the v2.0.0 release candidate, follow
[TASK-289-PROD-DEPLOY-RUNBOOK.md](TASK-289-PROD-DEPLOY-RUNBOOK.md) and its
hash-bound review, addendum, and Recovery-ACK rules. TASK 289 is DEV-only and
does not authorize PROD execution.

The wrapper deliberately does not switch `current` back automatically after a
failure once shutdown has started. It leaves both managed units stopped and
disabled and records `manual_recovery_required`. Before an administrator
chooses a recovery action, run the checks below. They inspect the journal, pin,
manifest, current pointer, previous release, database signature, backup hash,
and preserved preflight. They do not clear or rewrite audit files, change the
pointer, restore a database, or change service state. The SQLite snapshot check
uses a temporary private copy under `/tmp` and removes only that temporary
copy.

Run this metadata check only from a separately reviewed root-owned copy and
after explicit administrative approval. It accepts `current` pointing either
to the previous release or to v2.0.0, covering a failure after the installer
has already switched the pointer. The old incident is bound to the v1.1.4
wrapper's embedded release identities, not to a future v2.0.0 pin. The check
therefore verifies the exact old wrapper and installer hashes and uses those
reviewed identities. Do not create the v2.0.0 pin until the incident is
resolved and acknowledged.

Replace `<FINAL_REVIEWED_SHA>` below with the commit-specific root-owned
candidate staging path. The candidate must have passed bundle and tree checks.
The snapshot module is read as bounded source bytes, checked against its
reviewed SHA-256, then compiled directly in memory. This avoids importing the
old checkout's `.pyc` files. The root command first scans the staged candidate
for symlinks and Python caches. It then reads `production_admin.py` as a
single-link regular file through a no-follow file descriptor, checks root
ownership, protected permissions, stable metadata, and its exact SHA-256, and
compiles those bytes in memory. It does not import any candidate module through
Python's normal import machinery. `production_admin.py` uses only Python
standard-library modules; no local project module is imported before the
snapshot source has also been verified. The snapshot verifier still creates and removes a
private temporary SQLite copy under `/tmp`; this does not alter the production
database or backup, but that temporary write must be included in the separate
administrative approval. The check stops on any mismatch:

```bash
sudo /usr/bin/python3.14 -I -B - <<'PY'
import grp, hashlib, json, os, pwd, re, stat, types
from pathlib import Path

root = Path("/var/lib/armory-deployment-audit")
journal_path = root / "deployment-state.json"
base = Path("/opt/wow-forever-armory-bot")
releases = base / "releases"
current = base / "current"
state = Path("/var/lib/wow-forever-armory-bot")
database = state / "guild-armory.sqlite3"
backups = state / "backups"
legacy_wrapper = Path("/usr/local/sbin/armory-deploy-a1a6a2a")
legacy_installer = base / "bin/production_install.py"
reviewed_candidate = Path(
    "/var/lib/armory-preflight-staging/<FINAL_REVIEWED_SHA>/candidate"
)
ADMIN_SHA256 = "61c159173e3139f1fda44a5e303399511a772513a17c80e31ec1f93cba152149"

def load_source(path, expected_sha256):
    assert path.is_absolute() and path.resolve(strict=True) == path
    parent = path.parent
    parent_info = parent.lstat()
    assert stat.S_ISDIR(parent_info.st_mode) and parent_info.st_uid == 0
    assert not parent_info.st_mode & 0o222
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened_parent = os.fstat(directory_fd)
        assert (opened_parent.st_dev, opened_parent.st_ino) == (
            parent_info.st_dev, parent_info.st_ino
        )
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            assert stat.S_ISREG(before.st_mode) and before.st_uid == 0
            assert not before.st_mode & 0o222 and before.st_nlink == 1
            assert before.st_size <= 1_048_576
            chunks = []
            while True:
                block = os.read(fd, 65_536)
                if not block:
                    break
                chunks.append(block)
                assert sum(map(len, chunks)) <= 1_048_576
            raw = b"".join(chunks)
            after = os.fstat(fd)
            entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            signature = lambda info: (
                info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                info.st_gid, info.st_nlink, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns,
            )
            assert signature(before) == signature(after) == signature(entry)
            assert len(raw) == after.st_size
        finally:
            os.close(fd)
        assert hashlib.sha256(raw).hexdigest() == expected_sha256
        assert (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino) == (
            parent.lstat().st_dev, parent.lstat().st_ino
        )
        module = types.ModuleType("_reviewed_production_admin")
        module.__file__ = str(path)
        exec(compile(raw.decode("utf-8"), str(path), "exec", dont_inherit=True), module.__dict__)
        return module
    finally:
        os.close(directory_fd)

candidate_info = reviewed_candidate.lstat()
assert stat.S_ISDIR(candidate_info.st_mode) and candidate_info.st_uid == 0
assert not candidate_info.st_mode & 0o222
for directory, names, files in os.walk(reviewed_candidate, followlinks=False):
    assert Path(directory).name != "__pycache__"
    for item in [Path(directory), *(Path(directory) / name for name in names + files)]:
        info = item.lstat()
        assert info.st_uid == 0 and not info.st_mode & 0o222
        assert stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        assert not stat.S_ISLNK(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            assert info.st_nlink == 1
    assert not any(name.endswith((".pyc", ".pyo")) for name in files)
admin_module = load_source(
    reviewed_candidate / "deploy/production_admin.py", ADMIN_SHA256
)

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

service_uid = pwd.getpwnam("wow-armory").pw_uid
service_gid = grp.getgrnam("wow-armory").gr_gid

def verify_release(path):
    admin_module._verify_release_tree(
        path, service_uid=service_uid, service_gid=service_gid
    )

regular(legacy_wrapper, 0, 0, 0o755)
assert digest(legacy_wrapper) == "ef1bba2bc30f0748cad943040109a1bc0838df07b6430ec5e5a6988577655c51"
regular(legacy_installer, 0, 0, 0o755)
assert digest(legacy_installer) == "57a25089cd62cee224a6c5260fba53874969409132b8ea758716f64b03c233dc"
journal = read_private_json(journal_path, 0, 0o600)
approved = "a1a6a2ad95302f7e3c4900f18e91691f43631e18"
previous = "dcceeb85464e09e34b112e41b16a833226c72270"
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

snapshot_module = admin_module.load_reviewed_python_source(
    reviewed_candidate / "deploy/production_db_snapshot.py",
    expected_sha256="f99f098ff369b70a5b543f53795166fb7f9e12dd0af7fbb999c11f64e89d4a4b",
    owner_uid=0,
)
snapshot_module.verify_database_snapshot(
    database, expected_owner=service_uid, expected_group=service_gid
)
snapshot_module.verify_database_snapshot(
    backup, expected_owner=service_uid, expected_group=service_gid
)
print(f"Metadata preflight passed; phase={journal['phase']}; current={target.name}; previous={previous}")
PY
```

Then use the previous release's installed Python to run its read-only SQLite
integrity check against both the live database and the verified backup. Replace
`<PREVIOUS_SHA>` with the value checked against the pin above:

```bash
sudo -u wow-armory /opt/wow-forever-armory-bot/releases/<PREVIOUS_SHA>/.venv/bin/python -I -B -m wow_forever_armory.operations check --database /var/lib/wow-forever-armory-bot/guild-armory.sqlite3
sudo -u wow-armory /opt/wow-forever-armory-bot/releases/<PREVIOUS_SHA>/.venv/bin/python -I -B -m wow_forever_armory.operations check --database /var/lib/wow-forever-armory-bot/backups/active/<BACKUP_NAME>
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
   required owner and permissions. For the recorded v1.1.4 incident, use the
   release identities bound by the exact-hash-verified old wrapper; do not
   create or substitute the future v2.0.0 pin during recovery.
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
Never edit its JSON contents. The administrative tool preserves the journal's
exact bytes. It creates the archive with an atomic no-overwrite hard link,
verifies the archive bytes and hash, and only then removes the active name.
The archive, manifest, backup, release directories, and preflight copy remain
as audit evidence.

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

Use the reviewed `deploy/production_admin.py` tool for these writes; do not
hand-edit JSON, use `mv` alone, or use `install` to overwrite an existing
target. Before root execution, copy the exact reviewed file to a root-owned
non-writable path and verify its SHA-256 against the reviewed commit. For the
inline recovery check above, use its independent no-follow source loader and
the exact reviewed `ADMIN_SHA256`; do not import any module from the candidate
checkout. The admin tool has only standard-library imports. Pin creation and
acknowledgment use a locked directory descriptor, private exclusive temporary
files, file and directory `fsync`, read-back checks, and atomic no-replace
hard-link publication. Existing files and symlinks are never replaced.

Provision a v2.0.0 pin only after the prior incident has been resolved and
acknowledged, the installed v2.0.0 installer has been independently reviewed,
and the final release commit has been approved. The exact target is
`/etc/wow-forever-armory-bot/deployment-pin.json`; the parent must be root-owned
mode `0750`. The pin must be a regular root-owned `0600` file containing exactly
`release_sha`, `previous_sha`, and `installer_sha256`. The release and previous
values are full lowercase 40-character commit SHAs, must differ, and the
installer value is the exact SHA-256 of the installed helper. If the path
already exists, stop; this command does not rotate or replace pins.

```bash
sudo /usr/bin/python3.14 -I -B \
  <ROOT_OWNED_REVIEWED_COPY>/production_admin.py provision-pin \
  --release-sha <FINAL_REVIEWED_COMMIT> \
  --previous-sha <VERIFIED_PREVIOUS_RELEASE_SHA> \
  --installer-sha256 <VERIFIED_INSTALLED_INSTALLER_SHA256>
sudo /usr/bin/stat -c '%F %U:%G %a %s %n' \
  /etc/wow-forever-armory-bot/deployment-pin.json
sudo /usr/bin/sha256sum \
  /etc/wow-forever-armory-bot/deployment-pin.json \
  /opt/wow-forever-armory-bot/bin/production_install.py
```

For `acknowledge-recovery`, first preserve a root-owned `0600` review record at
`recovery-review-<journal-sha256>.json` in the audit directory. It must contain
the journal and manifest hashes, release identities, all of the following
checks marked `PASS`, observed release target and marker, database signature
and integrity result, backup path/hash/integrity, saved-preflight path/hash,
all three live unit states, review completion time, administrator, approval
reference, and approval time. The tool checks that observations agree with the
journal and manifest, that every check passed, and that live systemd queries
still show the required stopped states before archival, before active-journal
removal, and immediately before acknowledgment publication.

The checked review names are `release_pointer`, `installation_marker`,
`release_permissions`, `database_signature`, `database_integrity`,
`backup_metadata`, `backup_sha256`, `backup_integrity`, `manifest`,
`saved_preflight_sha256`, `bot_unit_state`, `backup_timer_state`, and
`backup_service_state`. Any `FAIL`, `UNKNOWN`, missing field, mismatched hash,
active unit, changed journal, unapproved symlink in either release tree,
unsafe owner/mode, hardlink, special file, or conflicting
archive/acknowledgment causes refusal. The only accepted release-tree symlinks
are the exact Python 3.14 venv links documented below; application-code links
and any other link target are rejected. The tool accepts only two safe journal states: an active
journal alone, or an active journal and an archive that have identical bytes
and the same inode, or the verified archive alone after a prior interruption.
It rehashes the journal and manifest, rereads the administrative review,
reruns the filesystem evidence checks, and queries unit states on every retry.
It then creates or verifies the archive, removes the active name only after
matching the inode, and publishes the acknowledgment without replacement.

If interrupted at any point, do not edit or remove files. After confirming the
original separate recovery approval remains valid, rerun the same
`acknowledge-recovery` command with the same journal hash and review record.
The retry revalidates all evidence and live unit states; it never treats the
presence of an archive alone as approval. It can continue from the active
journal, an identical active-plus-archive pair, or the archive alone. If an
active journal and any acknowledgment file are both present, the tool refuses
before changing files; this includes a journal restored after an earlier
acknowledgment, so an old approval cannot authorize deleting that journal. The
tool checks for this conflict again immediately before removing an active
journal. Only an archive without an active journal may use a matching existing
acknowledgment as an idempotent success, and only after fresh evidence checks.
A differing acknowledgment or any other contradictory artifact is a hard
refusal. An interrupted private temporary acknowledgment file is removed only
after its type, owner, mode, link count, and directory entry are checked, and
only after the evidence checks pass. Unrecognized or unsafe temporary files
require manual administrator investigation; never delete them to force a retry.
An interrupted private temporary acknowledgment file is removed only after
its type, owner, mode, link count, and directory entry are checked, and only
after the evidence checks pass. Unrecognized or unsafe temporary files require
manual administrator investigation; never delete them to force a retry.

The review JSON has exactly these top-level fields: `schema_version` (integer
`1`), `journal_sha256`, `manifest_sha256`, `approved_release_sha`,
`previous_release_sha`, `checks`, `observations`, `completed_at_utc`,
`approved_by`, `approval_reference`, and `approved_at_utc`. `checks` must map
exactly the 13 names above to `PASS`. `observations` must contain exactly
`release_target`, `installation_marker`, `database_signature`,
`database_integrity`, `backup_path`, `backup_sha256`, `backup_integrity`,
`saved_preflight_path`, `saved_preflight_sha256`, and `systemd_states`.
Database and backup integrity values must be `ok`; the recorded signature,
backup path/hash, and saved-preflight path/hash must match the active journal.
The recorded release target and marker must be one of the two journal-bound
releases. `systemd_states` records the active and enabled state for the bot,
backup timer, and backup service. The tool rechecks the live pointer, marker,
release permissions, database signature, backup hash, preflight hash, and live
systemd states before archiving. It does not rerun SQLite integrity; that result
must come from the separately performed, approved recovery check.

```bash
sudo /usr/bin/python3.14 -I -B \
  <ROOT_OWNED_REVIEWED_COPY>/production_admin.py acknowledge-recovery \
  --journal-sha256 <REVIEWED_ACTIVE_JOURNAL_SHA256> \
  --review-record \
  /var/lib/armory-deployment-audit/recovery-review-<JOURNAL_SHA256>.json \
  --approved-by '<APPROVING_ADMINISTRATOR>' \
  --approval-reference '<SEPARATELY_APPROVED_RECORD>'
```

The review record is an administrative attestation, not a cryptographic
signature. The command requires root and a separate recovery approval. It
queries systemd but never changes service state, database contents, release
pointer, or backup files. Its filesystem writes are limited to an archive
hard-link, removal of the active journal name after verification, private
temporary acknowledgment data, and the final acknowledgment file; it fsyncs
the relevant files and audit directory. If recovery is not fully verified, it
stops and leaves existing evidence untouched except for a safely resumable
archive step already completed.

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

The root-run `armory-db-preflight` and the deployment preflight/wrapper use the
same `deploy/recovery_audit.py` validator. The installer places a root-owned
copy at `/opt/wow-forever-armory-bot/bin/recovery_audit.py`; each caller checks
its SHA-256 before compiling those source bytes in memory, so Python bytecode is
not used. Production preflight also compares the installed helper's hash with
the protected checkout. The validator checks active and temporary journals,
archive and acknowledgment pairing, the exact journal/manifest links, backup
and saved-preflight identities, fail-closed fields, acknowledgment decision,
approval-reference format, timestamps, hashes, and orphan or contradictory
manifests. Only `phase=complete` with a matching completion acknowledgment or
`phase=manual_recovery_required` with verified fail-closed evidence and a
matching manual-recovery acknowledgment can pass. It never changes audit
contents. Once this preflight and its matching validator are installed, a
manual systemd start is blocked by unresolved or incomplete evidence. The old v1.1.4
production copy does not gain this development change retroactively; keep the
current bot and backup timer stopped and disabled until the incident is
reviewed. `fail_closed_verified` records only the wrapper's observation at
failure time; it is not a permanent lock against a later administrator or
another process starting the bot.

The audit check opens the protected directory without following a link,
requires a nonblocking shared `flock`, reads each JSON file through a
no-follow descriptor, and compares the names and file identities/metadata
before and after validation. Administrative recovery takes the corresponding
exclusive directory lock. The deployment wrapper keeps its exclusive lock
from retained-history validation through installation verification and process
exit. Its active journal continues to block every service start after the lock
is released. Administrative completion uses the same exclusive lock; service
activation holds a shared audit lock so systemd's read-only start gate can also
obtain a shared lock. The additional activation mutex under `/run/lock`
serializes activation attempts. A privileged writer that ignores these locks
is outside the coordination guarantee; protected directory ownership and
before/after identity checks detect ordinary races and fail closed, but do not
provide an absolute filesystem snapshot against a hostile privileged process.

### Administrative deployment completion and service activation

The deployment wrapper does not start or re-enable services. A successful
installer and release verification produces an immutable
`deployment-installation-<deployment-id>.json` report containing the release,
pin/installer identity, manifest and backup/preflight hashes, database
signature, the policy target (`active` and `enabled` for the bot and backup
timer), observed stopped states, and UTC verification time. It accepts either
the matching initial pair (`inactive`/`disabled` for both units, as after
incident recovery, or `active`/`enabled` for both) and rejects mixed states.
It records the fixed target independently from the initial state, so beginning
from the stopped state never causes an early start. The wrapper publishes that report with exclusive
no-overwrite creation and file/directory `fsync`, then changes the active
journal to `phase=installation_verified`. This phase, like every other active
journal phase including `complete`, blocks systemd startup. It is not an
administrative completion or permission to start services. Both units remain
inactive and disabled until the separate activation stage.

An administrator must independently review the result and provide a specific
approval reference. The reviewed root-owned `production_admin.py` then runs
`finalize-deployment`. It takes the exclusive audit lock and rechecks the
installation result, pin, installer hash, current release symlink and marker,
release tree, manifest, backup and saved-preflight hashes, database signature
and SQLite integrity, backup SQLite integrity, and stopped systemd/process
state. The original journal, manifest, backup, and preflight are not edited.

The finalizer publishes the journal in this order:

1. Atomically replace the active `installation_verified` journal with a
   `complete` journal containing the administrator and approval reference.
2. Create a no-overwrite hard link named
   `deployment-state-<complete-journal-sha256>.json`, sync the directory, and
   verify the exact archived bytes.
3. Remove `deployment-state.json` and sync the directory.
4. Publish `deployment-recovery-ack-<complete-journal-sha256>.json` with
   no-overwrite semantics and sync it.
5. Run the shared validator while still holding the exclusive lock.

Until step 4 is complete and the final validator passes, startup remains
blocked: an active journal blocks directly, and an archive without its
matching completion acknowledgment fails history validation. Retry with the
same source-journal SHA, administrator, and approval reference. The operation
verifies existing archive/ack bytes and never replaces conflicting files.
An exact completed-journal temporary can be resumed after validation; an
inconsistent temporary, archive, or acknowledgment requires manual review and
NO-GO. Historical `manual_recovery_required` incidents cannot be passed to
`finalize-deployment`; they continue through the separate
`acknowledge-recovery` flow and recovery evidence.

Only after administrative completion may a separate approval invoke
`activate-services`, using the final archived journal SHA and a new activation
approval reference. The tool appends durable, no-overwrite JSON events to the
audit directory. Every event binds the deployment ID, release SHA, completed
journal SHA, completion-acknowledgment SHA, manifest SHA, installation-report
SHA, deployment-pin SHA, approver, approval reference, and saved bot/timer
states. Events record approval, each planned systemd action, each observed
result, and a final success or failure. Files are created exclusively and the
file and directory are synchronized before the operation proceeds.
The terminal success event is written only after all approved actions and
state checks pass. Console/journal reporting happens afterward; a broken
reporting pipe does not roll back services or append a contradictory failure.

The shared validator and systemd startup gate enforce the same event rules.
They deny startup if the active journal exists, the evidence conflicts, or
there is no matching completion and activation history. During activation, the
bot may start during activation only while its exact start action is the
currently pending durable action and the activation lock is held. Once that
action has been verified, further starts wait for the final success event. If the activation
process exits before success, that lock is released and a subsequent start is
denied until an administrator resumes activation with the same approval
reference. The backup timer's service may start only after the complete
activation success event. After success, systemd may perform ordinary future
restarts for the same completed release. A new active deployment journal
blocks those starts; a later deployment receives a different deployment ID,
release, and evidence chain and requires a new approval.

Activation first requires no active journal, a valid completion archive and
acknowledgment, the matching release pointer and installation marker, a
passing database readiness check, no unexpected bot process, and both managed
units stopped and disabled. It restores only the bot and timer states saved by
the wrapper. Before each systemd action it writes an action-planned event; after
the action it reads back unit state and verifies the bot process working
directory when starting the bot, then writes action-verified. It never starts
the backup job service directly. A failed action records failure when
possible, then attempts to stop and disable both managed units. If event
publication or state verification fails, the result is NO-GO and manual review
is required; an absent terminal success event is never treated as success.

An interrupted attempt can resume only with the same administrator and
approval reference while it is incomplete. A failed attempt is terminal and
requires a new approval reference and a new activation ID. A successful retry
is idempotent only when live unit states and the running release still match
the saved intent. The append-only history is retained; it is not rewritten by
retries. `finalize-deployment` never creates activation authorization or
starts services. Historical `manual_recovery_required` incidents remain on
the separate recovery path and cannot receive activation authorization.

The reviewed helper uses the pinned shared-validator SHA-256
`9201ffc0c36c0ed5119eb0af94a9bd2dadeb49b33525d7ca8162752ec317304e` and
snapshot-module SHA-256
`564fb40f430c9f96434ab03a3e008a631a7cb7e556f1f63b16f4e9357c2111c8` in this
development revision. The administrator must verify the exact reviewed
`production_admin.py` source hash before root execution; that hash is recorded
in the TASK-199D review section below. These development hashes are not
production authorization or permission to copy/install the helpers.

For future separately approved operations, the command forms are:

```bash
sudo /usr/bin/python3.14 -I -B \
  <ROOT_OWNED_REVIEWED_COPY>/production_admin.py finalize-deployment \
  --journal-sha256 <INSTALLATION_VERIFIED_JOURNAL_SHA256> \
  --approved-by <ADMINISTRATOR_ID> \
  --approval-reference <COMPLETION_APPROVAL_REFERENCE>

sudo /usr/bin/python3.14 -I -B \
  <ROOT_OWNED_REVIEWED_COPY>/production_admin.py activate-services \
  --journal-sha256 <FINAL_COMPLETE_ARCHIVE_SHA256> \
  --approved-by <ADMINISTRATOR_ID> \
  --approval-reference <SEPARATE_ACTIVATION_APPROVAL_REFERENCE>
```

The first command returns the final archive SHA, which is supplied to the
second command. Do not run either command if any hash, state, backup, marker,
release, process, or approval check fails. These command forms grant no
production approval.

The development wrapper now also scans `/proc` after stopping the managed
units. A remaining manually launched bot process or an unreadable process
inventory makes `fail_closed_verified` false. This detects a process outside
systemd that unit-state checks alone would miss; it does not terminate that
process. The systemd startup gate blocks ordinary service starts while
recovery evidence is pending. Directly launching the bot executable bypasses
that unit gate and remains prohibited during recovery.

#### TASK-199D development review hashes

These are the hashes recorded for the TASK-199D development source snapshot.
They do not authorize copying files to production or running an administrative
operation. TASK-200C changes are recorded separately below.

| File | SHA-256 |
| --- | --- |
| `deploy/production_admin.py` | `ce76868d9310708c27faacaf2c5358118d1423219a0e0e71a9ee1967b54c72f0` |
| `deploy/production_wrapper.py` | `2cb1b4125bceeca95b883e40b48435f7344f1bcaf25d55d534e0f1679062be86` |
| `deploy/recovery_audit.py` | `9201ffc0c36c0ed5119eb0af94a9bd2dadeb49b33525d7ca8162752ec317304e` |
| `deploy/production_preflight.py` | `841dfa37ab55d37cb962afbfd1389fdecec3d49385e37c01581f9544e6c2921b` |
| `deploy/systemd/armory-db-preflight.py` | `ec03ed66495ed89f5e30b973d6c6aac0f968d1052792c03efb0a09871b64317a` |
| `deploy/production_db_snapshot.py` | `78c59895f76834dd24d32ca7c744c001cae58b656b208c287da4ee0d27a2b313` |

#### TASK-200C current development review hashes

These hashes identify the locally modified development candidate only. They
are not release authorization and have not been transferred to production.
The administrator loader pin above must match `production_admin.py` exactly.

| File | SHA-256 |
| --- | --- |
| `deploy/production_admin.py` | `50ccda68ca6da1466e06fc299b1f805db866f8730aa1a5fc47b0434e4f83a27a` |
| `deploy/production_preflight.py` | `4d525f3c61cb28467c738b2fc985fe3b242c8366b248a3f47167916fc9407bac` |
| `deploy/systemd/armory-db-preflight.py` | `9567f17e24247470a3e12a5fb8a3d7e617f743ae707de533ff2f9d5ec233b4ec` |
| `deploy/production_db_snapshot.py` | `564fb40f430c9f96434ab03a3e008a631a7cb7e556f1f63b16f4e9357c2111c8` |
| `deploy/verify_production_backups.py` | `2125b228628cb2a4460a173a27436e4861b89d1d570ca560541a9942e49b9a0d` |
| `deploy/production_install.py` | `d5ca3bc5d48b2e9562831e1fa16c20c07c944ff85801b5e9ef987df5a329cca0` |
| `deploy/production_wrapper.py` | `2cb1b4125bceeca95b883e40b48435f7344f1bcaf25d55d534e0f1679062be86` |
| `deploy/recovery_audit.py` | `9201ffc0c36c0ed5119eb0af94a9bd2dadeb49b33525d7ca8162752ec317304e` |
| `deploy/systemd/wow-forever-armory-bot.service` | `e0b04e08957af6634cbd06bd5b55d621b1cfaced888723ba8a286cd9c38bd890` |
| `deploy/systemd/wow-forever-armory-backup.service` | `6b56dbb5bd6d47252bb7221f8dda304863a81c092b6f01d6192d2729588129d1` |

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
  ARMORY_BACKUP_DIR=/var/lib/wow-forever-armory-bot/backups/active \
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
BACKUP_FILE='/var/lib/wow-forever-armory-bot/backups/active/guild-armory-YYYYMMDDTHHMMSS.ffffffZ.sqlite3'
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

### Installer provenance and helper pin

The previously installed v1.1.4 helper is a patched standalone artifact, not a
byte-for-byte copy of the v1.1.4 checkout source. Its recorded SHA-256 is
`57a25089cd62cee224a6c5260fba53874969409132b8ea758716f64b03c233dc`; the
checkout source was `79e0b186467a29eb35d9aee5442107e411157728a0e6df057edb45b401bf38aa`.
The reviewed diff adds a fixed approved-checkout import path, a fixed source
repository for installation, and a restricted `installer_rejected` diagnostic
through the wrapper's inherited descriptor. Preserve the old binary and diff
as evidence; do not treat either old hash as approved for v2.0.0.

The TASK 222A draft snapshot of `deploy/production_install.py` has SHA-256
`d5ca3bc5d48b2e9562831e1fa16c20c07c944ff85801b5e9ef987df5a329cca0`.
This draft hash is not release authorization. Recompute and independently
review the hash from the final clean v2.0.0 commit before provisioning the
root-owned helper or deployment pin. The pin's
`installer_sha256` must equal both the reviewed source hash and installed
helper hash. The new wrapper is a separate root-owned file at
`/usr/local/sbin/armory-deploy-v2`; record its source and installed hashes in
the administrative review even though the pin schema has no wrapper-hash
field. Review sudoers separately and allow only the exact wrapper entry point.
Replacing the shared helper makes the old wrapper's embedded hash check fail;
perform this only after incident recovery and separate approval of the new
helper, pin, wrapper, and sudoers rule.

The version-controlled wrapper source is `deploy/production_wrapper.py`. It
loads `release_sha`, `previous_sha`, and `installer_sha256` from
`/etc/wow-forever-armory-bot/deployment-pin.json`, which must be a root-owned
regular file with mode `0600`. Create that pin only after the v2.0.0 commit and
the installed helper hash have been independently reviewed. Do not put the
Discord token or other secrets in the pin. The wrapper sets `umask 077` for
its audit files; the installer temporarily uses `umask 022` only while building
the release and restores `077` before returning.

The runtime, installer, and systemd preflight are not pinned to one guild.
Production configuration must omit `DISCORD_TEST_GUILD_ID`. Bot startup does
not synchronize global commands, so it cannot create, change, or remove them.
Global command changes require the separate reviewed workflow below. The config source must be a
root-owned private regular file. The credential source must be root-owned mode
`0600`; the installer copies it without displaying its contents. Do not store
the token in the EnvironmentFile. The systemd credential is passed through the
stable launcher as `DISCORD_BOT_TOKEN_FILE`; the token value is not copied into
the bot process environment. A rollback candidate must support this file-based
credential interface; do not roll back to a launcher/application combination
that expects only `DISCORD_BOT_TOKEN`.

### Separate global Discord command review and synchronization

Normal bot startup does not call `CommandTree.sync()` and cannot change global
commands. A one-shot operator must review the exact command diff before asking
for approval. The tool reads the existing token from the fixed root-owned
`/etc/wow-forever-armory-bot/discord-token` file; it does not accept a token in
arguments, environment variables, or output. Run it as root from the reviewed
release. A dry run authenticates over the REST API and fetches the current
global command set, but makes no Discord write:

```bash
sudo /opt/wow-forever-armory-bot/current/.venv/bin/python -I -B -m \
  wow_forever_armory.command_sync --mode dry-run
```

Record and independently review the `added`, `changed`, `removed`, and
`unchanged` entries and the full `diff_sha256`. A separately approved apply
must name that exact hash and a Product Owner decision reference:

```bash
sudo /opt/wow-forever-armory-bot/current/.venv/bin/python -I -B -m \
  wow_forever_armory.command_sync --mode apply \
  --approve-diff-sha256 '<REVIEWED_DIFF_SHA256>' \
  --approval-reference '<PO_SYNC_DECISION_REFERENCE>'
```

If and only if the reviewed diff includes removals, require an additional,
distinct Product Owner decision specifically approving those removals:

```bash
sudo /opt/wow-forever-armory-bot/current/.venv/bin/python -I -B -m \
  wow_forever_armory.command_sync --mode apply \
  --approve-diff-sha256 '<REVIEWED_DIFF_SHA256>' \
  --approval-reference '<PO_SYNC_DECISION_REFERENCE>' \
  --approve-deletions-sha256 '<SAME_REVIEWED_DIFF_SHA256>' \
  --deletion-approval-reference '<SEPARATE_PO_DELETION_DECISION_REFERENCE>'
```

Apply fetches the remote set again and refuses a stale diff before its single
sync request. It then fetches the remote set and compares it to the locally
registered commands. `APPLIED_AND_VERIFIED` means Discord returned the command
set matching the local definitions. `APPLY_UNVERIFIED` means the write may have
occurred but the result could not be verified; stop and inspect Discord's
current state before any retry. The tool never retries, and normal bot startup
does not repair a failed sync.

The release locks runtime and build dependencies in
`deploy/requirements-runtime.lock` and `deploy/requirements-build.lock` with
artifact hashes. They target CPython 3.14 on Linux x86_64. The installer checks
the interpreter and platform before creating a release, then installs only
hash-verified dependencies and builds the package without an unpinned isolated
build environment. Do not widen the supported Python/platform target without
generating and reviewing matching artifact hashes.

### Release tree and Python venv symlinks

The release tree verifier checks `.venv` as part of the release; it does not
skip the environment. It permits only these exact link texts:

| Link path | Required link text | Resolved target |
| --- | --- | --- |
| `.venv/lib64` | `lib` | `.venv/lib` inside the same release |
| `.venv/bin/python` | `python3.14` | `/usr/bin/python3.14` |
| `.venv/bin/python3` | `python3.14` | `/usr/bin/python3.14` |
| `.venv/bin/𝜋thon` | `python3.14` | `/usr/bin/python3.14` |
| `.venv/bin/python3.14` | `/usr/bin/python3.14` | The trusted system interpreter |

The verifier checks raw link text, resolved target, ownership, permissions,
and that the links do not change during inspection. The system interpreter
must be a root-owned regular file in a trusted directory chain and readable
and executable by the service account. All other symlinks, including links
inside application code, are rejected. Python 3.14's standard `venv` module
creates the `𝜋thon` alias when the filesystem encoding is UTF-8; its unusual
Unicode name is expected only at that exact path and with that exact target.
This explains the observed shape but is not independent proof of how the
production links were created.

The installer validates Python sources by parsing them with `-I -B`; it must
not use `compileall` in an immutable release because that command writes
bytecode caches. The bot and backup unit set
`PYTHONDONTWRITEBYTECODE=1`. The protected deployment checkout must contain no
`__pycache__`, `.pyc`, or `.pyo` files; both preflight and the deployment
wrapper reject such files even when Git ignores them. `-B` prevents writing new
bytecode but does not prevent Python from loading an existing `.pyc`. When a
privileged recovery check must execute reviewed source from a checkout, use
`load_reviewed_python_source` from the root-owned staged candidate: it opens a
single-link regular file without following symlinks, verifies stable metadata
and the reviewed source hash, and compiles those exact source bytes in memory.
Do not import a module from the old deployment checkout while its cache files
remain there. Use `-B` for reviewed Python commands so they do not create new
cache files.

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
before the launcher. The installer also copies the exact-hash-checked shared
`deploy/recovery_audit.py` validator to the same protected `bin` directory.
These files are installed or updated together; a missing, changed, linked, or
unsafe validator stops both deployment checks and service startup. This short,
root-owned helper needs host process visibility to
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
  --inspect-legacy --expected-guild-id REPLACE_WITH_CONFIRMED_PROD_GUILD_ID
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


## TASK 298 recovery audit history

The reviewed read-only preflight preserves the existing recovery records and
uses the exact hash-pinned release validator. The validator accepts only the
explicit historical directory names `reviewed-v2.0.0/` and `archive/` when the
caller opts in. `reviewed-v2.0.0/` must be root-owned mode `0500` and contain
only `production_db_snapshot.py` and `production_admin.py`, each root-owned
mode `0400`. `archive/` and its nested directories must be root-owned mode
`0700`; its regular JSON files must be root-owned mode `0600`. Their bytes are
hashed and reported as untrusted history, never parsed as recovery evidence.
Single-link checks, same-filesystem traversal, stable identities and
before/after snapshots remain required. Links, special files, unknown review
files, non-JSON archive files, unsafe permissions, unrecognized audit-root
entries, invalid active evidence, or changes during validation still fail
closed. Calls that do not opt in retain the original strict behavior.

TASK 280's earlier production inventory identifies one exact historical file
as `prod_observation_only`: `archive/pre-wrapper-fix-20261114T184045Z/armory-deploy-a1a6a2a`,
SHA-256 `cf4c6c4cc9219c9f6f84142502b0a4007b0d82c4812252dba9ab2f7ab0342a24`,
root:root, mode `0750`, one link, 43,813 bytes, mtime
`1794676808459758687` ns. The validator accepts this single exact historical
entry only if every recorded attribute and its content hash match. It opens
the object as a regular file with no-follow flags solely to hash and inventory
it as `untrusted-file`; it never executes, imports, parses or uses it as
recovery evidence. All other executable historical files remain rejected.

Mode `0750` still grants the owner execute permission and grants the group
read and execute permission. Group members could execute these bytes outside
the validator. The exception does not remove that underlying access risk; it
is narrowly constrained by the independently recorded TASK 280 identity,
content hash and metadata, and the data remains explicitly untrusted. Any
metadata, path, size, hash or link-count deviation fails closed. No permission
change is part of this procedure.

The installed PROD validator remains pinned to its prior SHA until a separately
approved release is installed. TASK 298 preflight verifies that installed hash,
then runs the candidate validator only after verifying the candidate bundle,
commit, tree and complete checkout. This read-only check does not install or
activate the candidate.

TASK 298 candidate hashes:

| File | SHA-256 |
| --- | --- |
| `deploy/recovery_audit.py` | `756155878fb49f902c0ebf15cdb5c18900da6e7715d21188e84c4aa39ace51fb` |
| `deploy/production_admin.py` | `adc1d661cdb2124c4311ddb5fae47af397810fa50799203ec300fb22947fc5bd` |
| `deploy/production_preflight.py` | `a7f6274f744b72d30cc8a7832e2a17775038ca043cd6010b7f12a8e01c8c3e14` |
| `deploy/production_wrapper.py` | `4cd83e9d6eeab7fd46c210837bf4f61cb6c676b4ea6bd6d965f7d3751c1342b6` |
| `deploy/systemd/armory-db-preflight.py` | `969dfafcf67b63bfe66e80b60af47f1982622c9173d7ab522c8e2114d6a53185` |

DEV permission-compatibility correction (not yet a release pin):
`deploy/production_preflight.py` SHA-256 is
`96aa614ed9e18144fc996da8e5d0f8ea00d3121fee60f759a2a9c05c4c5ed529` and
`deploy/production_wrapper.py` SHA-256 is
`503c5b1d6a556d7b53eedd62f8aae1c640588f28420975120f0a3b597745dc64`.
These working-tree hashes are review evidence, not release pins; no existing
release artifact or deployment pin is updated by this DEV change.

The corrected wrapper and read-only preflight enforce the same checkout trust
boundary: every parent is root-owned and not group/world-writable; root-owned
checkout directories may be owner-writable; ordinary files must be non-writable
and single-link; only `deploy/recovery_audit.py` may be `0444` or `0644`, and it
must be single-link and pass the pinned SHA-256 check before compilation. All
other hard-linked files, symlinks, special files, bytecode caches, and
group/world-writable entries fail closed. Before any Git worktree query, the
wrapper checks local Git setting names against the staging allowlist and runs
Git with system/global config disabled, replacement objects disabled, and
fsmonitor, hooks, credentials, symlink handling, and bare-repository behavior
overridden to safe values. The release branch, HEAD, clean worktree, and
required ancestry checks remain mandatory.

TASK 299Q4 adds an explicit Git metadata boundary before any branch, HEAD,
status, release identity, or ancestry query. The `.git` entry must be a real
directory inside the canonical checkout; its device and inode are recorded and
rechecked after validation. Git's exact child environment is fixed, so
`GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`, `GIT_OBJECT_DIRECTORY`, alternate
index paths, and other inherited Git variables cannot redirect a query. Local
Git config remains allowlisted, with worktree config extensions excluded.
Reject repositories containing `objects/info/alternates`,
`objects/info/http-alternates`, `info/grafts`, `shallow`, any `refs/replace`
directory, or any `refs/replace/*` record in `packed-refs`. Symlinks, hard
links, special files, and files or directories on a different filesystem
inside the checkout are rejected. `GIT_NO_REPLACE_OBJECTS=1` remains set for
every Git subprocess.

For local review, run the complete test suite and verify Git object integrity
from the candidate checkout. Then inspect the Git metadata directly; all
redirector paths must be absent, and packed refs must contain no replace-ref
names:

```bash
python3 -m pytest -p no:cacheprovider -q
git fsck --full --no-reflogs
test ! -e .git/objects/info/alternates
test ! -e .git/objects/info/http-alternates
test ! -e .git/info/grafts
test ! -e .git/shallow
test ! -e .git/refs/replace
! /usr/bin/grep -E '(^| )refs/replace/' .git/packed-refs
git status --short --branch
git rev-parse HEAD
git rev-parse HEAD^{tree}
sha256sum deploy/production_wrapper.py deploy/production_preflight.py
```

These checks and the generated pin/artifacts are local review evidence. They do
not provision a production pin, authorize publication, or authorize deployment.

For a corrected candidate, the root-owned wrapper at
`/usr/local/sbin/armory-deploy-v2` must be replaced with the reviewed wrapper
from that candidate, installed as `root:root` mode `0755`, and checked against
its recorded SHA-256 before it is invoked. Preserve and record the prior
wrapper bytes and hash for rollback.
The deployment checkout at
`/opt/wow-forever-armory-bot/deployment-checkout` must be replaced by the full,
clean checkout at the corrected candidate commit using the no-overwrite archive
procedure above; do not update it in place or run the wrapper during the
two-rename interval. Recompute the root-owned `0600` deployment pin with the
corrected release commit, the verified previous PROD release, and the unchanged
installer hash. The existing installer (`production_install.py`) and installed
recovery validator do not change for this correction; keep their pinned hashes
and metadata checks. Rebuild the release bundle, source distribution, wheel,
standalone preflight and manifests from the corrective commit. No old pin or
artifact hash authorizes these new bytes.

## TASK 298 recovery-attestation compatibility duplicate

The observed name `recovery-attestation-f16dd8394724b6e15797d9ac7a7f6f39241cb58dadf86b05347e2372b874e6cb.json`
contains the SHA-256 of the known TASK 267 incident journal. Its bytes have
been compared in PROD with the canonical
`task262-attestation-dc82c79862053f64f460e41cc18bca6a90308545d4a4ac56781c09628dde09c4.json`;
both have SHA-256
`dc82c79862053f64f460e41cc18bca6a90308545d4a4ac56781c09628dde09c4` and the
byte comparison was true.

The validator accepts only the exact legacy alias
`recovery-attestation-f16dd8394724b6e15797d9ac7a7f6f39241cb58dadf86b05347e2372b874e6cb.json`.
When present, it requires the canonical TASK 262 file alongside it, validates
both as regular root:root mode-`0600` single-link files with the existing
no-follow, stable-read checks, independently hashes both against the pinned
TASK 262 content SHA-256, and compares the bytes directly. The alias is only
compatibility material: it is not parsed, added to recovery records, or
counted as independent evidence. Other recovery-attestation names, a missing
canonical file, content/hash mismatch, unsafe metadata or a link fail closed.

## TASK-301 backup-verified recovery

`production_admin.py acknowledge-pre-shutdown-recovery` is restricted to the
hash-pinned TASK-301 journal interrupted at `backup_verified`. It rechecks the
live release pointer and marker, production database hash and isolated SQLite
snapshot, registered backup hash and snapshot, historical preflight hash,
stopped systemd units, and absence of an Armory process. It then preserves the
original journal bytes under the journal-hash archive name and writes the
administrative acknowledgment. It does not require a deployment manifest and
does not modify the database or backup. See `TASK-301-RECOVERY.md` for the
exact journal identity, command invocation, test evidence, and current GO/NO-GO.
The corrected deployment preflight source SHA-256 is
`58d1292fa948822cfd53797471e8778ab046dbe024cb830493cbe5d486ba5ecd`.
