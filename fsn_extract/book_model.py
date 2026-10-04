"""Build the complete branch-aware EPUB of Fate/stay night Remaster.

Structure: parts (Prologue / Fate / UBW / HF / Last Episode / Extras) ->
chapters (one XHTML each, from .fcf flowchart + scene scripts) -> scene
sections with inline color art, dialog speaker labels, choice boxes that
hyperlink to the target scene (branch-aware), flag-branch notes, and Tiger
Dojo bad-end asides.

Kobo landscape optimization: light reading theme (e-ink friendly), inline
<img> art plates (Kobo does not reliably render CSS background images),
float-left art with side-by-side text on wide viewports, generous type,
bordered choice boxes with internal links.
"""
import collections
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fsn2epub import (SPEAKERS, TAG_RE, smart_quotes, ImageResolver,
                      MAIN_EXE, KEY_BIN)
import sprites

KAG = os.path.join(HERE, 'kag_all')
STATIC_DEC = os.path.join(HERE, 'raw', 'us_epks', 'statictext.epk_dec')
OUT_DIR = os.path.join(HERE, 'book')
WORK = os.path.join(HERE, 'book_work')

# ---------------------------------------------------------------- text DB
def load_flowtext():
    """flowchart labels (scene titles, choice options) from statictext"""
    slots = {}
    pat = re.compile(r'^\d+::\$\$\$(flowtext_\d+_\d+_\d+)\$\$\$::(.*)::\s*$')
    for line in open(STATIC_DEC, encoding='utf-8'):
        m = pat.match(line.rstrip('\n'))
        if m:
            slots[m.group(1)] = m.group(2)
    return slots


SCRIPT_EPK_MAP = json.load(open(os.path.join(HERE, 'script_epk_map.json'))) \
    if os.path.exists(os.path.join(HERE, 'script_epk_map.json')) else {}
_SLOT_CACHE = {}


def script_slots(script_name):
    """message slots for one -NN.ks script, from its matched text epk"""
    if script_name in _SLOT_CACHE:
        return _SLOT_CACHE[script_name]
    epk = SCRIPT_EPK_MAP.get(script_name)
    slots = {}
    if epk:
        path = os.path.join(HERE, 'raw', 'us_epks', epk + '.epk_dec')
        pat = re.compile(r'^\d+::\$\$\$(message_\d+_\d+_\d+)\$\$\$::(.*)::\s*$')
        try:
            for line in open(path, encoding='utf-8'):
                m = pat.match(line.rstrip('\n'))
                if m:
                    slots[m.group(1)] = m.group(2)
        except OSError:
            pass
    if len(_SLOT_CACHE) > 8:
        _SLOT_CACHE.clear()
    _SLOT_CACHE[script_name] = slots
    return slots


# ---------------------------------------------------------------- fcf
def parse_fcf(path):
    """Returns (nodes, routes) where routes preserve file order."""
    nodes, routes = {}, []
    try:
        data = open(path, encoding='utf-8-sig').read()
    except OSError:
        return nodes, routes
    for line in data.split('\n'):
        line = line.strip()
        if ';' not in line:
            continue
        parts = line.split(';', 1)[1].split("'")
        typ = parts[0]
        if typ == 'ROUTE':
            nums = [p for p in parts[1:] if p and re.fullmatch(r'-?\d+', p)]
            if len(nums) >= 2:
                routes.append((int(nums[-2]), int(nums[-1])))
        elif typ in ('SCENE', 'SELECTER', 'OUTERLABEL'):
            nid = int(parts[1])
            flows = re.findall(r'flowtext_(\d+)_(\d+)_(\d+)', line)
            conds = re.findall(r":([^:'/][^'/]*)//", line)
            nodes[nid] = {'type': typ,
                          'flows': [f'{a}_{b}_{c}' for a, b, c in flows],
                          'conds': [c for c in conds if not c.isdigit()]}
    return nodes, routes


# ---------------------------------------------------------------- ks parse
SAY_RE = re.compile(r'^@say storage=\w+_(\w+)_\d+')
SLOT_RE = re.compile(r'^\$\$\$(message_\d+_\d+_\d+)\$\$\$$')
LABEL_RE = re.compile(r'^\*page(\d+)\|')
# background-setting commands: fadein/bg/rep may use file=, bg= or storage=
# (1290 @fadein storage=black/white clears alone); a2a/i2i/i2o (+ _fast/T/
# _curtain/4demo variants) are transition commands that land a new bg.
# (@rep with c= is a sprite change; storages= (plural) won't match.)
BG_RE = re.compile(
    r'^@(?:fadein4demo|fadein|bg|rep|a2a(?:_fast)?T?|i2i(?:_fast)?T?|'
    r'i2o(?:_fast|_curtain)?T?)\s+(?:file|bg|storage)=(\S+)')
BG2_RE = re.compile(r'^@image(?:ex)?\s+storage=(\S+)')
# BG_RE above only matches when file=/bg=/storage= comes first; ~1,200 lines
# put time=/rule=/fliplr= first (@fadein time=600 file=a41, @rep fliplr=0
# storages=... bg=...). parse_bg_cmd reads the params in any order.
BG_CMD_RE = re.compile(
    r'^@(fadein4demo|fadein|bg|rep|a2a(?:_fast)?T?|i2i(?:_fast)?T?|'
    r'i2o(?:_fast|_curtain)?T?)\s(.*)$')
DASH_CMD_RE = re.compile(r'^@(dash(?:combo)?T?)\s(.*)$')
IMAGE_RE = re.compile(r'^@image(?:ex)?\s')
PARAM_RE = re.compile(r'(\w+)=(\S+)')
# @contrast/@contrastT set a screen-wide tone filter (level=100 = neutral,
# <100 crushes shadows, >100 lifts them, negative dims the whole screen);
# @contrastoff/@contrastoffT clear it. Calibrated against the retail game
# via YouTube frames: level 60 measured gain ~0.2-0.3 in shadows, level 62
# ~0.8 at midtones, level -120 ~0.6-0.8 everywhere (see lp_audit).
CONTRAST_RE = re.compile(r'^@contrast(off)?T?\b')
CONTRAST_GAMMA_P = 1.1     # out = 255*(in/255)^((100/level)^p), level > 0
CONTRAST_NEG_DIV = 400.0   # gain = 1 + level/400, level < 0 (floor 0.15)


def contrast_lut(level):
    """256-entry tone curve for a contrast level, or None if neutral."""
    if level is None:
        return None
    level = int(level)
    if level == 100:
        return None
    if level > 0:
        gamma = min((100.0 / level) ** CONTRAST_GAMMA_P, 4.5)
        return [min(255, round(255.0 * (i / 255.0) ** gamma)) for i in range(256)]
    gain = max(0.15, 1.0 + level / CONTRAST_NEG_DIV)
    return [min(255, round(i * gain)) for i in range(256)]


def apply_contrast(resolver, base, level):
    """Tone-mapped copy of a resolved slide image under an active
    @contrast level -> resolver-style (filename, w, h)."""
    from PIL import Image
    lut = contrast_lut(level)
    if lut is None:
        return base
    key = hashlib.md5(repr((base[0], int(level))).encode('utf-8')).hexdigest()[:12]
    fn = f'ct_{key}.jpg'
    out = os.path.join(resolver.cachedir, fn)
    if os.path.exists(out):
        w, h = Image.open(out).size
        return (fn, w, h)
    im = Image.open(os.path.join(resolver.cachedir, base[0])).convert('RGB')
    im = im.point(lut * 3)
    im.save(out, quality=86)
    return (fn, im.width, im.height)


# @fadein/@bg clear the standing cast unless noclear=1 (693 script lines
# pass noclear to keep characters placed on the back page beforehand)
CLEARING_BG_CMDS = {'fadein', 'fadein4demo', 'bg'}
REP_POS = {'l': 'left', 'lc': 'leftcenter', 'c': 'center',
           'rc': 'rightcenter', 'r': 'right'}


def truthy(v):
    return v in ('true', '1')


def parse_bg_cmd(ln):
    """-> (cmd, name, fliplr, flipud, params) for a background command,
    else None. @rep's storages= are layer images, not the bg (that is bg=)."""
    m = BG_CMD_RE.match(ln)
    if not m:
        return dash_bg_cmd(ln)
    p = dict(PARAM_RE.findall(m.group(2)))
    cmd = m.group(1)
    name = p.get('file') or p.get('bg') or \
        (p.get('storage') if cmd != 'rep' else None)
    if not name:
        return None
    return (cmd, name, truthy(p.get('fliplr', '')),
            truthy(p.get('flipud', '')), p)


