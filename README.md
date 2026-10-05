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

Setup per platform (installs [uv](https://docs.astral.sh/uv/),
a C++ compiler and `git`):

macOS:

```sh
xcode-select --install --no-sudo 2>/dev/null; \
curl -LsSf https://astral.sh/uv/install.sh | sh && exec $SHELL -l
```

Debian:

```sh
sudo apt update && sudo apt install -y build-essential git && \
curl -LsSf https://astral.sh/uv/install.sh | sh && exec $SHELL -l
```

Windows (PowerShell, run as admin):

```powershell
winget install -e --id Git.Git; winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --passive"; winget install -e --id astral-sh.uv
```

Then point `config.toml` at the game (copy
[`config.example.toml`](config.example.toml) and set `blade` to the
game's Blade folder) and build:

    make

You can also run steps individually:

    make tool    # clone + compile the FSNr decryptor (native binary)
    make run     # uv run fsn-book && uv run fsn-validate

```sh
uv sync
uv run fsn-book
uv run fsn-validate
```

Finished EPUBs land in `output/`; all intermediate data (extracted
game data, work dirs) goes to `output/temp/`.

## Credits

- [kurikomoe/FSNr_tools](https://github.com/kurikomoe/FSNr_tools) —
  EPK archive decryption (`build/main.exe`) and the FPD keystream
  (`scripts/decryptKey.bin`)
- [DaZombieKiller/FatePackageManager](https://github.com/DaZombieKiller/FatePackageManager)
  (MIT) — FPD pack format documentation and reference implementation

This is a format-shift tool and distributes no game content.
