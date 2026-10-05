# -*- coding: utf-8 -*-
"""Character sprite compositor PoC for the Fate/stay night Remaster EPUB project.

Standalone module.  Reads:
    kag_all/*.ks                    KAG scripts (patch versions)
    fileinfo_*.txt                  image manifests (name::ext::dat::off::size::md5)
    img/ex_pack.dat                 local blob for ex_pack sprites/backgrounds
    img/bg_*.jpg                    already-converted backgrounds (READ ONLY)
    <Blade>/obb/*.bin                FPD packs holding saber/rin/sakura/patch .dat blobs
                                     (see config.py / config.example.toml)
    raw/us_epks/*.epk_dec           English text slots ('NNN::$$$message_B_P_L$$$::text::')
    script_epk_map.json             script filename -> epk basename

Writes ONLY under sprites_preview/ (index.html + cache/).  Never touches
book_model.py / fsn2epub.py / fpd.py / book/ / book_work/ / preview/ / img/.

Spec'd API (see task):
    parse_sprite_track(script_text) -> list of sprite-layer events
    snapshot_at_lines(script_text)  -> [(line_index, {pos: {'file','index','fliplr'}})]
    composite(bg_jpg_path, sprites) -> path of composited JPEG in sprites_preview/cache/

Deviations from the letter of the spec, both deliberate (data-driven):
    * snapshot slot values are small dicts {'file','index','fliplr'} rather than a
      bare filename: the spec itself asks for per-slot fliplr AND index= z-order at
      composite time, which a bare string cannot carry.
    * @cl*/@clfg honour a pos= parameter when present ('@cl pos=center' clears only
      the center slot; 'pos=all' or no pos clears everything).  The chapter uses
      per-position clears in 37 of 58 @cl lines; clearing all slots every time
      would visibly desync the standing cast.
    * @ldall/@ldallT (l= r= il= ir=) are folded into the @ld* family as two
      simultaneous placements (left + right); they occur 11x in this chapter.
    * placing a character vacates any other position slot occupied by the SAME
      character (key = leading kana/kanji run of the sprite name).  Without
      this, '@ld pos=right 一成01c' after '@ld pos=center 一成01a' leaves the
      same character standing in two places (crossfade moves, e.g. script -03
      lines 66-88, script -07 line 145); the game shows them moved, not cloned.
    * sprites are divided by the manifest render-scale field (8th '::' field;
      ex_pack sprites carry 1.128125).  Without it, raw heights 814-1140px vs the
      941px canvas clip up to 200px of head; with it display heights land at
      722-1010px (tall characters still graze the top edge, matching typical VN
      framing).  Backgrounds carry scale 1.0 so this only affects sprites.
"""

import hashlib
import html
import io
import json
import os
import re
import sys

from PIL import Image, ImageOps

ROOT = os.path.dirname(os.path.abspath(__file__))
try:
    from .config import TEMP, BLADE, KEY_BIN
except ImportError:  # run as a plain script
    sys.path.insert(0, ROOT)
    from config import TEMP, BLADE, KEY_BIN  # noqa: E402

KAG_DIR = os.path.join(TEMP, 'kag_all')
IMG_DIR = os.path.join(TEMP, 'img')          # read-only
PREVIEW_DIR = os.path.join(TEMP, 'sprites_preview')
CACHE_DIR = os.path.join(PREVIEW_DIR, 'cache')
SPRITE_CACHE_DIR = os.path.join(CACHE_DIR, 'sprites')
os.makedirs(SPRITE_CACHE_DIR, exist_ok=True)
EPK_DIR = os.path.join(TEMP, 'raw', 'us_epks')
EPK_MAP = os.path.join(ROOT, 'script_epk_map.json')

# manifest order matters: later manifests override earlier ones (patch wins)
MANIFESTS = ['fileinfo_fileinfo_ex_pack.txt', 'fileinfo_saber.txt',
             'fileinfo_rin.txt', 'fileinfo_sakura.txt', 'fileinfo_patch.txt']
DAT_PACK = {'ex_pack.dat': ('pack10d', 'pack/ex_pack.dat'),
            'saber.dat': ('pack11d', 'pack/saber.dat'),
            'rin.dat': ('pack12d', 'pack/rin.dat'),
            'sakura.dat': ('pack12d', 'pack/sakura.dat'),
            'patch.dat': ('patch01d', 'pack/patch.dat')}