def dash_bg_cmd(ln):
    """@dash/@dashcombo/@dashcomboT zoom-in a storage onto a layer. With
    layer=base or page=back and (near-)full opacity (>= 200; the script typo
    `opasity` counts, default 255) it lands a new background (CGs such as
    b_cs13...). Lower opacities are flagged params['_low']: over a scene
    they are zoom-blur effects (06火花 opacity=64 during a fight), but on a
    black/white screen the stacked blur copies ARE the picture (Prologue
    Day 1: @bg file=black, then @dashcombo storage=B16 opacity=32 shows
    Tokiomi's hands) — parse_scene decides. Not a clearing cmd: the cast
    and fg art stay."""
    m = DASH_CMD_RE.match(ln)
    if not m:
        return None
    p = dict(PARAM_RE.findall(m.group(2)))
    name = p.get('storage')
    if not name or not (p.get('layer') == 'base' or p.get('page') == 'back'):
        return None
    digits = re.sub(r'\D', '', p.get('opacity', p.get('opasity', '255')))
    if int(digits or 255) < 200:
        p['_low'] = '1'
    return (m.group(1), name, truthy(p.get('fliplr', '')),
            truthy(p.get('flipud', '')), p)


# @dash zooms the camera to `mag` around a fixed point (cx, cy). Calibrated
# against two gameplay screenshots of Prologue Day 1 (B16 at mag=2 cx=450
# cy=600, and mag=2.1 cx=250 cy=324; template-match correlation 0.90/0.81):
# the fixed point in 2040x1200 art pixels is (2.36*cx, 1.80*cy), and the
# screen is the centred 1920x1080 window of the art.
DASH_CX_SCALE, DASH_CY_SCALE = 2.36, 1.80
SCREEN_WIN = (60, 60, 1920, 1080)     # x, y, w, h of the screen in art px
# mag 4-8 dashes are fast spark/impact zoom-blurs: a literal 1/8 crop is
# an unreadable smear, so slides frame them at most this close
DASH_MAX_MAG = 3.0


def dash_zoom(p):
    """@dash params -> (cx, cy, mag) the camera settles on, or None for no
    zoom. cx/cy None = screen centre (cx=c)."""
    try:
        mag = float(p.get('mag', '1'))
    except ValueError:
        return None
    if mag <= 1.01:
        return None

    def coord(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None              # 'c' or missing: screen centre
    return (coord(p.get('cx')), coord(p.get('cy')), min(mag, DASH_MAX_MAG))


def zoom_crop(resolver, base, base_name, zoom):
    """Crop a resolved bg to the window a @dash zoom ends on.
    -> resolver-style (filename, w, h)."""
    from PIL import Image
    key = hashlib.md5(repr((base[0], zoom)).encode('utf-8')).hexdigest()[:12]
    fn = f'zm_{key}.jpg'
    out = os.path.join(resolver.cachedir, fn)
    if os.path.exists(out):
        w, h = Image.open(out).size
        return (fn, w, h)
    im = Image.open(os.path.join(resolver.cachedir, base[0])).convert('RGB')
    native_w = (art_dims(base_name) or (im.width,))[0]
    s = im.width / native_w
    cx, cy, m = zoom
    sx, sy, sw, sh = SCREEN_WIN
    fx = sx + sw / 2 if cx is None else cx * DASH_CX_SCALE
    fy = sy + sh / 2 if cy is None else cy * DASH_CY_SCALE
    w, h = sw / m, sh / m
    x0 = min(max(0, fx + (sx - fx) / m), native_w - w)
    y0 = min(max(0, fy + (sy - fy) / m), im.height / s - h)
    box = tuple(round(v * s) for v in (x0, y0, x0 + w, y0 + h))
    im = im.crop(box).resize((1600, round(1600 * h / w)), Image.LANCZOS)
    im.save(out, quality=86)
    return (fn, im.width, im.height)


def rep_position(pos, left, taken=()):
    """@rep poss= entry; an empty one places by lefts= pixel offset at the
    nearest slot not already taken by another entry of the same @rep (the
    pixel placement is free-form; rounding two characters onto one slot
    would make the second replace the first)"""
    if pos in REP_POS:
        return REP_POS[pos]
    try:
        frac = (float(left) + 340) / 1920
    except (TypeError, ValueError):
        frac = 0.5
    for k in sorted(sprites.POS_ANCHOR,
                    key=lambda k: abs(sprites.POS_ANCHOR[k] - frac)):
        if k not in taken:
            return k
    return 'center'


# Full-screen scene art the game shows on a FOREGROUND layer (@fg storage=a41
# top=200 between letterbox bars, @rep storages=<CG>, @movefg pans). Solid
# fills, flashes, letterbox bars, cracks and blood splashes are effects.
FG_EFFECT_RE = re.compile(r'^(black|white|red\d?|特殊[白黒]|066_upperblack|'
                          r'シネスコ|ヒビ|こぼれる血|血管|ダミー|EDfont|光|白光|'
                          r'letext|P01通常軌跡|\d+ダメージ|11爆発)')
_ART_DIMS = {}
_DIM_RESOLVER = []


def _art_resolver():
    if not _DIM_RESOLVER:
        _DIM_RESOLVER.append(ImageResolver(os.path.join(HERE, 'img')))
    return _DIM_RESOLVER[0]


def art_image(name):
    """decoded RGBA image of a manifest entry, or None"""
    import io
    from PIL import Image
    res = _art_resolver()
    ent = res._lookup(name)
    if not ent:
        return None
    dat, off, size = ent
    with open(res._blob(dat), 'rb') as f:
        f.seek(off)
        return Image.open(io.BytesIO(f.read(size))).convert('RGBA')


def art_dims(name):
    """native (w, h) of a manifest image, or None"""
    if name not in _ART_DIMS:
        import io
        from PIL import Image
        res = _art_resolver()
        ent = res._lookup(name)
        dims = None
        if ent:
            dat, off, size = ent
            with open(res._blob(dat), 'rb') as f:
                f.seek(off)
                dims = Image.open(io.BytesIO(f.read(size))).size
        _ART_DIMS[name] = dims
    return _ART_DIMS[name]


_ART_OPAQUE = {}


def art_opaque(name):
    """True when an art image covers the screen (>=95% opaque pixels);
    cloud/grass/smoke layers are mostly transparent"""
    if name not in _ART_OPAQUE:
        im = art_image(name)
        ok = True
        if im is not None:
            h = im.getchannel('A').histogram()
            ok = h[255] >= 0.95 * sum(h)
        _ART_OPAQUE[name] = ok
    return _ART_OPAQUE[name]


def is_fg_art(name):
    """non-sprite, non-effect image at least 1200x600 (full-screen art)"""
    if not name or sprites.is_sprite_name(name) or FG_EFFECT_RE.match(name):
        return False
    d = art_dims(name)
    return bool(d) and d[0] >= 1200 and d[1] >= 600


SCREEN_W, SCREEN_H = 2040, 1200     # game screen in script pixels


def layer_composite(resolver, base, base_name, info):
    """Foreground-layer CG as the game frames it. info = ((left, top),
    ((name, dx, dy), ...)): higher art layers are stacked over the CG
    (Last Episode finale: csラストep07 + its transparent grass layer), and
    panoramas wider than the slide filter allows (3480x1200 pans) are
    cropped to the screen window the scene opens on.
    base is the resolver result for base_name -> (filename, w, h)."""
    from PIL import Image
    (bx, by), layers, fill = info
    wide = not 0.45 < base[1] / base[2] < 2.6
    if not layers and not wide:
        return base
    key = hashlib.md5(repr((base[0], info)).encode('utf-8')).hexdigest()[:12]
    fn = f'lay_{key}.jpg'
    out = os.path.join(resolver.cachedir, fn)
    if os.path.exists(out):
        w, h = Image.open(out).size
        return (fn, w, h)
    canvas = Image.open(os.path.join(resolver.cachedir, base[0])).convert('RGBA')
    s = canvas.width / (art_dims(base_name) or (canvas.width,))[0]
    for name, dx, dy in layers:
        im = art_image(name)
        if im is None:
            continue
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                       Image.LANCZOS)
        over = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
        over.paste(im, (round(dx * s), round(dy * s)))
        canvas = Image.alpha_composite(canvas, over)
    if wide or layers:
        # layer offsets are screen placements: show the screen window
        x0 = min(max(0, round(-bx * s)), max(0, canvas.width - round(SCREEN_W * s)))
        y0 = min(max(0, round(-by * s)), max(0, canvas.height - round(SCREEN_H * s)))
        canvas = canvas.crop((x0, y0, min(canvas.width, x0 + round(SCREEN_W * s)),
                              min(canvas.height, y0 + round(SCREEN_H * s))))
    flat = Image.new('RGB', canvas.size,
                     (255, 255, 255) if fill == 'white' else (0, 0, 0))
    flat.paste(canvas, (0, 0), canvas)
    canvas = flat
    if canvas.width > 1600:
        canvas = canvas.resize((1600, round(canvas.height * 1600 / canvas.width)),
                               Image.LANCZOS)
    canvas.save(out, quality=86)
    return (fn, canvas.width, canvas.height)


# speaker-change bridging keeps a character drawn through a gap of at most
# this many text beats (a longer gap is narration the game shows without them)
BRIDGE_MAX_BEATS = 2
DOJO_CODES = {'dtg', 'dir', 'tig', 'iri', 'ksh'}
# wordmark / logo images: menu splash art, not scene backgrounds
WORDMARKS = {'fate', 'ubw', 'hf', 'unlimitedbladeworks', 'heavensfeel',
             'realta', 'lastepisode', 'title', 'fate_b'}

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']


