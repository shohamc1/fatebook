# fsn_extract — conversion pipeline

Scripts that turn *Fate/stay night REMASTERED* (Steam appid 2396980,
v1.4.0.388; engine "fsn2", KiriKiri/KAG-based) into a branch-aware EPUB.
Setup and usage live in the [repo README](../README.md) — the short
version: point `config.toml` at the game, run `make` (or
`uv run fsn-book`).

## Flow

1. `extract_all.py` reads the game's FPD packs and writes the working
   data to `../output/temp/`: `kag_all/` (.ks/.fcf scenario scripts,
   patch versions winning
   over base), `raw/us_epks/` (English text archives, decrypted via
   FSNr `main`), `img/*.dat` CG blobs and the `fileinfo_*.txt`
   manifests.
2. `book_model.build_book()` decodes scripts + text + flowcharts and
   renders the book into `../output/`: 3 volumes plus the single-file
   branch-aware edition (6 parts, 54 chapters, 728 scene sections,
   440 art plates, 717 internal links).
3. `validate_book.py` checks the built tree (must print errors: 0).

Re-runs are incremental: extraction skips data that already matches
byte-for-byte, and the book rebuilds from whatever is on disk.

## Formats

**FPD packs** (`Blade/obb/*.bin`) — 0x38-byte big-endian header (magic
`FPD\0`, version, fileCount, dataStart), then an XOR-scrambled entry
table (0x20 B/entry: nameOffset, dataOffset, dataLength, fullLength)
plus a zlib name buffer; file data at dataStart, each file
XOR-scrambled with a 64 KB repeating keystream
(`tools_fsnr/scripts/decryptKey.bin`), zlib-compressed when
fullLength != 0. Reader: `fpd.py`.

**EPK text archives** — `root/data/epk/<hash>.epk` (Japanese) and
`root/data/locale/{us,ck}/epk/<hash>.epk` (English / Simplified
Chinese). The filename is a 26-char base-36 hash of the chapter's
Japanese name (MD5 → 5-bit groups). Payload: Blowfish-like 18-round
Feistel keyed by the filename; the container appends 0x10 zeros + BE32
real_size + zeros + MD5(payload ‖ "8FE9D249BD2689BB4B70F5AE88A9E645").
Decrypted with `tools_fsnr/build/main.exe`; the plaintext is a `DAT`
key-value dump (`qid::message_...::text::`).

**Scenario scripts** — `root/data/kag/*.ks` and `.fcf` inside the FPD
packs. KAG syntax: `*pageN|` page labels, `$$$message_BBBB_PPPP_LLLL$$$`
text slots, `@say storage=<chap>_<voice>_<n>` speaker cues (voice codes
→ `SPEAKERS` map in `fsn2epub.py`), `@r`/`@pg`/`@pgnl` flow,
`@date_title` day cards.

**CG artwork** — webp images inside `pack/*.dat` blobs (ex_pack.dat in
pack10d holds the scenario CGs; saber/rin/sakura.dat the route extras;
patch.dat overrides). The `pack/fileinfo_*.txt` manifests map the
Japanese storage names used by .ks directives (`@fadein file=04突き`,
`@image storage=...`) to `name::webp::<dat>::offset::size::md5`.
`ImageResolver` extracts on demand, applies fliplr/flipud as the script
specifies, converts to JPEG (Pillow, max width 1600).

## Book mechanics

- `.fcf` flowcharts are the branch graph: SCENE/SELECTER/OUTERLABEL
  nodes, the ROUTE table is the directed edges in display order.
  SELECTER labels come from `statictext.epk` (flowtext_*); option k
  pairs with the k-th outgoing route. Virtual nodes (no script, e.g.
  flag junctions) are skipped; links resolve forward through the graph.
- Each `-NN.ks` sub-script has its own text epk; the mapping is
  `script_epk_map.json` (slot-key overlap matching, 719/728; 9 system
  scripts without text).
- Choices render as bordered boxes hyperlinked to the target scene;
  multi-out SCENE edges render as dashed "hidden fork" boxes with the
  flag name (桜好感度 etc.); bad ends carry their Tiger Dojo skit
  inline (`@pgtg`/`@tiger_end`, detected via dtg/dir voice codes).
- Art curation: image runs collapse to the settled background; flashes
  (no text between), letterbox bars (シネスコ), logos and extreme aspect
  ratios are dropped. Per-page background = the last bg directive
  before the page's first text slot; blackout → plain black page.

## Rendering notes (Kobo / KOReader crengine)

- COLOR art as full-bleed CSS backgrounds behind the text, dark scrim
  panel with a long gradient fade and a solid rgba fallback; one "page"
  div per background change; simple CSS only (no vh/media queries); one
  XHTML per chapter. If a reader drops background-images, pages fall
  back to black with the translucent panel — still readable. For Kobo's
  KePub renderer, rename to `*.kepub.epub`.
- Verified from crengine source: `background-image` is supported
  (DrawBackgroundImage), including background-position/-repeat/-size
  and `cover cover` / `contain contain` scaling. Gotchas:
  `background-size: cover` (single keyword) parses but draws nothing on
  older builds — set both values; `linear-gradient()` is unsupported;
  avoid floats and vh units. The book pairs `background-size: contain;`
  with `contain contain;` so both spec browsers and cre get a valid
  value.