# ---------------------------------------------------------------- geometry
# horizontal anchor (fraction of canvas width) for each standing position
POS_ANCHOR = {'left': 0.28, 'leftcenter': 0.39, 'center': 0.50,
              'rightcenter': 0.61, 'right': 0.72}
# z-order fallback when an @ld line lacks index=
POS_DEFAULT_INDEX = {'left': 1000, 'leftcenter': 2000, 'center': 3000,
                     'rightcenter': 4000, 'right': 5000}
POS_ALIAS = {'l': 'left', 'left': 'left',
             'lc': 'leftcenter', 'leftcenter': 'leftcenter',
             'c': 'center', 'center': 'center',
             'rc': 'rightcenter', 'rightcenter': 'rightcenter',
             'r': 'right', 'right': 'right'}

BEAT_RE = re.compile(r'^\$\$\$(message_\d+_\d+_\d+)\$\$\$$')
PARAM_RE = re.compile(r'(\w+)=(\S+)')
TAG_RE = re.compile(r'\[(?:lr|line\d+|r|pg|l)\]')          # display tags in epk text

LD_CMDS = ('ld', 'ld_auto', 'ldnotrans', 'ld_notrans')
CL_CMDS = ('cl', 'cl_auto', 'clnotrans', 'cl_notrans', 'clfg')
# every command that mutates the standing-cast slots (book_model dispatch)
SPRITE_CMDS = {'ld', 'ld_auto', 'ldnotrans', 'ld_notrans', 'fg', 'chgfg',
               'cl', 'cl_auto', 'clnotrans', 'cl_notrans', 'clfg', 'cl_fadein'}
# character sprites always start with kana/kanji, contain a digit and carry a
# distance suffix (遠/中/近); everything else placed on character slots is an
# effect overlay (シネスコ letterbox, damage flashes, colors, cut-ins)
SPRITE_NAME_RE = re.compile(r'^[ぁ-ヿ一-鿿].*\d.*\([遠中近]\)$')


def is_sprite_name(name):
    return bool(SPRITE_NAME_RE.match(name or ''))
# commands that set the background; param looked up in order file=, bg=, storage=
BG_CMDS = ('fadein', 'bg', 'rep', 'a2a', 'a2aT', 'i2i', 'i2iT', 'i2o', 'i2oT')
CLEAR_CMDS = {'blackout': 'black', 'black': 'black',
              'whiteout': 'white', 'white': 'white'}

MAX_BEATS_PER_SCRIPT = 40
CANVAS_W = 1600
CANVAS_H = 941            # actual height of the converted bg jpgs (1600x941)
MAX_SPRITE_W_FRAC = 0.45  # sprite wider than 45% of canvas -> scale down


# ================================================================ manifest
class ManifestIndex:
    """name::ext::dat::offset::size::md5::0::scale::... index, case-insensitive."""

    def __init__(self):
        self.exact = {}                     # name -> (dat, offset, size, scale)
        for fi in MANIFESTS:
            path = os.path.join(TEMP, fi)
            if not os.path.exists(path):
                continue
            for line in open(path, encoding='utf-8'):
                parts = line.split('::')
                if len(parts) >= 5 and parts[1] in ('webp', 'png', 'jpg'):
                    scale = 1.0
                    try:
                        if len(parts) >= 8 and parts[7]:
                            scale = float(parts[7]) or 1.0
                    except ValueError:
                        pass
                    self.exact[parts[0]] = (parts[2], int(parts[3]),
                                            int(parts[4]), scale)
        self.lower = {k.lower(): (k, v) for k, v in self.exact.items()}

    def lookup(self, name):
        """-> (dat, offset, size, scale) or None."""
        ent = self.exact.get(name)
        if ent:
            return ent
        hit = self.lower.get(name.lower())
        return hit[1] if hit else None


_MANIFEST = None


def manifests():
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = ManifestIndex()
    return _MANIFEST


# ---------------------------------------------------------------- blobs
_BLOBS = {}      # datname -> open binary file handle (local copy)