def format_date(code):
    """@date_title date=MMDD game code (131 = January 31) -> label"""
    d = (code or '').lstrip('0') or '0'
    if not d.isdigit():
        return '—'
    month = day = None
    if len(d) == 3:
        month, day = int(d[0]), int(d[1:])
    elif len(d) == 4:
        if 10 <= int(d[:2]) <= 12 and 1 <= int(d[2:]) <= 31:
            month, day = int(d[:2]), int(d[2:])
        elif 1 <= int(d[0]) <= 9 and 1 <= int(d[1:]) <= 31:
            month, day = int(d[0]), int(d[1:])
    if month and day and 1 <= month <= 12 and 1 <= day <= 31:
        return f'{MONTHS[month - 1]} {day}'
    return '—'


def clean_text(s):
    """Strip KAG pacing/layout tags; mark [font italic=...] toggles with
    placeholder tokens (\x01/\x02) — the renderer pairs them per paragraph,
    since italic spans can cross the game's line breaks."""
    s = smart_quotes(TAG_RE.sub(' ', s))
    s = re.sub(r'\[font italic=true\]', '\x01', s)
    s = re.sub(r'\[font italic=[^\]]*\]', '\x02', s)
    # remaining KAG-ish tags: lowercase word, optional params
    s = re.sub(r'\[/?[a-z][a-z0-9_ =.]*\]', '', s)
    return esc(s)


