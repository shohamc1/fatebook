"""Shared path configuration for the FSN -> EPUB pipeline.

Machine-dependent locations (game install, vendored FSNr_tools) resolve in
this order:
  1. <repo>/config.toml  (copy config.example.toml and edit; gitignored)
  2. Repo-default layout: the directories sitting next to fsn_extract/

config.toml keys ([paths] table, all optional):
  blade     dir holding the game's FPD packs (default Blade/obb, *.bin)
  main_exe  FSNr_tools EPK decryptor (default tools_fsnr/build/main.exe)
  key_bin   FSNr_tools FPD keystream (default tools_fsnr/scripts/decryptKey.bin)

Everything else in the pipeline (extracted data, work dirs, output books) is
derived from the scripts' own location, so the repo itself can live anywhere.
"""
import os
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(HERE)

try:
    with open(os.path.join(_REPO, 'config.toml'), 'rb') as _f:
        _cfg = tomllib.load(_f).get('paths', {})
except FileNotFoundError:
    _cfg = {}

BLADE = _cfg.get('blade', os.path.join(_REPO, 'Blade', 'obb'))
MAIN_EXE = _cfg.get('main_exe', os.path.join(_REPO, 'tools_fsnr', 'build', 'main.exe'))
KEY_BIN = _cfg.get('key_bin', os.path.join(_REPO, 'tools_fsnr', 'scripts', 'decryptKey.bin'))