def _blob_path(datname, warnings):
    """Local .dat blob path; extract from the FPD pack once if needed."""
    local_img = os.path.join(IMG_DIR, datname)
    if os.path.exists(local_img):
        return local_img                       # read in place (ex_pack is 628 MB)
    cached = os.path.join(CACHE_DIR, datname)
    if os.path.exists(cached):
        return cached
    if datname not in DAT_PACK:
        warnings.append(f'unknown dat {datname}')
        return None
    pack, entry = DAT_PACK[datname]
    try:
        sys.path.insert(0, ROOT)
        from fpd import FPD, load_key           # noqa: F401  (fpd.py is read-only)
        load_key(KEY_BIN)
        fpd = FPD(os.path.join(BLADE, f'{pack}.bin'), None)
        for i, (name, _off, _ln, _fl) in enumerate(fpd.entries):
            if name == entry:
                data = fpd.read_entry(i)[1]
                with open(cached, 'wb') as f:
                    f.write(data)
                return cached
        warnings.append(f'entry {entry} not found in {pack}.bin')
    except Exception as e:                     # pragma: no cover
        warnings.append(f'blob extract failed for {datname}: {e}')
    return None


def _blob_handle(datname, warnings):
    path = _blob_path(datname, warnings)
    if path is None:
        return None
    if datname not in _BLOBS:
        try:
            _BLOBS[datname] = open(path, 'rb')
        except OSError as e:
            warnings.append(f'cannot open {path}: {e}')
            return None
    return _BLOBS[datname]


# ---------------------------------------------------------------- images
WARNINGS = []       # module-level sink for lookup/extract problems
_SPRITE_MEM = {}    # name -> PIL RGBA image


def _safe_name(name):
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('_')
    return f'{base}_{hashlib.md5(name.encode("utf-8")).hexdigest()[:6]}'


def get_sprite_rgba(name, warnings=None):
    """Decode a character sprite webp to RGBA, applying the manifest render
    scale (ex_pack sprites carry scale=1.128125 -> divide stored size by it;
    raw heights 814-1140px otherwise overflow the 941px canvas badly).
    Raw webp extracts are cached under cache/sprites/.  Decoded sprites are
    LRU-capped: a full-book build touches ~1,500 of them (~3 GB as RGBA)."""
    warnings = warnings if warnings is not None else WARNINGS
    if name in _SPRITE_MEM:
        im = _SPRITE_MEM.pop(name)      # move-to-end (LRU)
        _SPRITE_MEM[name] = im
        return im
    ent = manifests().lookup(name)
    if ent is None:
        warnings.append(f'sprite not in manifest: {name}')
        return None
    datname, off, size, scale = ent
    webp_path = os.path.join(SPRITE_CACHE_DIR, _safe_name(name) + '.webp')
    if not os.path.exists(webp_path):
        fh = _blob_handle(datname, warnings)
        if fh is None:
            return None
        fh.seek(off)
        raw = fh.read(size)
        with open(webp_path, 'wb') as f:
            f.write(raw)
    im = Image.open(webp_path).convert('RGBA')
    if scale != 1.0:
        im = im.resize((max(1, round(im.width / scale)),
                        max(1, round(im.height / scale))), Image.LANCZOS)
    _SPRITE_MEM[name] = im
    while len(_SPRITE_MEM) > 160:
        _SPRITE_MEM.pop(next(iter(_SPRITE_MEM)))
    return im


def bg_jpg_name(name):
    """Replicated fsn2epub background filename convention."""
    return ('bg_' + re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_') +
            '_' + hashlib.md5(name.encode('utf-8')).hexdigest()[:6] + '.jpg')


def get_bg_jpg(name, warnings=None, flipped=False):
    """-> jpg path for a background name, or None.

    Uses the existing img/ conversion when present; otherwise converts the
    webp into sprites_preview/cache/ (width 1600, RGB, q86).  Never writes img/.
    """
    warnings = warnings if warnings is not None else WARNINGS
    if not name:
        return None
    fn = bg_jpg_name(name) + ('_f' if flipped else '')
    in_img = os.path.join(IMG_DIR, fn)
    if os.path.exists(in_img):
        return in_img
    cached = os.path.join(CACHE_DIR, fn)
    if os.path.exists(cached):
        return cached
    ent = manifests().lookup(name)
    if ent is None:
        warnings.append(f'bg not in manifest: {name}')
        return None
    datname, off, size, _scale = ent
    fh = _blob_handle(datname, warnings)
    if fh is None:
        return None
    fh.seek(off)
    im = Image.open(io.BytesIO(fh.read(size))).convert('RGB')
    if flipped:
        im = ImageOps.mirror(im)
    if im.width > CANVAS_W:
        im = im.resize((CANVAS_W, round(im.height * CANVAS_W / im.width)),
                       Image.LANCZOS)
    im.save(cached, quality=86)
    return cached