def parse_scene(text, slots):
    """-> list of blocks: {'img': (name, fliplr, flipud)} | {'p': [...beats]}
    | {'day': n} | {'dojo-start': True} | {'movie': name}
    | {'route': label} | {'ending': label}"""
    blocks = []
    img_buf = []          # pending images; only the settled one is kept
    paras = []            # list of lines; line = list of beats
    cur = []
    speaker = None
    last_dlg = None
    bg = None
    dojo = False
    dojo_enabled = '@tiger_end' in text
    # standing character cast: sprite commands evolve slots; a cast change
    # emits a new img block (with 'sprites') so render_blocks starts a new
    # slide, exactly like a background change does
    spr_state = sprites.SpriteState()
    last_vis = [None]     # last emitted (img-tuple, sprite key)
    fgcg = {}             # index -> full-screen art on a foreground layer
    fgpos = {}            # index -> (left, top) script placement of that art
    zoom = [None]         # @dash camera zoom on the current bg (dash_zoom)
    contrast = [None]     # active @contrast level (None = neutral)

    # Overlay/strip families: names the script places on layers >= 1 are
    # parallax strips or sprite pieces composited over the background, not
    # backgrounds themselves (e.g. 102無限の剣製・呪文a..h on layers 0-7;
    # the real bg fades in beforehand via @fadein). Skip the whole family,
    # including its layer-0 member.
    strip_names = set()
    for ln in text.split('\n'):
        m = re.match(r'^@image(?:ex)?\s+storage=(\S+)', ln.strip())
        if m and re.search(r'layer=[1-9]', ln):
            n = m.group(1)
            strip_names.add(n)
            strip_names.add(re.sub(r'[a-z]$', '', n))

    def is_background(ln, name):
        # a character sprite slid in on an image layer is not a backdrop
        if sprites.is_sprite_name(name):
            return False
        lay = re.search(r'layer=(\S+)', ln)
        # layer=base loaded on the back page is shown by the next @trans
        if lay is not None and lay.group(1) == 'base':
            return True
        if name in strip_names:
            return False
        if 'page=back' in ln:
            return False
        # layer 0 also carries caption cards (舞台説明橋), chibi pieces and
        # credit fonts faded in over the scene: only full-screen art counts
        return (lay is None or lay.group(1) == '0') and is_fg_art(name)

    def visual():
        """what fills the screen: foreground-layer art covers the bg"""
        # the lowest foreground art is the scene CG; higher art layers
        # (layers()) are composited over it
        base, _ = art_stack()
        if base is None:
            return bg
        return (fgcg[base], False, False)

    def art_stack():
        """-> (index of the foreground art the stack starts from, or None
        to start from the bg; indexes of the art layers above it). The
        stack starts at the topmost OPAQUE art; transparent layers (clouds,
        grass, smoke) need what is beneath them."""
        if not fgcg:
            return None, []
        order = sorted(fgcg)
        opaque = [i for i in order if art_opaque(fgcg[i])]
        if opaque:
            base = opaque[-1]
        elif bg and bg[0] not in ('black', 'white'):
            base = None
        else:
            base = order[0]
        return base, [i for i in order if base is None or i > base]

    def layers():
        """foreground art placement: ((left, top) of the stack base,
        ((name, dx, dy), ...) art layers relative to it, flatten colour)"""
        if not fgcg:
            return ()
        base, above = art_stack()
        bx, by = fgpos.get(base, (0, 0)) if base is not None else (0, 0)
        fill = 'white' if bg and bg[0] == 'white' else 'black'
        return ((bx, by),
                tuple((fgcg[i], fgpos.get(i, (0, 0))[0] - bx,
                       fgpos.get(i, (0, 0))[1] - by) for i in above),
                fill)

    def cur_zoom():
        # the camera zoom frames the bg; foreground art replaces that view
        return None if fgcg else zoom[0]

    def vkey():
        # contrast rides in the key: a level change over unchanged art
        # still starts a new (filtered) slide
        return (visual(), layers(), cur_zoom(), contrast[0])

    def set_art(idx, name, p):
        fgcg[idx] = name
        try:
            fgpos[idx] = (float(p.get('left', 0) or 0), float(p.get('top', 0) or 0))
        except ValueError:
            fgpos[idx] = (0.0, 0.0)

    def queue_visual(img=False):
        # the cast is captured NOW: later sprite ops/clears must not leak
        # into a background queued before them; the contrast level rides
        # along for the same reason (the tone the art settled under)
        img_buf.append((None if img is None else visual(),
                        spr_state.snapshot(), spr_state.soft_key(),
                        () if img is None else layers(),
                        None if img is None else cur_zoom(),
                        None if img is None else contrast[0]))

    def flush_line():
        nonlocal cur
        if cur:
            paras.append(cur)
            cur = []

    def flush_paras():
        nonlocal paras
        # close the line in progress too: beats before a mid-line visual
        # change belong to the state before it
        flush_line()
        flush_img_buf()
        if paras:
            blocks.append({'p': paras})
            paras = []

    def flush_img_buf():
        # emit only the last image of a run (the settled background);
        # drops rapid effect chains and trailing flashes; None = screen
        # cleared to black/white. The standing cast rides along so the
        # slide art is the composited (bg + cast) frame.
        nonlocal img_buf
        if img_buf:
            img, snap, sk, lay, zm, ct = img_buf[-1]
            blocks.append({'img': img, 'sprites': snap or None,
                           'layers': lay, 'zoom': zm, 'ct': ct})
            last_vis[0] = ((img, lay, zm, ct), sk)
            img_buf = []

    def emit_visual():
        """Emit the current bg + cast as an img block. Deduped on the SOFT
        cast key: expression/pose swaps within one outfit don't split slides
        (the slide freezes the cast as it stood when it opened); membership,
        position, outfit and strong-state changes do."""
        snap = spr_state.snapshot()
        vis = (vkey(), spr_state.soft_key())
        if vis != last_vis[0]:
            blocks.append({'img': visual(), 'sprites': snap or None,
                           'layers': layers(), 'zoom': cur_zoom(),
                           'ct': contrast[0]})
            last_vis[0] = vis

    for raw in text.split('\n'):
        ln = raw.strip()
        if not ln or ln.startswith(';'):
            continue
        if LABEL_RE.match(ln) or (ln.startswith('*') and not SLOT_RE.match(ln)):
            flush_line()
            continue
        m = SAY_RE.match(ln)
        if m:
            speaker = m.group(1)
            if dojo_enabled and speaker in DOJO_CODES and not dojo:
                dojo = True
                flush_line()
                blocks.append({'dojo-start': True})
            continue
        if re.match(r'^@talk', ln) or ln in ('@tiger_start',
                                             '@approachTigerSchool'):
            if dojo_enabled and not dojo:
                dojo = True
                flush_line()
                blocks.append({'dojo-start': True})
            continue
        if ln in ('@pg', '@pgnl', '@pgtg', '@r', '@l', '@er', '@p'):
            flush_line()
            continue
        if ln.startswith('@black') or re.match(r'^@white(?:out)?\b', ln):
            # screen goes dark/light: clear the art so later text does not
            # ride the previous scene's image; the standing cast is wiped
            # with it (the game re-places characters after the fade)
            flush_paras()
            spr_state.slots.clear()
            fgcg.clear()
            queue_visual(None)
            bg = ('black', False, False)
            zoom[0] = None
            continue
        bgc = parse_bg_cmd(ln)
        if bgc:
            cmd, name, flr, fud, p = bgc
            if p.get('_low') and (fgcg or (bg and bg[0].lower()
                                           not in ('black', 'white'))):
                continue   # low-opacity zoom blur over an existing scene
            before = (vkey(), spr_state.soft_key())
            flush_paras()
            if ((name.lower() in ('black', 'white') and not p.get('storages'))
                    or (cmd in CLEARING_BG_CMDS
                        and not truthy(p.get('noclear', '')))):
                spr_state.slots.clear()
                fgcg.clear()
            if cmd == 'rep' and p.get('storages'):
                # @rep replaces the whole foreground: storages= are the new
                # cast (poss=/lefts=) and/or full-screen art (indexes=)
                spr_state.slots.clear()
                fgcg.clear()
                poss = p.get('poss', '').split(',')
                lefts = p.get('lefts', '').split(',')
                tops = p.get('tops', '').split(',')
                idxs = p.get('indexes', '').split(',')
                # named slots first, so pixel-placed entries avoid them
                taken = {REP_POS[x] for x in poss if x in REP_POS}
                for k, s in enumerate(p['storages'].split(',')):
                    idx = (int(idxs[k]) if k < len(idxs) and idxs[k].isdigit()
                           else None)
                    if sprites.is_sprite_name(s):
                        named = poss[k] if k < len(poss) else 'c'
                        pos = rep_position(named,
                                           lefts[k] if k < len(lefts) else None,
                                           taken)
                        taken.add(pos)
                        spr_state.apply([('ld', pos, s, idx, False)])
                    elif is_fg_art(s):
                        set_art(idx or 1000 + k, s,
                                {'left': lefts[k] if k < len(lefts) else 0,
                                 'top': tops[k] if k < len(tops) else 0})
            bg = (name, flr, fud)
            zoom[0] = dash_zoom(p) if cmd.startswith('dash') else None
            if (vkey(), spr_state.soft_key()) != before:
                queue_visual()
            continue
        if IMAGE_RE.match(ln):
            m = BG2_RE.match(ln) or re.match(r'^@image(?:ex)?\s.*?storage=(\S+)', ln)
            if m and is_background(ln, m.group(1)):
                new = (m.group(1), bool(re.search(r'fliplr=(?:true|1)\b', ln)),
                       bool(re.search(r'flipud=(?:true|1)\b', ln)))
                flush_paras()
                if new != bg:
                    bg = new
                    zoom[0] = None
                    queue_visual()
            continue
        m = re.match(r'@(fg|movefg)\s(.*)$', ln)
        if m:
            p = dict(PARAM_RE.findall(m.group(2)))
            n = p.get('storage') or p.get('file') or ''
            if n and not sprites.is_sprite_name(n):
                before = vkey()
                idx = int(p['index']) if p.get('index', '').isdigit() else 1000
                if m.group(1) == 'movefg':
                    if p.get('opacity') == '0':
                        for k in [k for k, v in fgcg.items() if v == n]:
                            del fgcg[k]
                    elif n not in fgcg.values() and is_fg_art(n):
                        set_art(idx, n, p)
                elif is_fg_art(n) and p.get('opacity', '255') != '0':
                    set_art(idx, n, p)
                else:
                    fgcg.pop(idx, None)   # an effect layer replaces it
                if vkey() != before:
                    flush_paras()
                    queue_visual()
                continue
            if m.group(1) == 'movefg':
                continue
        if CONTRAST_RE.match(ln) and os.environ.get('FSN_NO_CONTRAST') != '1':
            # screen tone filter (@contrastT/@contrast set, off-variants
            # clear); the level persists across bg changes until turned off
            off = bool(re.match(r'^@contrastoff', ln))
            lvl = re.search(r'level=(-?\d+)', ln)
            new_ct = None if off else (int(lvl.group(1)) if lvl else 100)
            if new_ct != contrast[0]:
                before = vkey()
                contrast[0] = new_ct
                if vkey() != before and (bg or fgcg):
                    flush_paras()
                    queue_visual()
            continue
        if ln.startswith('@playmovie'):
            m2 = re.search(r'storage=(\S+)', ln)
            flush_paras()
            blocks.append({'movie': m2.group(1) if m2 else ''})
            continue
        if ln.startswith('@selectroute'):
            # route lock: the player's path is now fixed to one heroine
            m2 = re.search(r'route=(\S+)', ln)
            label = ROUTE_PREFIX.get(m2.group(1)) if m2 else None
            if label:
                flush_paras()
                blocks.append({'route': label})
            continue
        if ln.startswith('@unlockachievement'):
            # only the five route-ending achievements become dividers;
            # dojo badges (0021) and milestones are ignored silently
            m2 = re.search(r'id=achievement_(\d+)', ln)
            label = ENDINGS.get(m2.group(1)) if m2 else None
            if label:
                flush_paras()
                blocks.append({'ending': label})
            continue
        if ln.startswith('@date_title'):
            m2 = re.search(r'date=(\d+)', ln)
            flush_paras()
            blocks.append({'day': format_date(m2.group(1) if m2 else '')})
            continue
        m = re.match(r'@(\w+)(.*)$', ln)
        if m and (m.group(1) in sprites.SPRITE_CMDS
                  or m.group(1).startswith('ldall')):
            ops = sprites.parse_line_sprite_ops(m.group(1), m.group(2))
            p = dict(PARAM_RE.findall(m.group(2)))
            new_cg = dict(fgcg)
            if any(o[0] == 'clall' for o in ops):
                new_cg = {}
            elif m.group(1) == 'clfg' and p.get('storage') and not p.get('pos'):
                gone = set(p['storage'].split(','))
                new_cg = {k: v for k, v in new_cg.items() if v not in gone}
            if ops or new_cg != fgcg:
                probe = copy.deepcopy(spr_state)
                probe.apply(ops)
                changed = (probe.soft_key() != spr_state.soft_key()
                           or new_cg != fgcg)
                if changed:
                    # cast changed at slide level: text so far and any
                    # pending bg belong to the state BEFORE this op
                    flush_paras()
                spr_state.apply(ops)
                fgcg.clear()
                fgcg.update(new_cg)
                if changed:
                    emit_visual()
            continue
        if ln.startswith('@'):
            continue
        m = SLOT_RE.match(ln)
        if m:
            t = slots.get(m.group(1))
            if t is None:
                continue
            stripped = t.strip()
            centered = (len(t) - len(t.lstrip(' '))) >= 6
            is_dlg = bool(stripped) and stripped[0] in '"\u201c「'
            spk = None
            if speaker:
                if speaker in SPEAKERS:
                    spk = SPEAKERS[speaker]
                elif re.match(r'^(?:ot|tw|th|ksh|cat)', speaker):
                    spk = 'Tiger Dojo'   # dojo assistant variants
                else:
                    spk = speaker.capitalize()
                last_dlg = spk
            elif is_dlg and last_dlg and cur:
                spk = last_dlg
            clean = clean_text(stripped)
            cur.append({'text': clean, 'speaker': spk if is_dlg else None,
                        'centered': centered})
            speaker = None
    flush_line()
    flush_paras()

    # Speaker-change bridging: the game crossfades the standing cast out
    # while the POV character speaks and re-places them a couple of lines
    # later (@cl -> Rin's lines -> @ld same character). In a static book the
    # character blinking out and back looks like a rendering error, so keep
    # the cast standing through brief gaps when the same character returns
    # on the same background.
    def _cast_key(sprs):
        return tuple(sorted(sprites.char_key(s['file']) for s in sprs))

    i = 0
    while i < len(blocks):
        b = blocks[i]
        if not (isinstance(b, dict) and b.get('sprites')):
            i += 1
            continue
        key, bgv = _cast_key(b['sprites']), b['img']
        j, clears = i + 1, 0
        while j < len(blocks):
            nb = blocks[j]
            if isinstance(nb, dict) and 'img' in nb:
                if nb['img'] != bgv or nb.get('sprites'):
                    break
                clears += 1
                if clears > 3:
                    break
            j += 1
        gap_beats = sum(len(line) for k in range(i + 1, min(j, len(blocks)))
                        if 'p' in blocks[k] for line in blocks[k]['p'])
        if (j < len(blocks) and isinstance(blocks[j], dict)
                and blocks[j].get('sprites') and blocks[j]['img'] == bgv
                and _cast_key(blocks[j]['sprites']) == key
                and gap_beats <= BRIDGE_MAX_BEATS):
            for k in range(i + 1, j):
                g = blocks[k]
                if (isinstance(g, dict) and 'img' in g and g['img'] == bgv
                        and not g.get('sprites')):
                    g['sprites'] = [dict(s) for s in b['sprites']]
        i = max(i + 1, j)
    return blocks


# ---------------------------------------------------------------- book model
JP_DAYS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
           '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12, '十三': 13,
           '十四': 14, '十五': 15, '十六': 16}


def day_no(jp):
    m = re.match(r'([一二三四五六七八九十]+)日目', jp)
    if not m:
        m2 = re.fullmatch(r'[一二三四五六七八九十]+', jp)
        return JP_DAYS[m2.group(0)] if m2 else None
    return JP_DAYS[m.group(1)]


