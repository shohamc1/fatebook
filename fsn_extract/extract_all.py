"""Bulk-extract, from the game's FPD packs, everything the EPUB build consumes.

Produces, under <dest> (default: next to this script):
  kag_all/<name>           all .ks/.fcf scenario scripts (patch00m overrides
                           pack00m where present)
  raw/us_epks/<h>.epk      English text archives, plus their decrypted
  raw/us_epks/<h>.epk_dec  forms via tools_fsnr main.exe 'dec'
  img/<dat>                CG blobs (ex_pack/saber/rin/sakura/patch .dat)
  fileinfo_*.txt           image manifests

Game/tool locations come from config.py (config.toml / repo layout).
Packs are visited in sorted order so patch overrides land after the base
files they replace; files already matching byte-for-byte are left alone,
which makes re-runs cheap and the final state deterministic.

Note: FSNr main.exe requires ASCII paths — keep the repo (and thus the
epk files it decrypts) under an ASCII-only directory.

Usage:
  python extract_all.py [--dest DIR] [--force]
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from . import config
    from .config import TEMP
    from .fpd import FPD, load_key
except ImportError:  # run as a plain script
    import config  # noqa: E402
    from config import TEMP  # noqa: E402
    from fpd import FPD, load_key  # noqa: E402


def _put(path, data, force=False):
    """Write unless identical content is already on disk.

    Content, not size: some patch00m overrides are the same size as their
    pack00m base but differ in bytes, and the patch version must win.
    """
    if not force and os.path.exists(path):
        with open(path, 'rb') as f:
            if f.read() == data:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return True


def extract(dest=None, force=False):
    dest = dest or TEMP
    load_key(config.KEY_BIN)
    kag_dir = os.path.join(dest, 'kag_all')
    epk_dir = os.path.join(dest, 'raw', 'us_epks')
    img_dir = os.path.join(dest, 'img')
    os.makedirs(epk_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    stats = {'kag': 0, 'epk': 0, 'dec': 0, 'dat': 0, 'manifest': 0}
    to_dec = []                 # epks queued for main.exe, decrypted in
                                # parallel once all packs are walked

    def write_entry(fpd, i, n):
        name, _, _, full_len = fpd.entries[i]
        base = name.rsplit('/', 1)[-1]
        if name.startswith('root/data/kag/'):
            if _put(os.path.join(kag_dir, base), fpd.read_entry(i)[1], force):
                n['kag'] += 1
        elif name.startswith('root/data/locale/us/epk/') and base.endswith('.epk'):
            epk_path = os.path.join(epk_dir, base)
            changed = _put(epk_path, fpd.read_entry(i)[1], force)
            if changed:
                n['epk'] += 1
            if changed or not os.path.exists(epk_path + '_dec'):
                to_dec.append(epk_path)
                n['dec'] += 1
        elif name.startswith('pack/'):
            if base.endswith('.dat'):
                dst, kind = os.path.join(img_dir, base), 'dat'
            elif base.startswith('fileinfo_'):
                # ex_pack's manifest is saved with a doubled fileinfo_
                # prefix — sprites.py MANIFESTS expects that spelling
                out = 'fileinfo_' + base if base == 'fileinfo_ex_pack.txt' else base
                dst, kind = os.path.join(dest, out), 'manifest'
            else:
                return
            # dats/manifests each live in exactly one pack (no overrides),
            # so a size match is sufficient — skips decompressing the
            # >1 GB d packs on re-runs
            want = full_len if full_len else fpd.entries[i][2]
            if not force and os.path.exists(dst) \
                    and os.path.getsize(dst) == want:
                return
            if _put(dst, fpd.read_entry(i)[1], force):
                n[kind] += 1

    # Text packs (pack00m + patch00m): patch overrides base for same-named
    # entries — sometimes same size but different bytes — so pick the
    # winner per entry up front and write each file exactly once.
    text_packs = [FPD(os.path.join(config.BLADE, fn), None)
                  for fn in ('pack00m.bin', 'patch00m.bin')
                  if os.path.exists(os.path.join(config.BLADE, fn))]
    winners = {}
    for pi, fpd in enumerate(text_packs):
        for i, (name, _, _, _) in enumerate(fpd.entries):
            winners[name] = (pi, i)
    n = dict.fromkeys(stats, 0)
    for name, (pi, i) in winners.items():
        write_entry(text_packs[pi], i, n)
    done = {k: v for k, v in n.items() if v}
    if done:
        print('pack00m+patch00m (patch wins):', done)
    for k in stats:
        stats[k] += n[k]

    # Remaining packs: CG blobs and manifests, no cross-pack clashes;
    # parsed one at a time (the d packs are >1 GB in memory).
    for fn in sorted(os.listdir(config.BLADE)):
        if not fn.endswith('.bin') or fn in ('pack00m.bin', 'patch00m.bin'):
            continue
        try:
            fpd = FPD(os.path.join(config.BLADE, fn), None)
        except AssertionError:
            print(f'{fn}: not an FPD pack, skipped')
            continue
        n = dict.fromkeys(stats, 0)
        for i in range(len(fpd.entries)):
            write_entry(fpd, i, n)
        del fpd
        done = {k: v for k, v in n.items() if v}
        if done:
            print(f'{fn}:', done)
        for k in stats:
            stats[k] += n[k]
    # one main.exe process per epk (~27 ms spawn overhead each): run the
    # queue through a small pool — separate processes, so threads scale
    if to_dec:
        def _dec(p):
            subprocess.run([config.MAIN_EXE, 'dec', p], check=True,
                           stdout=subprocess.DEVNULL)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_dec, to_dec))
    return stats


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dest', default=TEMP,
                    help='output root (default: next to this script)')
    ap.add_argument('--force', action='store_true',
                    help='re-extract and re-decrypt even if files exist')
    args = ap.parse_args()
    stats = extract(args.dest, args.force)
    print('extracted:', stats)