# ================================================================ KAG walk
def _params(rest):
    return dict(PARAM_RE.findall(rest))


# variants that change the character's READ (corruption/rain/mud/blood)
# keep exact identity in slide keys; plain expression/pose suffixes don't
STRONG_VARIANT_RE = re.compile(r'(汚染|雨|泥|血)')


def base_key(file_name):
    """Outfit-level identity of a sprite: kana/kanji run + first digit group
    ('桜制服13b頬(中)' -> '桜制服13'). Expression letters (a/b/c), pose and
    detail suffixes collapse onto it — slides freeze the cast at the state
    they had when the slide opened, like a static book."""
    m = re.match(r'^([ぁ-ヿ一-鿿]+\d+)', file_name or '')
    return m.group(1) if m else (file_name or '')


def char_key(name):
    """Identity key of the character a sprite belongs to: the leading run of
    kana/kanji before the first digit or latin char ('桜制服09a(中)' ->
    '桜制服').  Used to detect a character being re-placed in a new position
    slot while still standing in another one."""
    m = re.match(r'^([ぁ-ヿ一-鿿]+)', name or '')
    return m.group(1) if m else (name or '')


def _cmd(line):
    """-> (cmd, rest) for a non-comment command line, else (None, None)."""
    s = line.strip()
    if not s.startswith('@'):
        return None, None
    m = re.match(r'@(\w+)(.*)$', s)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def parse_line_sprite_ops(cmd, rest):
    """One sprite-family command line -> list of slot ops.
       ('ld', pos, file, index, fliplr) | ('chg', file, index)
       | ('cl', pos) | ('clchar', file) | ('clall',)
    Names failing the character-sprite shape guard are dropped: those lines
    place effect overlays (letterbox/flash/cut-in), not standing cast."""
    p = _params(rest)
    ops = []
    if cmd.startswith('ldall'):
        # l=/r= side pairs (il=/ir= z-order) and c=/lc=/rc= single placements
        # (ic=/ilc=/irc= z-order) both occur
        sides = (('l', 'left'), ('r', 'right'), ('c', 'center'),
                 ('lc', 'leftcenter'), ('rc', 'rightcenter'))
        for side, pos in sides:
            f = p.get(side)
            if f and is_sprite_name(f):
                ops.append(('ld', pos, f,
                            int(p.get('i' + side, 0)) or None,
                            p.get('fliplr') == 'true'))
        return ops
    if cmd in LD_CMDS or cmd == 'fg':
        f = p.get('file') or p.get('storage')
        if f and is_sprite_name(f):
            pos = POS_ALIAS.get(p.get('pos', 'c'), 'center')
            idx = p.get('index')
            ops.append(('ld', pos, f, int(idx) if idx else None,
                        p.get('fliplr') == 'true'))
        return ops
    if cmd == 'chgfg':
        st = p.get('storage') or p.get('file') or ''
        idx = p.get('index')
        for piece in st.split(','):
            if piece and is_sprite_name(piece):
                ops.append(('chg', piece, int(idx) if idx else None))
        return ops
    # cl family: @clfg storage=X clears that character; pos= clears one slot;
    # anything else (incl. bare @clfg/@cl) clears the whole cast
    if cmd == 'clfg' and not p.get('pos') and p.get('storage'):
        for piece in p['storage'].split(','):
            if piece:
                ops.append(('clchar', piece))
        return ops
    pos = POS_ALIAS.get(p.get('pos'))
    ops.append(('cl', pos) if pos else ('clall',))
    return ops