def chapter_sort(name):
    m = re.match(r'(セイバー|凛|桜)ルート(.+)', name)
    if m:
        d = day_no(m.group(2))
        return (0, d or 99) if d else (1, 0)
    return (2, 0)


ROUTE_PREFIX = {'セイバー': 'Fate', '凛': 'Unlimited Blade Works',
                '桜': 'Heaven\u2019s Feel'}

# @unlockachievement ids that mark a route ending (from the hiscore config);
# every other achievement id is ignored
ENDINGS = {
    '0001': 'Fate \u2014 Saber\u2019s Ending',
    '0002': 'Unlimited Blade Works \u2014 Rin\u2019s Good Ending',
    '0003': 'Unlimited Blade Works \u2014 Rin\u2019s True Ending',
    '0004': 'Heaven\u2019s Feel \u2014 Sakura\u2019s Normal Ending',
    '0005': 'Heaven\u2019s Feel \u2014 Sakura\u2019s True Ending',
}

# chapters that constitute each ending (achievement unlocks are
# program-side, so key off the epilogue chapters themselves)
ENDING_CHAPTERS = {
    'セイバーエピローグ': ENDINGS['0001'],
    '凛エピローグ': ENDINGS['0002'],
    '凛エピローグ2': ENDINGS['0003'],
    '桜エピローグ': ENDINGS['0004'],
    '桜エピローグ2': ENDINGS['0005'],
    'ラストエピソード': 'Last Episode',
}


def build_chapter_index():
    groups = collections.defaultdict(dict)
    for f in sorted(os.listdir(KAG)):
        if not f.endswith('.ks'):
            continue
        m = re.match(r'(.+?)(?:-(\d+))?\.ks', f)
        groups[m.group(1)][int(m.group(2) or 0)] = f
    parts = collections.OrderedDict()
    parts['Prologue'] = []
    parts['Fate'] = []
    parts['Unlimited Blade Works'] = []
    parts['Heaven\u2019s Feel'] = []
    parts['Last Episode'] = []
    parts['Extras'] = []
    for name in groups:
        if re.match(r'プロローグ\d日目', name):
            parts['Prologue'].append(name)
        elif name.startswith('セイバー'):
            parts['Fate'].append(name)
        elif name.startswith('凛'):
            parts['Unlimited Blade Works'].append(name)
        elif name.startswith('桜'):
            parts['Heaven\u2019s Feel'].append(name)
        elif name == 'ラストエピソード':
            parts['Last Episode'].append(name)
        else:
            parts['Extras'].append(name)
    for k in parts:
        if k == 'Extras':
            parts[k].sort()
        else:
            parts[k].sort(key=chapter_sort)
    # human titles
    titles = {}
    for name in groups:
        m = re.match(r'(セイバー|凛|桜)ルート(.+?)(日目)?$', name)
        if m and m.group(3):
            titles[name] = f'Day {day_no(m.group(2))}'
        elif name == 'セイバーエピローグ':
            titles[name] = 'Epilogue'
        elif name == '凛エピローグ':
            titles[name] = 'Epilogue I'
        elif name == '凛エピローグ2':
            titles[name] = 'Epilogue II \u2014 sunny life'
        elif name == '桜エピローグ':
            titles[name] = 'Epilogue I'
        elif name == '桜エピローグ2':
            titles[name] = 'Epilogue II'
        elif re.match(r'プロローグ\d日目', name):
            m2 = re.match(r'プロローグ(\d)日目', name)
            titles[name] = f'Day {m2.group(1)}'
        elif name == 'ラストエピソード':
            titles[name] = 'Last Episode'
        elif name == '凛エピローグ2sub':
            titles[name] = 'Epilogue II — sunny life (alt)'
        elif name.startswith('ミニ劇場'):
            titles[name] = 'Mini Theater ' + name[-1]
        elif 'タイガー道場' in name and name.endswith('sub'):
            titles[name] = 'Tiger Dojo Special (alt)'
        elif 'タイガー道場' in name:
            titles[name] = 'Tiger Dojo Special'
        else:
            titles[name] = name
    return groups, parts, titles


