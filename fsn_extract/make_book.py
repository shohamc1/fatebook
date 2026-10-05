"""One-shot build: game install -> branch-aware EPUB volumes.

Point config.toml at the game (see config.example.toml), then run:

    python make_book.py

Extracts the scenario scripts, English text archives and CG data from the
game's FPD packs (skipping anything already extracted), builds the three
volumes into ../output/, and lists the outputs. Check the result with
validate_book.py (must print errors: 0).

FSNr main.exe requires ASCII paths — keep the repo under an
ASCII-only directory.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from . import config
except ImportError:  # run as a plain script
    sys.path.insert(0, HERE)
    import config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--extract-only', action='store_true',
                    help='stop after extracting the game data')
    ap.add_argument('--no-extract', action='store_true',
                    help='skip extraction, build from existing data')
    ap.add_argument('--force', action='store_true',
                    help='re-extract and re-decrypt even if files exist')
    args = ap.parse_args()

    missing = [f'{label} ({path})'
               for label, path in (('game packs [config.toml blade]', config.BLADE),
                                   ('FSNr main.exe', config.MAIN_EXE),
                                   ('FSNr decryptKey.bin', config.KEY_BIN))
               if not os.path.exists(path)]
    if missing:
        sys.exit('missing:\n  ' + '\n  '.join(missing))

    if not args.no_extract:
        try:
            from . import extract_all
        except ImportError:
            import extract_all
        stats = extract_all.extract(config.TEMP, force=args.force)
        print('extraction:', stats)

    if args.extract_only:
        return

    try:
        from .book_model import build_book, VOLUMES
    except ImportError:
        from book_model import build_book, VOLUMES
    for i, (vtitle, vparts, vout) in enumerate(VOLUMES, 1):
        print(f'=== Volume {vtitle} ===')
        build_book(limit_parts=vparts, volume_title=vtitle, out_name=vout,
                   work_dir=os.path.join(config.TEMP, f'book_work_v{i}'))
        print('built:', os.path.join(config.OUTPUT_DIR, vout))


if __name__ == '__main__':
    main()