class SpriteState:
    """Standing-cast slots, evolved by parse_line_sprite_ops output."""

    def __init__(self):
        self.slots = {}          # pos -> {'file','index','fliplr'}

    def apply(self, ops):
        """Apply ops; -> True when the visible cast changed."""
        before = self.key()
        for op in ops:
            kind = op[0]
            if kind == 'ld':
                _, pos, f, idx, flip = op
                ck = char_key(f)
                for other in [p for p, s in self.slots.items()
                              if p != pos and char_key(s['file']) == ck]:
                    del self.slots[other]      # crossfade move, not a clone
                self.slots[pos] = {'file': f, 'index': idx, 'fliplr': flip}
            elif kind == 'chg':
                _, f, idx = op
                ck = char_key(f)
                for s in self.slots.values():
                    if char_key(s['file']) == ck:
                        s['file'] = f
                        if idx:
                            s['index'] = idx
            elif kind == 'cl':
                self.slots.pop(op[1], None)
            elif kind == 'clchar':
                ck = char_key(op[1])
                for p in [q for q, s in self.slots.items()
                          if char_key(s['file']) == ck]:
                    del self.slots[p]
            elif kind == 'clall':
                self.slots.clear()
        return self.key() != before

    def key(self):
        return tuple(sorted((p, s['file'], s['index'], s['fliplr'])
                            for p, s in self.slots.items()))

    def soft_key(self):
        """Slide-emission key: expression/pose variants of one outfit collapse
        to base identity; strong visual states stay exact."""
        return tuple(sorted(
            (p, s['file'] if STRONG_VARIANT_RE.search(s['file'])
             else base_key(s['file']))
            for p, s in self.slots.items()))

    def snapshot(self):
        """Composite-ready slot list (default z-order filled in)."""
        return [{'file': s['file'], 'pos': p,
                 'index': s['index'] or POS_DEFAULT_INDEX[p],
                 'fliplr': s['fliplr']}
                for p, s in sorted(self.slots.items())]


def parse_sprite_track(script_text):
    """Sprite-layer events only.

    Returns a list of dicts:
        {'line_index', 'cmd', 'action': 'ld'|'cl', 'pos', 'file',
         'index', 'fliplr'}
    '@ldall' expands into two 'ld' events (left + right).
    Comment lines (leading ';') are ignored.
    """
    events = []
    for i, line in enumerate(script_text.splitlines()):
        cmd, rest = _cmd(line)
        if cmd is None:
            continue
        if cmd in LD_CMDS or cmd.startswith('ldall'):
            p = _params(rest)
            if cmd.startswith('ldall'):
                for side, pos in (('l', 'left'), ('r', 'right')):
                    if p.get(side):
                        events.append({'line_index': i, 'cmd': cmd,
                                       'action': 'ld', 'pos': pos,
                                       'file': p[side],
                                       'index': int(p.get('i' + side, 0)) or None,
                                       'fliplr': p.get('fliplr') == 'true'})
                continue
            pos = POS_ALIAS.get(p.get('pos', 'c'), 'center')   # no pos -> center
            idx = p.get('index')
            events.append({'line_index': i, 'cmd': cmd, 'action': 'ld',
                           'pos': pos, 'file': p.get('file'),
                           'index': int(idx) if idx else None,
                           'fliplr': p.get('fliplr') == 'true'})
        elif cmd in CL_CMDS:
            p = _params(rest)
            pos = POS_ALIAS.get(p.get('pos'))
            events.append({'line_index': i, 'cmd': cmd, 'action': 'cl',
                           'pos': pos, 'file': None, 'index': None,
                           'fliplr': False})
    return events