# ---------------------------------------------------------------- rendering
def esc(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


# movie posters available to render_blocks ('op01' -> 'poster_op01');
# populated by build_book from img/poster_opNN.jpg when they exist
BOOK_POSTERS = {}


def render_blocks(blocks, resolver, images):
    """Renders scene blocks as 'slides': each background change opens a new
    slide div whose art is drawn via CSS background-image (KOReader cre
    needs 'cover cover'/'contain contain', no gradients), followed by a
    separate solid text panel so the artwork is never covered."""
    slides = []           # (img_or_None, html[, raw]) — raw skips the tp wrap
    cur_img = None
    cur_content = []
    dojo_open = False
    em_open = False
    aside_reopen = [False]
    # fixed art present in this source tree? (checked once per scene)
    deadend_ok = os.path.exists(os.path.join(HERE, 'img', 'dead_end.jpg'))
    deadend_shown = [False]   # DEAD END card goes in once per scene

    def flush():
        nonlocal cur_content
        if not cur_content:
            return
        if aside_reopen[0]:
            cur_content.insert(0, '<aside class="dojo">'
                                      '<p class="dojo-title">Tiger Dojo</p>')
            aside_reopen[0] = False
        if cur_img:
            images.add(cur_img)
        # Split into page-sized chunks that all repeat the same background,
        # like the game advancing text pages over a fixed scene. Sized by
        # ESTIMATED rendered lines (~8 per slide) so each slide fits one
        # reader page and the background is never sliced across pages.
        # Tiger Dojo asides may be split too: each chunk closes its <aside>
        # and the next reopens it (aside_reopen handles the flush boundary).
        depth = 0
        chunk, lines = [], 0
        pending_open = False

        def close_chunk(is_last):
            nonlocal chunk, lines, pending_open
            if not chunk:
                return
            if depth > 0:
                chunk.append('</aside>')
                if is_last:
                    aside_reopen[0] = True
                else:
                    pending_open = True
            slides.append((cur_img, '\n'.join(chunk)))
            chunk, lines = [], 0

        for item in cur_content:
            item_lines = est_lines(item)
            if chunk and lines + item_lines > SLIDE_MAX_LINES:
                close_chunk(is_last=False)
            if pending_open:
                chunk.append('<aside class="dojo">'
                             '<p class="dojo-title">Tiger Dojo</p>')
                pending_open = False
            chunk.append(item)
            lines += item_lines
            if '<aside' in item:
                depth += 1
            if '</aside>' in item:
                depth = max(0, depth - 1)
        close_chunk(is_last=True)
        cur_content = []

    for b in blocks:
        if 'img' in b:
            if b['img'] is None:
                # screen cleared to black/white: next text gets no art
                flush()
                cur_img = None
                continue
            # Tiger Dojo skits get their art too (dojo set + Taiga/Illya);
            # the slide chunker closes and reopens the aside across slides
            name, flr, fud = b['img']
            if name in ('black', 'white'):
                flush()
                cur_img = None
                continue
            if name.lower() in WORDMARKS:
                continue
            if any(w in name for w in ('シネスコ', 'typemoon', '黒帯')):
                continue
            got = resolver.get_jpg(name, flr, fud) if resolver else None
            if got and b.get('layers'):
                got = layer_composite(resolver, got, name, b['layers'])
            if got and b.get('zoom'):
                got = zoom_crop(resolver, got, name, b['zoom'])
            if got and 0.45 < got[1] / got[2] < 2.6:
                flush()
                cur_img = got[0]
                sprs = b.get('sprites')
                if sprs and os.environ.get('FSN_NO_SPRITES') != '1':
                    # standing cast: composite over the resolved bg
                    # (content-addressed cache; result copied into the
                    # resolver cache dir so the ship loop picks it up
                    # like any other slide image)
                    comp = sprites.composite(
                        os.path.join(resolver.cachedir, got[0]), sprs)
                    comp_bn = os.path.basename(comp)
                    dest = os.path.join(resolver.cachedir, comp_bn)
                    if not os.path.exists(dest):
                        shutil.copy(comp, dest)
                    cur_img = comp_bn
                if b.get('ct') is not None:
                    # @contrast tone filter active on this slide; applied
                    # to the composited frame because the game's filter
                    # runs range=all (cast included)
                    cur_img = apply_contrast(
                        resolver, (cur_img, got[1], got[2]), b['ct'])[0]
            continue
        if 'day' in b:
            if dojo_open:
                cur_content.append('</aside>')
                dojo_open = False
            flush()
            slides.append((None,
                           f'<p class="day-divider">{b["day"]}</p>'))
            continue
        if 'route' in b:
            flush()
            slides.append((None,
                           f'<p class="route-divider">— Route locked: '
                           f'{esc(b["route"])} —</p>'))
            continue
        if 'ending' in b:
            flush()
            slides.append((None,
                           f'<p class="ending-divider">— ENDING: '
                           f'{esc(b["ending"])} —</p>'))
            continue
        if 'movie' in b:
            flush()
            name = b['movie'].rsplit('.', 1)[0]
            poster = BOOK_POSTERS.get(name)
            if poster:
                # poster art slide with the movie title as its caption
                slides.append((None,
                               f'<div class="tp-poster">'
                               f'<img src="../images/{esc(poster)}.jpg" alt=""/>'
                               f'<p class="movie-divider">— {esc(name)} —</p>'
                               f'</div>', True))
            else:
                slides.append((None,
                               f'<p class="movie-divider">— {esc(name)} —</p>'))
            continue
        if 'dojo-start' in b:
            if not dojo_open:
                if deadend_ok and not deadend_shown[0]:
                    # a bad end precedes every Tiger Dojo skit: show the
                    # game's DEAD END card on its own dedicated slide
                    deadend_shown[0] = True
                    flush()
                    slides.append((None,
                                   '<div class="tp-deadend">'
                                   '<img src="../images/dead_end.jpg" '
                                   'alt="DEAD END"/></div>', True))
                cur_content.append('<aside class="dojo">'
                                   '<p class="dojo-title">Tiger Dojo</p>')
                dojo_open = True
            continue
        if 'p' in b:
            html = []
            for line in b['p']:
                bits, spk = [], None
                for beat in line:
                    if beat['speaker']:
                        spk = beat['speaker']
                    bits.append(beat['text'])  # already escaped
                # translate italic placeholders; 'em_open' is the logical
                # italic state carried across elements — each element
                # reopens/closes its own <em> so XML stays balanced
                out_bits = []
                tag_open = False

                def sync(piece):
                    nonlocal tag_open
                    if em_open and not tag_open:
                        piece.append('<em>')
                        tag_open = True
                    elif not em_open and tag_open:
                        piece.append('</em>')
                        tag_open = False

                for t in bits:
                    parts = re.split(r'([\x01\x02])', t)
                    piece = []
                    sync(piece)
                    for part in parts:
                        if part == '\x01':
                            em_open = True
                            sync(piece)
                        elif part == '\x02':
                            em_open = False
                            sync(piece)
                        else:
                            piece.append(part)
                    out_bits.append(''.join(piece))
                if tag_open and out_bits:
                    out_bits[-1] += '</em>'
                cls = []
                if line[0]['centered']:
                    cls.append('centered')
                if spk:
                    cls.append('dialog')
                c = f' class="{" ".join(cls)}"' if cls else ''
                tag = 'div' if spk else 'p'

                def emit(body_html, with_speaker):
                    h = body_html
                    if with_speaker and spk:
                        h = (f'<span class="speaker">{esc(spk)}</span>'
                             f'{h}')
                    html.append(f'<{tag}{c}>{h}</{tag}>')

                # split oversized line groups at beat boundaries so no
                # single element exceeds the page budget (speaker label
                # stays on the first piece)
                acc, acc_lines, first = [], 0, True
                for t in out_bits:
                    tl = max(1, -(-len(re.sub(r'<[^>]+>', '', t))
                                 // CHARS_PER_LINE))
                    if acc and acc_lines + tl > SLIDE_MAX_LINES:
                        emit('<br/>'.join(acc), first)
                        first = False
                        acc, acc_lines = [], 0
                    acc.append(t)
                    acc_lines += tl
                emit('<br/>'.join(acc), first)
            # each paragraph element is its own item so the slide chunker
            # can split page-sized groups between them
            cur_content.extend(html)
    if dojo_open:
        cur_content.append('</aside>')
    flush()

    out = []
    for s in slides:
        img, content = s[0], s[1]
        # VN overlay style: full-bleed art on the slide div (class-based —
        # cre ignores background-image in inline style attributes) with the
        # translucent text panel rendered INSIDE, on top of the artwork.
        # 'raw' slides (DEAD END card, movie posters) carry their own
        # full-page art div instead of the text panel.
        extra = f' bg-{img_class(img)}' if img else ''
        if len(s) > 2 and s[2]:
            out.append(f'<div class="slide{extra}">{content}</div>')
            continue
        panel = f'<div class="tp">{content}</div>' if content else ''
        out.append(f'<div class="slide{extra}">{panel}</div>')
    return '\n'.join(out)


def img_class(fn):
    """stable CSS class suffix for an image filename"""
    return re.sub(r'\W', '_', fn[:-4])


# text lines per slide: keeps each slide within one reader page so the
# background image is never sliced across pages (Kobo/KOReader landscape)
SLIDE_MAX_LINES = 5
CHARS_PER_LINE = 52      # landscape e-ink at default font (measured)


def est_lines(item_html):
    """estimate rendered text lines of one paragraph/dialog element"""
    brs = item_html.count('<br/>')
    text = re.sub(r'<[^>]+>', '', item_html)
    n = max(1, -(-len(text) // CHARS_PER_LINE))   # ceil
    if '<span class="speaker">' in item_html:
        n += 1
    return int(max(n, brs + 1))


def choice_box(options, title='Choice'):
    """options: list of (label, href)"""
    if not options:
        return ''
    items = '\n'.join(
        f'<li><a href="{href}">{esc(label)}</a></li>'
        for label, href in options)
    return (f'<div class="choice"><p class="choice-hdr">{title}</p>'
            f'<ol>{items}</ol></div>')


def branch_box(options):
    items = '\n'.join(
        f'<li><a href="{href}">{esc(label)}</a></li>'
        for label, href in options)
    return (f'<div class="branch"><p class="choice-hdr">'
            f'The story forks (hidden condition)</p><ul>{items}</ul></div>')


def link_target(chapter_files, ch, node):
    """anchor for scene node of chapter ch"""
    return f'{chapter_files[ch]}#s{node}'


GLOSS = {'桜好感度': 'Sakura affection', '凛好感度': 'Rin affection',
        'セイバー好感度': 'Saber affection'}


def cond_label(c):
    c = c.strip()
    if c in GLOSS:
        return GLOSS[c]
    return c


CSS = '''@charset "utf-8";
/* KOReader/cre engine rules (verified against crengine source):
   - background-image works, but background-size needs BOTH values
     ("contain contain" / "cover cover"); a bare "cover" draws nothing
   - no linear-gradient(); rgba() IS supported but we use solid colors
   - explicit em heights for art strips; no vh units, no floats */
body { font-family: serif; line-height: 1.6; margin: 0; padding: 0 2.5%; color: #f2efe6; background-color: #000000; }
h1.part { font-size: 1.6em; font-weight: normal; text-align: center; letter-spacing: .1em; margin: 2.2em 0 .2em; color: #e8c9a0; }
h2.ch { font-size: 1.25em; font-weight: normal; text-align: center; margin: 1.4em 0 1em; color: #e8c9a0; }
h3.scene { font-size: 1.0em; margin: 1.3em 0 .7em; color: #e8c9a0; letter-spacing: .05em; font-weight: bold; }
div.slide {
  background-color: #000000;
  background-position: center center;
  background-size: cover;             /* spec form (browsers) */
  background-size: cover cover;       /* KOReader/cre: both values required */
  background-repeat: no-repeat;
  min-height: 14em;                   /* keep art presence for short beats */
  padding-top: 7em;                   /* art showcase zone above the panel */
  margin: 0 0 1.1em 0;
  page-break-inside: avoid;           /* never slice a slide across pages */
}
div.tp {
  background-color: rgba(12, 14, 20, 0.78);
  color: #f2efe6;
  padding: 1.4em 1.7em 1.0em;
}
p { margin: 0 0 .8em 0; text-align: justify; }
p.centered { text-align: center; font-style: italic; margin: 1.2em 0; }
div.dialog { margin: 0 0 1em 0; }
span.speaker { display: block; font-size: .8em; letter-spacing: .1em; text-transform: uppercase; color: #e8c9a0; margin-bottom: .1em; }
p.movie-divider { text-align: center; font-variant: small-caps; letter-spacing: .3em; padding: 1em 0; margin: 0 0 1.1em 0; color: #8d8574; background-color: #000000; }
p.day-divider { text-align: center; font-variant: small-caps; letter-spacing: .35em; padding: 1.2em 0; margin: 0 0 1.2em 0; color: #e8c9a0; background-color: #000000; }
p.route-divider { text-align: center; font-variant: small-caps; letter-spacing: .35em; padding: 1.2em 0; margin: 0 0 1.2em 0; color: #e8c9a0; background-color: #000000; }
p.ending-divider { text-align: center; font-variant: small-caps; letter-spacing: .35em; padding: 2.2em 0; margin: 0 0 1.4em 0; color: #f5dfae; background-color: #000000; }
div.tp-deadend { text-align: center; padding: 1em; background-color: #000; }
div.tp-deadend img { width: 100%; height: auto; }
div.tp-poster { text-align: center; padding: 1em; background-color: #000; }
div.tp-poster img { width: 100%; height: auto; }
div.choice { border: 1.5px solid #b08a4f; padding: .8em 1.1em .6em; margin: 1.5em 0; background-color: #17140f; page-break-inside: avoid; }
div.choice ol, div.branch ul { margin: .2em 0 .3em 1.2em; padding: 0; }
div.choice li, div.branch li { margin: .5em 0; }
p.choice-hdr { font-size: .8em; letter-spacing: .25em; text-transform: uppercase; color: #e8c9a0; margin: 0 0 .3em; text-align: left; }
div.choice a { color: #f5dfae; text-decoration: none; font-weight: bold; }
div.branch { border: 1px dashed #9a8a6a; padding: .8em 1.1em .6em; margin: 1.5em 0; background-color: #17140f; page-break-inside: avoid; }
div.branch a { color: #f5dfae; }
aside.dojo { border-left: 4px solid #b5722c; background-color: #1e160a; padding: .7em 1.1em; margin: 1.4em 0; }
p.dojo-title { font-size: .8em; letter-spacing: .25em; text-transform: uppercase; color: #e8a95c; margin: 0 0 .5em; }
p.continue { text-align: center; margin: 1.8em 0 1.2em; }
p.continue a { color: #f5dfae; text-decoration: none; }
hr.scene-rule { border: 0; border-top: 1px solid #2a2a2a; margin: 1.6em 22%; }
nav ol { list-style: none; margin-left: .8em; }
nav ol ol { margin-left: 1.4em; }
nav a { color: #f5dfae; text-decoration: none; }
section.titlepage h1 { font-weight: normal; letter-spacing: .08em; text-align: center; margin-top: 2.4em; }
p.subtitle { color: #cbbfa8; text-align: center; margin-top: 0; }
p.sample-note { text-align: center; margin-top: 2.6em; font-variant: small-caps; letter-spacing: .2em; color: #e8c9a0; }
p.credits { text-align: center; margin-top: 3.4em; font-size: .85em; color: #8d8574; }
'''



def xhtml(title, body, extra_class='', css='css/style.css'):
    c = f' class="{extra_class}"' if extra_class else ''
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head><title>{esc(title)}</title>
<link rel="stylesheet" type="text/css" href="{css}"/></head>
<body{c}>
{body}
</body></html>
'''


TITLE_PAGE = xhtml('Fate/stay night', '''
<section epub:type="titlepage" class="titlepage">
<h1>Fate/stay night</h1>
<p class="subtitle">Remaster — the complete scenario, branch-aware edition</p>
<p class="sample-note">Prologue · Fate · Unlimited Blade Works · Heaven\u2019s Feel · Last Episode</p>
<p class="credits">Story: Kinoko Nasu / TYPE-MOON<br/>English localization: Aniplex</p>
</section>''')

GUIDE = xhtml('Reading guide', '''
<section class="guide">
<h1 class="part">Reading guide</h1>
<p>This edition reproduces the game\u2019s complete scenario with its
branches, split into three route volumes (the Prologue opens each one).
Within a volume the parts follow the canonical play order:</p>
<ol>
<li><b>Prologue</b> — linear; introduces Rin and the war.</li>
<li><b>Fate</b> (Saber route) — intended first route.</li>
<li><b>Unlimited Blade Works</b> (Rin route) — branches from the common days.</li>
<li><b>Heaven\u2019s Feel</b> (Sakura route) — the darkest route.</li>
<li><b>Last Episode</b> — the post-game coda, unlocked after the other endings.</li>
</ol>
<p><b>Choices.</b> When the game asks the player to decide, this book shows
a bordered <i>Choice</i> box. Each option links to the scene it leads to —
follow a link to take that path, or keep reading linearly to follow the
links in order.</p>
<p><b>Hidden forks.</b> Some branches depend on invisible affection or
progress flags rather than a player choice. These appear as dashed
<i>fork</i> boxes naming the condition.</p>
<p><b>Bad ends.</b> Wrong turns can end the story; the game then plays a
comedy skit. Those skits are included where they occur, marked
<i>Tiger Dojo</i>.</p>
<p><b>Artwork.</b> Scene illustrations from the game are placed where they
appear, in color.</p>
</section>''')


VOLUMES = [
    # (display title, parts, out filename) — the sprite-composited frames
    # make a single combined edition ~0.5 GB, so the book ships as three
    # route volumes; the Prologue is duplicated as each volume's opener
    ('I — Fate', ['Prologue', 'Fate'], 'FateStayNight_Vol1_Fate.epub'),
    ('II — Unlimited Blade Works', ['Prologue', 'Unlimited Blade Works'],
     'FateStayNight_Vol2_UBW.epub'),
    ('III — Heaven\u2019s Feel',
     ['Heaven\u2019s Feel', 'Last Episode', 'Extras'],
     'FateStayNight_Vol3_HF.epub'),
]


def link_or_copy(src, dst):
    """Hardlink when src and dst share a volume, else copy. The staged
    work tree must contain the images (validate_book.py checks it in
    place), but the bytes are final and never modified there — linking
    skips re-writing ~700 MB per build."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy(src, dst)


def build_book(limit_parts=None, volume_title=None, out_name=None,
               work_dir=None, _limit_chapters=0):
    flow = load_flowtext()
    print(f'flowtext slots: {len(flow)}')
    groups, parts, titles = build_chapter_index()
    for p, chs in parts.items():
        print(f'part {p}: {len(chs)} chapters')
    if limit_parts:
        parts = collections.OrderedDict(
            (k, v) for k, v in parts.items() if k in limit_parts)
    if _limit_chapters:
        parts = collections.OrderedDict(
            (k, v[:_limit_chapters]) for k, v in parts.items())
    work = work_dir or WORK
    out_path = os.path.join(OUT_DIR, out_name) if out_name else \
        os.path.join(OUT_DIR, 'FateStayNight_branch_aware.epub')
    vol_short = volume_title or 'branch-aware edition'
    title_page = xhtml('Fate/stay night', f'''
<section epub:type="titlepage" class="titlepage">
<h1>Fate/stay night</h1>
<p class="subtitle">Remaster — {esc(vol_short)}</p>
<p class="sample-note">{' · '.join(esc(p) for p in parts)}</p>
<p class="credits">Story: Kinoko Nasu / TYPE-MOON<br/>English localization: Aniplex</p>
</section>''')

    resolver = ImageResolver()
    images = set()

    # movie posters shipped with the source tree (img/poster_opNN.jpg);
    # keyed by movie base name for render_blocks
    global BOOK_POSTERS
    POSTERS = {n: f'poster_op{n}' for n in ('01', '02', '03')
               if os.path.exists(os.path.join(HERE, 'img',
                                              f'poster_op{n}.jpg'))}
    BOOK_POSTERS = {f'op{n}': p for n, p in POSTERS.items()}

    # chapter file names
    chapter_files = {}
    for pi, (part, chs) in enumerate(parts.items()):
        for ci, ch in enumerate(chs):
            chapter_files[ch] = f'text/p{pi}_c{ci:02d}.xhtml'

    # global chapter sequence for continue links
    seq = [ch for chs in parts.values() for ch in chs]

    manifest, spine, toc = [], [], []
    manifest.append('  <item id="css" href="css/style.css" media-type="text/css"/>')
    oebps = os.path.join(work, 'OEBPS')
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(oebps, 'css'))
    os.makedirs(os.path.join(oebps, 'images'))
    os.makedirs(os.path.join(oebps, 'text'))
    # EPUB cover (optional): copy + manifest item; absent cover is fine
    cover_src = os.path.join(HERE, 'img', 'cover.jpg')
    has_cover = os.path.exists(cover_src)
    if has_cover:
        link_or_copy(cover_src, os.path.join(oebps, 'cover.jpg'))
        manifest.append('  <item id="cover-image" href="cover.jpg" '
                        'media-type="image/jpeg" properties="cover-image"/>')
    with open(os.path.join(oebps, 'css', 'style.css'), 'w', encoding='utf-8') as f:
        f.write(CSS)
    with open(os.path.join(oebps, 'title.xhtml'), 'w', encoding='utf-8') as f:
        f.write(title_page)
    with open(os.path.join(oebps, 'guide.xhtml'), 'w', encoding='utf-8') as f:
        f.write(GUIDE)
    manifest.append('  <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>')
    manifest.append('  <item id="guide" href="guide.xhtml" media-type="application/xhtml+xml"/>')
    spine += ['  <itemref idref="title"/>', '  <itemref idref="guide"/>']
    toc.append('<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>')
    toc.append('<li><a href="title.xhtml">Title</a></li>')
    toc.append('<li><a href="guide.xhtml">Reading guide</a></li>')

    total_scenes = 0
    for pi, (part, chs) in enumerate(parts.items()):
        toc.append(f'<li><span>{esc(part)}</span><ol>')
        # part divider page
        pf = f'text/part{pi}.xhtml'
        with open(os.path.join(oebps, pf), 'w', encoding='utf-8') as f:
            f.write(xhtml(part, f'<h1 class="part">{esc(part)}</h1>'))
        manifest.append(f'  <item id="pt{pi}" href="{pf}" media-type="application/xhtml+xml"/>')
        spine.append(f'  <itemref idref="pt{pi}"/>')
        toc.append(f'<li><a href="{pf}">{esc(part)}</a></li>')

        for ci, ch in enumerate(chs):
            fn = chapter_files[ch]
            nodes, routes = parse_fcf(os.path.join(KAG, ch + '.fcf'))
            out_edges = collections.defaultdict(list)   # node -> [targets in route order]
            for a, b in routes:
                out_edges[a].append(b)
            scene_files = groups[ch]

            def resolve_node(n, depth=0):
                """virtual nodes (no script) link forward to the nearest
                script-bearing node along the graph"""
                if n in scene_files or depth > 32:
                    return n
                for t in out_edges.get(n, []):
                    r = resolve_node(t, depth + 1)
                    if r is not None:
                        return r
                return None

            def tgt_href(n):
                r = resolve_node(n)
                return f'{fn}#s{r}' if r is not None else fn

            body = [f'<h2 class="ch">{esc(titles.get(ch, ch))}</h2>']
            # ending chapters ARE the endings (the game unlocks the
            # achievements programmatically, not via @unlockachievement)
            end_label = ENDING_CHAPTERS.get(ch)
            if end_label:
                body.append(f'<div class="slide"><div class="tp">'
                            f'<p class="ending-divider">— ENDING: '
                            f'{esc(end_label)} —</p></div></div>')
            # chapter opener script (-00) if the graph has no SCENE 0
            for nid in sorted(set(scene_files) | set(n for n, d in nodes.items() if d['type'] == 'SCENE')):
                nd = nodes.get(nid)
                fname = scene_files.get(nid)
                if not fname:
                    continue   # virtual node; links resolve through it
                total_scenes += 1
                title_txt = None
                if nd and nd['flows']:
                    title_txt = flow.get('flowtext_' + nd['flows'][0])
                if nid == 0 and title_txt in ('Day 1',):
                    title_txt = None
                h = (f'<h3 class="scene">{esc(title_txt)}</h3>'
                     if title_txt else '')
                body.append(f'<section id="s{nid}" epub:type="scene">{h}')
                text = open(os.path.join(KAG, fname), encoding='utf-8-sig').read()
                blocks = parse_scene(text, script_slots(fname))
                body.append(render_blocks(blocks, resolver, images))
                # choice / branch boxes from the graph
                targets = out_edges.get(nid, [])
                # if this node is itself a SELECTER with a script, its own
                # outgoing edges are the options
                sels = ([nid] if nd and nd['type'] == 'SELECTER'
                        else [t for t in targets
                              if nodes.get(t, {}).get('type') == 'SELECTER'])
                direct = [] if (nd and nd['type'] == 'SELECTER') else \
                    [t for t in targets
                     if nodes.get(t, {}).get('type') != 'SELECTER']
                if len(direct) > 1:
                    conds = [cond_label(c) for c in (nd['conds'] if nd else [])]
                    opts = []
                    for t in direct:
                        lab = 'Continue'
                        if nodes.get(t, {}).get('flows'):
                            lt = flow.get('flowtext_' + nodes[t]['flows'][0])
                            if lt:
                                lab = lt
                        if conds:
                            lab = f'{lab}'
                        opts.append((lab + (f'  ({conds[k]})' if k < len(conds) and conds[k] else ''),
                                     tgt_href(t)))
                    body.append(branch_box(opts))
                for st in sels:
                    snd = nodes[st]
                    tg = out_edges.get(st, [])
                    opts = []
                    for k, t in enumerate(tg):
                        label = ''
                        if k + 1 < len(snd['flows']):
                            label = flow.get('flowtext_' + snd['flows'][k + 1], '')
                        opts.append((label or f'Option {k+1}',
                                     tgt_href(t)))
                    body.append(choice_box(opts))
                body.append('</section>')
                body.append('<hr class="scene-rule"/>')
            # chapter continue link
            idx = seq.index(ch)
            if idx + 1 < len(seq):
                nxt = seq[idx + 1]
                body.append(f'<p class="continue">'
                            f'<a href="{chapter_files[nxt]}">Continue — '
                            f'{esc(titles.get(nxt, nxt))} \u2192</a></p>')
            elif volume_title:
                body.append('<p class="continue">'
                            '— end of this volume —</p>')
            with open(os.path.join(oebps, fn), 'w', encoding='utf-8') as f:
                f.write(xhtml(titles.get(ch, ch), '\n'.join(body),
                              css='../css/style.css'))
            iid = f'c{pi}_{ci}'
            manifest.append(f'  <item id="{iid}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine.append(f'  <itemref idref="{iid}"/>')
            toc.append(f'<li><a href="{fn}">{esc(titles.get(ch, ch))}</a></li>')
            print(f'  {part} / {ch} -> {fn}')
        toc.append('</ol></li>')
    toc.append('</ol></nav>')
    with open(os.path.join(oebps, 'nav.xhtml'), 'w', encoding='utf-8') as f:
        f.write(xhtml('Contents', '\n'.join(toc)))
    manifest.append('  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

    # images + generated per-image background rules (cre needs class-based
    # background-image; inline style attributes are not parsed)
    total_img = 0
    bg_rules = []
    for fn in sorted(images):
        src = os.path.join(resolver.cachedir, fn)
        if not os.path.exists(src):
            continue
        link_or_copy(src, os.path.join(oebps, 'images', fn))
        total_img += os.path.getsize(src)
        iid = 'i' + re.sub(r'\W', '_', fn[:-4])
        mt = 'image/webp' if fn.endswith('.webp') else 'image/jpeg'
        manifest.append(f'  <item id="{iid}" href="images/{fn}" media-type="{mt}"/>')
        bg_rules.append(f'.bg-{img_class(fn)} {{ background-image: url(\'../images/{fn}\'); }}')
    with open(os.path.join(oebps, 'css', 'style.css'), 'a', encoding='utf-8') as f:
        f.write('\n/* generated slide art rules */\n' + '\n'.join(bg_rules) + '\n')
    # fixed presentation art (DEAD END card, movie posters): copied whenever
    # present and manifested, so inline ../images/*.jpg links always validate
    for art in ['dead_end.jpg'] + [f'{p}.jpg' for p in POSTERS.values()]:
        src = os.path.join(HERE, 'img', art)
        if os.path.exists(src):
            link_or_copy(src, os.path.join(oebps, 'images', art))
            iid = 'i' + re.sub(r'\W', '_', art[:-4])
            manifest.append(f'  <item id="{iid}" href="images/{art}" '
                            f'media-type="image/jpeg"/>')
    print(f'images: {len(images)} ({total_img/1e6:.1f} MB)  scenes: {total_scenes}')

    # EPUB2-style cover pointer (readers that ignore properties="cover-image")
    cover_meta = ('\n<meta name="cover" content="cover-image"/>'
                  if has_cover else '')
    vol_slug = re.sub(r'\W', '', volume_title or 'branch-aware').lower()
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">urn:uuid:fsn2-remaster-{vol_slug}</dc:identifier>
<dc:title>Fate/stay night — {esc(vol_short)}</dc:title>
<dc:language>en</dc:language>
<dc:creator>Kinoko Nasu / TYPE-MOON</dc:creator>
<meta property="dcterms:modified">2026-10-03T00:00:00Z</meta>{cover_meta}
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine>
{chr(10).join(spine)}
</spine>
</package>
'''
    with open(os.path.join(oebps, 'content.opf'), 'w', encoding='utf-8') as f:
        f.write(opf)
    os.makedirs(os.path.join(work, 'META-INF'), exist_ok=True)
    with open(os.path.join(work, 'META-INF', 'container.xml'), 'w',
              encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>')
    out = out_path
    os.makedirs(OUT_DIR, exist_ok=True)
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip',
                   compress_type=zipfile.ZIP_STORED)
        for root, _, files in os.walk(work):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, work).replace('\\', '/')
                # media is already compressed (jpeg/webp): store, don't deflate
                ct = (zipfile.ZIP_STORED
                      if fn.endswith(('.jpg', '.webp', '.png'))
                      else zipfile.ZIP_DEFLATED)
                z.write(full, arc, compress_type=ct)
    print('wrote', out, f'({os.path.getsize(out)/1e6:.1f} MB)')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--volume', type=int, default=0,
                    help='1/2/3 to build one volume; 0 builds all three')
    ap.add_argument('--chapters', type=int, default=0,
                    help='debug: keep only the first N chapters per part')
    ap.add_argument('--out', default=None, help='debug: output filename')
    args = ap.parse_args()
    for i, (vtitle, vparts, vout) in enumerate(VOLUMES, 1):
        if args.volume and args.volume != i:
            continue
        print(f'=== Volume {vtitle} ===')
        kwargs = dict(limit_parts=vparts, volume_title=vtitle,
                      out_name=args.out or vout,
                      work_dir=os.path.join(HERE, f'book_work_v{i}'))
        if args.chapters:
            kwargs['limit_parts'] = None
            kwargs['_limit_chapters'] = args.chapters
        build_book(**kwargs)
