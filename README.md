# fatebook

<p align="center"><img src="screenshot.png" width="480"></p>

Scripts to convert *Fate/stay night REMASTERED* into a readable EPUB for
eReader use.

- Branch-aware: the full game as 3 volumes — 6 parts, 54 chapters, 728
  scenes; choices hyperlink through the game's flowcharts and bad ends
  carry their Tiger Dojo skits inline
- ~440 art plates as full-bleed page backgrounds, verified on Kobo and
  KOReader
- Decodes the game's container layers itself: FPD packs, EPK text
  archives, KAG scenario scripts, CG blobs
- Voice cues attribute every line to its speaker; flashes, letterbox
  bars and logos are curated out of the art

## Usage

Point `config.toml` at the game (copy
[`config.example.toml`](config.example.toml) to `config.toml` and set
`blade`), then:

    python fsn_extract/make_book.py

## Credits

- [kurikomoe/FSNr_tools](https://github.com/kurikomoe/FSNr_tools) —
  EPK archive decryption (`build/main.exe`) and the FPD keystream
  (`scripts/decryptKey.bin`)
- [DaZombieKiller/FatePackageManager](https://github.com/DaZombieKiller/FatePackageManager)
  (MIT) — FPD pack format documentation and reference implementation

This is a format-shift tool and distributes no game content.