def walk_script(script_text):
    """Single pass over the script.

    Returns a list of beat dicts, one per ^$$$message_B_P_L$$$ line:
        {'line_index', 'message_id', 'sprites': {pos: slot}, 'bg': name|None,
         'bg_color': 'black'|'white'|None}
    where slot = {'file', 'index', 'fliplr'}.
    Also returned (second value): the raw sprite event list.
    """
    state = SpriteState()
    bg = None           # current bg image name (None = cleared screen)
    bg_color = None     # 'black'/'white' when screen cleared to a colour
    beats = []
    events = parse_sprite_track(script_text)
    ev_by_line = {}
    for ev in events:
        ev_by_line.setdefault(ev['line_index'], []).append(ev)

    for i, line in enumerate(script_text.splitlines()):
        stripped = line.strip()
        m = BEAT_RE.match(stripped)
        if m:
            beats.append({'line_index': i, 'message_id': m.group(1),
                          'sprites': {p: dict(v)
                                      for p, v in state.slots.items()},
                          'bg': bg, 'bg_color': bg_color})
            continue
        if stripped.startswith(';') or not stripped.startswith('@'):
            continue
        cmd, rest = _cmd(line)
        if cmd is None:
            continue
        if (cmd in SPRITE_CMDS or cmd.startswith('ldall')):
            state.apply(parse_line_sprite_ops(cmd, rest))
        if cmd in BG_CMDS:                         # background switch
            p = _params(rest)
            name = p.get('file') or p.get('bg') or p.get('storage')
            if name:
                if name.lower() in ('black', 'white'):
                    bg, bg_color = None, name.lower()
                else:
                    bg, bg_color = name, None
        elif cmd in CLEAR_CMDS:                    # screen clear
            bg, bg_color = None, CLEAR_CMDS[cmd]
    return beats, events


def snapshot_at_lines(script_text):
    """Spec-facing: standing cast at every text beat.

    -> [(line_index, {pos_name: {'file', 'index', 'fliplr'}})]
    (slot dicts carry the per-slot fliplr and z-order index).
    """
    beats, _ = walk_script(script_text)
    return [(b['line_index'], b['sprites']) for b in beats]


# ================================================================ compositor
_COMPOSITE_KEYS = {}     # key -> output filename

# Standing-cast display calibration (2026-10-03, user review "slightly too
# large"): manifest-native sizes put (中) sprites at 77-107% of the 941px
# canvas — the tallest clip the top edge, which the game never does.  The
# game's own framing (text window covers the bottom 30%) puts standing
# characters at ~75-85%; scale to 0.85 with feet on a 3% floor line.
STANDING_SCALE = 0.85
FEET_LINE = 0.97
KEY_VERSION = 'v2'       # bump to invalidate stale composite caches


def composite(bg_jpg_path, sprites, bgcolor=None):
    """Alpha-composite standing sprites over a background.

    bg_jpg_path : jpg path or None (None + bgcolor -> flat colour canvas)
    sprites     : iterable of {'file','pos','index','fliplr'}
    bgcolor     : 'black'/'white' fallback when bg_jpg_path is None
    -> path of the saved JPEG in sprites_preview/cache/ (content-addressed).

    Sprites are resolved through get_sprite_rgba(); unresolvable names are
    skipped (and logged to WARNINGS).
    """
    wanted = [s for s in sprites if s and s.get('file')]
    bg_key = os.path.basename(bg_jpg_path) if bg_jpg_path else f'flat:{bgcolor}'
    sprite_key = '|'.join(sorted(f"{s['pos']}:{s['file']}:{s['index']}:{s['fliplr']}"
                                 for s in wanted))
    key = hashlib.md5((KEY_VERSION + bg_key + '#' + sprite_key)
                      .encode('utf-8')).hexdigest()[:16]
    out = os.path.join(CACHE_DIR, f'comp_{key}.webp')
    if os.path.exists(out):
        return out

    if bg_jpg_path:
        canvas = Image.open(bg_jpg_path).convert('RGBA')
    else:
        rgb = (255, 255, 255) if bgcolor == 'white' else (16, 16, 16)
        canvas = Image.new('RGBA', (CANVAS_W, CANVAS_H), rgb + (255,))

    W, H = canvas.size
    live = []
    for s in wanted:
        im = get_sprite_rgba(s['file'])
        if im is not None:
            live.append(s)
    for s in sorted(live, key=lambda s: (s['index'] if s['index'] is not None
                                         else POS_DEFAULT_INDEX[s['pos']])):
        im = get_sprite_rgba(s['file']).copy()
        if s.get('fliplr'):
            im = ImageOps.mirror(im)
        if im.width > MAX_SPRITE_W_FRAC * W:
            nw = int(MAX_SPRITE_W_FRAC * W)
            im = im.resize((nw, round(im.height * nw / im.width)), Image.LANCZOS)
        # standing calibration: game shows the cast smaller than manifest
        # native size; nothing may exceed 94% of the canvas height
        im = im.resize((max(1, round(im.width * STANDING_SCALE)),
                        max(1, round(im.height * STANDING_SCALE))), Image.LANCZOS)
        if im.height > 0.94 * H:
            im = im.resize((max(1, round(im.width * 0.94 * H / im.height)),
                            round(0.94 * H)), Image.LANCZOS)
        anchor = POS_ANCHOR[s['pos']]
        x = int(round(anchor * W - im.width / 2))
        y = int(round(FEET_LINE * H)) - im.height   # feet on the floor line
        if y < 0:
            y = 0
        overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        overlay.paste(im, (x, y))
        canvas = Image.alpha_composite(canvas, overlay)

    # target device: Kobo Libra Colour's color layer resolves ~150 ppi
    # (~1250px across in landscape), so 1280w WebP is beyond native; cre's
    # image loader decodes webp (lvimg.cpp) and it halves the bytes vs JPEG
    canvas = canvas.convert('RGB')
    if canvas.width > 1280:
        canvas = canvas.resize((1280, round(canvas.height * 1280 / canvas.width)),
                               Image.LANCZOS)
    canvas.save(out, format='WEBP', quality=78, method=4)
    return out


