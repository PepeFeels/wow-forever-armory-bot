# Character Overview Graphic Renderer

The member image workflow uses `graphic_renderer.py` to render one, two, or
three characters into fixed landscape PNGs (800×900, 1200×900, or 1800×900).
The Discord layer selects the already-authorized member state, uses the current
guild display name when it is cached, and offloads Pillow rendering from the
event loop. A small semaphore bounds concurrent renders. The existing per-guild
and per-user row-message ownership checks continue to control Discord edits.

## Data and missing fields

The renderer consumes normalized Core models and stored snapshots. The current
Core exposes character name, level, race, class, active specialization, spent
talent points, professions and the Armory update timestamp. It does not expose
a normalized surname, portrait/media asset, combat statistics, or PvP rank or
kill data. Until an independently verified source is approved, the graphic
shows a class-themed portrait placeholder, `No Stats Available`, and
`No PvP Data`. A missing profession skill or maximum is shown as `—`; no
surname is invented. The model leaves extension points for verified values,
but the Core scraper is unchanged.

Character snapshots are loaded from the existing guild/user-scoped database
records. No ownership decision is made by the renderer, no database schema is
changed, and the renderer accepts no user-provided filesystem path or remote
image URL. Optional future portrait bytes are decoded with format, byte, pixel,
and dimension bounds and fall back to the class placeholder on invalid data.
No remote image download is performed.

## Image content and freshness

The graphic contains the shared Discord display name and, per character,
identity, specialization/talent points, stats, primary professions, all three
secondary skill rows, PvP, and last-updated information. It excludes guild
names, item level, gear, equipment, and internal identifiers. Text is cleaned
for control and bidirectional override characters, bounded, and measured before
drawing. Stale Armory timestamps older than 30 days receive an `OUTDATED`
marker. The displayed timestamp is the source Armory timestamp in UTC, not
image-generation time.

Freshness markers follow application state. After a successful Armory fetch,
the newly submitted character path is marked `FRESH DATA` for that immediate
row render. Other characters from the persisted snapshot are marked
`CACHED DATA`; recovery and later management renders use cached state. An
unavailable source state is labeled `SOURCE UNKNOWN`. Actual Armory timestamps
are shown in UTC when present; unavailable timestamps remain `—`, and stale
source timestamps take precedence with `OUTDATED`.

## Assets and licensing

- The official logo is not part of the package or repository. An operator who
  has permission to use it may provision it separately and set
  `ARMORY_LOGO_PATH`; production accepts
  `/etc/wow-forever-armory-bot/wow_forever_logo.png`. The image must be a
  root-owned regular file, readable by the `wow-armory` group, and not
  world-accessible. The renderer refuses symlinks and validates PNG format,
  dimensions, and file size. Do not provision or redistribute the logo without
  documented permission covering its use. If no valid path is configured, the
  reserved header area stays blank; no substitute mark is drawn.
- The four bundled DejaVu Sans/Serif regular and bold TTF files are distributed
  under the included `assets/fonts/LICENSE-DejaVu.txt` (Bitstream Vera/DejaVu
  license). They are bundled so output does not depend on host-installed fonts.
- Background texture, borders, Discord mark, class placeholders, and profession
  marks are drawn locally with Pillow. Druid, Warrior, and Mage use original
  vector motifs, with a geometric fallback for other or unknown classes. There
  are no remote or third-party icon downloads.

The stats section keeps its heading and explicit missing-data text, but its
divider moves upward when there are no usable stats. The space is redistributed
to the profession block without changing the 1–3 character canvas sizes.

`pyproject.toml` includes the intro image and nested font/license files as
package data. It never includes the separately provisioned official logo.

## Offline previews and checks

The deterministic fictional fixture suite writes these review images under
`data/ops/previews/`:

- `armory-one-character.png`
- `armory-two-characters.png`
- `armory-three-characters.png`

Run the renderer test with the project environment and `PYTHONPATH=src`. The
test checks each canvas size, PNG encoding, bounded output/performance/memory,
and SHA-256 golden snapshots. Snapshot hashes normalize the logo area so the
tests pass without the unlicensed local logo. It uses no Discord connection or
network access.
The fixture names and profession values are illustrative only; the tests do
not provide fictional stats, PvP, surnames, or portraits.

The integration does not add commands, change storage, or alter deployment
configuration. The existing workflow replaces the member's owned image
attachment in place. Do not deploy these graphics until the preview images,
font/logo redistribution rights, and resulting Discord presentation are
independently reviewed. A deployment rollback remains the existing release
rollback process documented in `OPERATIONS.md`; this renderer task does not
change release or systemd files.
