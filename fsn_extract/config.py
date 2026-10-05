"""Shared path configuration for the FSN -> EPUB pipeline.

Machine-dependent locations (game install, vendored FSNr_tools) resolve in
this order:
  1. <repo>/config.toml  (copy config.example.toml and edit; gitignored)
  2. Repo-default layout: the directories sitting next to fsn_extract/

config.toml keys ([paths] table, all optional):
  blade     dir holding the game (Blade folder or its obb/ subfolder with
            the *.bin packs; obb appended automatically if missing)
  main_exe  FSNr_tools EPK decryptor (default tools_fsnr/build/main[.exe])
  key_bin   FSNr_tools FPD keystream (default tools_fsnr/scripts/decryptKey.bin)
  output    dir for the finished EPUBs (default <repo>/output)

All intermediate output (extracted game data and build work dirs) lives
in TEMP (default <repo>/output/temp).

Everything else in the pipeline (build work dirs) is
derived from the scripts' own location, so the repo itself can live anywhere.
"""
import glob
import os
import sys
import threading
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(HERE)

try:
    with open(os.path.join(_REPO, 'config.toml'), 'rb') as _f:
        _cfg = tomllib.load(_f).get('paths', {})
except FileNotFoundError:
    _cfg = {}

BLADE = _cfg.get('blade', os.path.join(_REPO, 'Blade'))
if os.path.basename(BLADE) != 'obb' and not glob.glob(
        os.path.join(BLADE, '*.bin')):
    BLADE = os.path.join(BLADE, 'obb')
_DEFAULT_EXE = 'main.exe' if sys.platform == 'win32' else 'main'
MAIN_EXE = _cfg.get('main_exe',
                    os.path.join(_REPO, 'tools_fsnr', 'build', _DEFAULT_EXE))
KEY_BIN = _cfg.get('key_bin', os.path.join(_REPO, 'tools_fsnr', 'scripts', 'decryptKey.bin'))
OUTPUT_DIR = _cfg.get('output', os.path.join(_REPO, 'output'))
TEMP = os.path.join(OUTPUT_DIR, 'temp')


def _tmp_name(path):
    return f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'


def write_atomic(path, data):
    """Write bytes so a concurrent reader never sees a partial cache file."""
    tmp = _tmp_name(path)
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def save_atomic(im, path, **kw):
    """PIL save() with write_atomic semantics; format comes from the
    extension, as in im.save(path)."""
    from PIL import Image
    fmt = kw.pop('format', None) or Image.registered_extensions()[
        os.path.splitext(path)[1].lower()]
    tmp = _tmp_name(path)
    im.save(tmp, format=fmt, **kw)
    os.replace(tmp, path)