# ================================================================ text
def load_text_slots(epk_dec_path):
    """epk_dec 'NNN::$$$message_B_P_L$$$::text::' -> {message_id: text}."""
    pat = re.compile(r'^\d+::\$\$\$(message_\d+_\d+_\d+)\$\$\$::(.*)::\s*$')
    slots = {}
    for line in open(epk_dec_path, encoding='utf-8'):
        m = pat.match(line.rstrip('\n'))
        if m:
            slots[m.group(1)] = m.group(2)
    return slots


def page_lines(slots, message_id, extra=2, cap=3):
    """Text of this beat plus up to `extra` following lines of the same page
    (message_B_P_L grouped by B_P).  -> list of cleaned strings."""
    m = re.match(r'message_(\d+)_(\d+)_(\d+)$', message_id)
    b, p, l = m.group(1), m.group(2), int(m.group(3))
    out = []
    for ll in range(l, l + extra + 1):
        t = slots.get(f'message_{b}_{p}_{ll:04d}')
        if t is None:
            break
        t = TAG_RE.sub('', t).strip()
        if t:
            out.append(t)
        if len(out) >= cap:
            break
    return out


# ================================================================ PoC render
POC_CHAPTER = 'セイバールート一日目'


def render_poc(chapter=POC_CHAPTER, max_beats=MAX_BEATS_PER_SCRIPT):
    """Render the first `max_beats` sprite beats of every script in a chapter.

    -> (sections, totals) where sections is per-script render info used to
    build sprites_preview/index.html.
    """
    os.makedirs(SPRITE_CACHE_DIR, exist_ok=True)
    warnings = WARNINGS
    epk_map = json.load(open(EPK_MAP, encoding='utf-8'))

    import glob
    scripts = sorted(glob.glob(os.path.join(KAG_DIR, f'{chapter}-*.ks')))
    sections = []
    totals = {'scripts': 0, 'beats': 0, 'beats_with_sprites': 0,
              'beats_rendered': 0, 'composites': 0}

    for ks_path in scripts:
        base = os.path.basename(ks_path)
        text = open(ks_path, encoding='utf-8').read()
        beats, events = walk_script(text)

        epk_base = epk_map.get(base)
        slots_text = {}
        if epk_base:
            dec = os.path.join(EPK_DIR, epk_base + '.epk_dec')
            if os.path.exists(dec):
                slots_text = load_text_slots(dec)
            else:
                warnings.append(f'missing epk_dec for {base}: {dec}')
        else:
            warnings.append(f'no epk mapping for {base}')

        rendered = []
        n_with = 0
        for beat in beats:
            if not beat['sprites']:
                continue
            n_with += 1
            if len(rendered) >= max_beats:
                continue
            bg_path = get_bg_jpg(beat['bg'], warnings)
            slots = [{'file': slot['file'], 'pos': pos,
                      'index': slot['index'] or POS_DEFAULT_INDEX[pos],
                      'fliplr': slot['fliplr']}
                     for pos, slot in beat['sprites'].items()]
            path = composite(bg_path, slots, bgcolor=beat['bg_color'])
            rendered.append({'message_id': beat['message_id'],
                             'img': os.path.relpath(path, PREVIEW_DIR).replace(os.sep, '/'),
                             'lines': page_lines(slots_text, beat['message_id']),
                             'bg': beat['bg'] or beat['bg_color'] or '-',
                             'sprites': ', '.join(
                                 f"{s['pos']}:{s['file']}" for s in slots)})

        sections.append({'script': base, 'total_beats': len(beats),
                         'beats_with_sprites': n_with,
                         'beats_rendered': len(rendered), 'events': len(events),
                         'items': rendered})
        totals['scripts'] += 1
        totals['beats'] += len(beats)
        totals['beats_with_sprites'] += n_with
        totals['beats_rendered'] += len(rendered)

    # count unique composites
    seen = set()
    for sec in sections:
        for it in sec['items']:
            seen.add(it['img'])
    totals['composites'] = len(seen)
    return sections, totals, warnings


def build_index(sections, totals, warnings):
    parts = ["""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Sprite compositor PoC — セイバールート一日目</title>
<style>
 body{background:#101014;color:#f2e8d5;font-family:Georgia,'Times New Roman',serif;
      margin:0;padding:24px 0 80px}
 h1{font-size:22px;margin:16px 24px} .stats{color:#9a8f7a;font-size:13px;margin:0 24px 24px}
 h2{font-size:16px;color:#c9b98a;margin:40px 24px 8px;border-bottom:1px solid #2a2a33;
    padding-bottom:6px}
 .secstats{color:#9a8f7a;font-size:12px;margin:0 24px 16px}
 figure{margin:0 24px 28px}
 img{max-width:900px;width:100%;display:block;border:1px solid #2a2a33}
 figcaption{max-width:900px;font-size:14px;line-height:1.55;margin-top:8px;white-space:pre-wrap}
 .meta{color:#77705f;font-size:11px;font-family:Consolas,monospace;margin-top:6px}
 .warn{color:#c96a5a;font-size:12px;margin:0 24px;max-width:900px}
</style></head><body>
<h1>Sprite compositor PoC — セイバールート一日目 (first beats with standing cast)</h1>"""]
    parts.append(f"<p class='stats'>{totals['scripts']} scripts · "
                 f"{totals['beats']} text beats · {totals['beats_with_sprites']} with sprites · "
                 f"{totals['beats_rendered']} rendered · "
                 f"{totals['composites']} unique composites</p>")
    if warnings:
        parts.append('<p class="warn">' + html.escape(
            ' | '.join(sorted(set(warnings))[:20])) + '</p>')
    for sec in sections:
        parts.append(f"<h2>{html.escape(sec['script'])}</h2>")
        parts.append(f"<p class='secstats'>{sec['total_beats']} beats · "
                     f"{sec['beats_with_sprites']} with sprites · "
                     f"{sec['beats_rendered']} rendered</p>")
        for it in sec['items']:
            body = html.escape('\n'.join(it['lines'])) or '(no text)'
            meta = html.escape(f"{it['message_id']}  bg={it['bg']}  "
                               f"[{it['sprites']}]")
            parts.append(f"<figure><img src=\"{html.escape(it['img'])}\" "
                         f"loading=\"lazy\"><figcaption>{body}"
                         f"<div class='meta'>{meta}</div></figcaption></figure>")
    parts.append('</body></html>')
    out = os.path.join(PREVIEW_DIR, 'index.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    return out


def main():
    sections, totals, warnings = render_poc()
    out = build_index(sections, totals, warnings)
    print(f'== sprite beats per script ==')
    for sec in sections:
        print(f"  {sec['script']}: {sec['total_beats']} beats, "
              f"{sec['beats_with_sprites']} with sprites, "
              f"{sec['beats_rendered']} rendered "
              f"({sec['events']} sprite events)")
    print(f"== totals: {totals} ==")
    if warnings:
        print(f'== warnings ({len(set(warnings))} unique) ==')
        for w in sorted(set(warnings)):
            print('  ' + w)
    print(f'index: {out}')


if __name__ == '__main__':
    main()
