"""Convert Fate/stay night Remaster scenario data to EPUB.

Pipeline:
  1. FPD packs (obb/*.bin, XOR keystream + zlib) -> kag scripts + epk files
     (fpd.py; keystream from FSNr_tools decryptKey.bin)
  2. EPK archives (filename-keyed Feistel cipher) -> text databases
     (tools_fsnr/build/main.exe, validated against reference pairs)
  3. KAG scripts (.ks) reference text via $$$message_B_P_L$$$ slots; @say
     voices give the speaker of the following slot; @pg/@r/@pgnl structure
     the flow; *pageN| labels are the game's page units.
  4. Render pages as XHTML -> EPUB 3.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fpd import FPD, load_key  # noqa: E402
from config import BLADE, MAIN_EXE, KEY_BIN  # noqa: E402

WORK = os.path.join(HERE, 'epub_work')

# Voice codes -> display names (official English spellings)
SPEAKERS = {
    'shi': 'Shirou Emiya', 'rin': 'Rin Tohsaka', 'sav': 'Saber',
    'sak': 'Sakura Matou', 'arc': 'Archer', 'kot': 'Kotomine Kirei',
    'gil': 'Gilgamesh', 'cas': 'Caster', 'zok': 'Zouken Matou',
    'sin': 'Shinji Matou', 'tig': 'Taiga Fujimura', 'ise': 'Issei Ryuudou',
    'kuz': 'Kuzuki Souichirou', 'mit': 'Ayako Mitsuzuri', 'bas': 'Berserker',
    'ran': 'Lancer', 'rad': 'Rider', 'has': 'True Assassin',
    'koj': 'Assassin', 'iri': 'Illyasviel', 'sao': 'Saegusa Yukika',
    'mak': 'Makidera Kaede', 'sae': 'Himuro Kane', 'him': 'Himuro Kane',
    'tig2': 'Taiga Fujimura', 'dir': '', 'tok': 'Tokiomi Tohsaka',
    'krn': 'Rin Tohsaka', 'kir': 'Emiya Kiritsugu', 'ser': 'Sella',
    'riz': 'Leysritt', 'dtg': 'Taiga Fujimura', 'ved': 'Bedivere',
}

TAG_RE = re.compile(r'\[(?:lr|line\d+|r|pg|l)\]')

# ---------------------------------------------------------------- images
FILEINFOS = ['fileinfo_ex_pack.txt', 'fileinfo_saber.txt', 'fileinfo_rin.txt',
             'fileinfo_sakura.txt', 'fileinfo_patch.txt',
             'fileinfo_fileinfo_ex_pack.txt', 'fileinfo_fileinfo_saber.txt',
             'fileinfo_fileinfo_rin.txt', 'fileinfo_fileinfo_sakura.txt',
             'fileinfo_fileinfo_patch.txt']
DAT_PACK = {'ex_pack.dat': ('pack10d', 'pack/ex_pack.dat'),
            'saber.dat': ('pack11d', 'pack/saber.dat'),
            'rin.dat': ('pack12d', 'pack/rin.dat'),
            'sakura.dat': ('pack12d', 'pack/sakura.dat'),
            'patch.dat': ('patch01d', 'pack/patch.dat')}


class ImageResolver:
    """Resolves .ks storage names (04突き etc.) to jpeg files via the
    fileinfo_* manifests + pack/*.dat blobs."""

    def __init__(self, cachedir='img'):
        self.cachedir = cachedir
        os.makedirs(cachedir, exist_ok=True)
        self.entries = {}          # name -> (dat, offset, size)
        for fi in FILEINFOS:
            path = os.path.join(HERE, fi)
            if not os.path.exists(path):
                continue
            for line in open(path, encoding='utf-8'):
                parts = line.split('::')
                if len(parts) >= 5 and parts[1] in ('webp', 'png', 'jpg'):
                    self.entries[parts[0]] = (parts[2], int(parts[3]),
                                              int(parts[4]))
        # the game resolves storage names case-insensitively (script 'B16'
        # vs manifest 'b16'); 462 of 1350 referenced names differ in case
        self.entries_lower = {k.lower(): v for k, v in self.entries.items()}
        self.used = {}             # logical name -> embedded filename

    def _lookup(self, name):
        if name in self.entries:
            return self.entries[name]
        return self.entries_lower.get(name.lower())

    def _blob(self, datname):
        local = os.path.join(self.cachedir, datname)
        if not os.path.exists(local):
            pack, entry = DAT_PACK[datname]
            load_key(KEY_BIN)
            p = FPD(os.path.join(BLADE, f'{pack}.bin'), None)
            for i, (name, off, ln, fl) in enumerate(p.entries):
                if name == entry:
                    with open(local, 'wb') as f:
                        f.write(p.read_entry(i)[1])
                    break
            else:
                raise SystemExit(f'dat blob not found: {entry}')
        return local

    def get_jpg(self, name, fliplr=False, flipud=False):
        """Returns (images-dir filename, w, h) or None if unresolvable."""
        key = (name, fliplr, flipud)
        if key in self.used:
            return self.used[key]
        ent = self._lookup(name)
        if ent is None:
            return None
        datname, off, size = ent
        with open(self._blob(datname), 'rb') as f:
            f.seek(off)
            raw = f.read(size)
        from PIL import Image, ImageOps
        import io
        im = Image.open(io.BytesIO(raw)).convert('RGB')
        if fliplr:
            im = ImageOps.mirror(im)
        if flipud:
            im = ImageOps.flip(im)
        if im.width > 1600:
            im = im.resize((1600, round(im.height * 1600 / im.width)),
                           Image.LANCZOS)
        # unique filename: Japanese-only names collide when slugified, so
        # append a short hash of the original storage name
        fn = ('bg_' + re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_') +
              '_' + hashlib.md5(name.encode('utf-8')).hexdigest()[:6] +
              ('_f' if (fliplr or flipud) else '') + '.jpg')
        out = os.path.join(self.cachedir, fn)
        im.save(out, quality=86)
        self.used[key] = (fn, im.width, im.height)
        return self.used[key]


def smart_quotes(s):
    """Convert straight double quotes to typographic pairs."""
    out = []
    open_q = True
    for ch in s:
        if ch == '"':
            out.append('\u201c' if open_q else '\u201d')
            open_q = not open_q
        else:
            out.append(ch)
    return ''.join(out)


def parse_dat(path):
    slots = {}
    pat = re.compile(r'^\d+::\$\$\$(message_\d+_\d+_\d+)\$\$\$::(.*)::\s*$')
    for line in open(path, encoding='utf-8'):
        m = pat.match(line.rstrip('\n'))
        if m:
            slots[m.group(1)] = m.group(2)
    return slots


def extract_chapter(chapter_jp, locale, pages=None):
    """Returns list of pages; each page is a list of beat dicts."""
    load_key(KEY_BIN)
    packs = {pk: FPD(os.path.join(BLADE, f'{pk}.bin'), None)
             for pk in ('pack00m', 'patch00m')}
    # .ks (patch preferred)
    ks_data = None
    for pk in ('pack00m', 'patch00m'):
        for i, (name, off, ln, fl) in enumerate(packs[pk].entries):
            if name == f'root/data/kag/{chapter_jp}.ks':
                ks_data = packs[pk].read_entry(i)[1]
                if pk == 'patch00m':
                    break
        if ks_data and pk == 'patch00m':
            break
    if ks_data is None:
        raise SystemExit(f'chapter script not found: {chapter_jp}')

    # text epk
    import hashlib, string
    alphabet = string.digits + string.ascii_lowercase
    bits = int.from_bytes(hashlib.md5(chapter_jp.encode('utf-8')).digest(), 'big')
    h = ''.join(alphabet[(bits << i >> 128) & 0x1F] for i in range(3, 131, 5))
    sub = 'us' if locale == 'us' else ('ck' if locale == 'ck' else None)
    epk_name = (f'root/data/locale/{sub}/epk/{h}.epk' if sub
                else f'root/data/epk/{h}.epk')
    epk_data = None
    for pk in ('pack00m', 'patch00m'):
        for i, (name, off, ln, fl) in enumerate(packs[pk].entries):
            if name == epk_name:
                epk_data = packs[pk].read_entry(i)[1]
    if epk_data is None:
        raise SystemExit(f'epk not found: {epk_name}')

    os.makedirs(f'{WORK}/epk', exist_ok=True)
    epk_path = f'{WORK}/epk/{h}.epk'
    with open(epk_path, 'wb') as f:
        f.write(epk_data)
    subprocess.run([MAIN_EXE, 'dec', epk_path], check=True,
                   stdout=subprocess.DEVNULL)
    slots = parse_dat(epk_path + '_dec')

    # ---- parse the .ks -------------------------------------------------
    text = ks_data.decode('utf-8-sig')
    pages_out = []
    cur_page = {'bg': None, 'lines': []}
    cur_line = []          # beats joined by <br/> (one @r group)
    speaker = None         # from @say, applies to next slot
    last_dialog_speaker = None
    slot_re = re.compile(r'^\$\$\$(message_\d+_\d+_\d+)\$\$\$$')
    say_re = re.compile(r'^@say storage=\w+_(\w+)_\d+')
    label_re = re.compile(r'^\*page(\d+)\|')
    bg_re = re.compile(r'^@(?:fadein|bg)\s+file=(\S+)')
    bg2_re = re.compile(r'^@image(?:ex)?\s+storage=(\S+)')
    bg_state = {}
    kw_re = re.compile(r'(\w+)=true')

    def flush_line():
        nonlocal cur_line
        if cur_line:
            cur_page['lines'].append(cur_line)
            cur_line = []

    def flush_page():
        nonlocal cur_page
        flush_line()
        if cur_page['lines']:
            pages_out.append(cur_page)
        cur_page = {'bg': None, 'lines': []}

    def set_bg(name, params):
        # last background directive before the page's first text wins
        bg_state.clear()
        bg_state.update({'name': name,
                         'fliplr': 'fliplr=true' in params,
                         'flipud': 'flipud=true' in params})

    for raw in text.split('\n'):
        ln = raw.strip()
        if not ln or ln.startswith(';'):
            continue
        m = label_re.match(ln)
        if m or (ln.startswith('*') and not slot_re.match(ln)):
            flush_page()
            continue
        m = say_re.match(ln)
        if m:
            speaker = m.group(1)
            continue
        if ln in ('@pg', '@pgnl'):
            flush_line()
            continue
        if ln == '@r':
            flush_line()
            continue
        if ln == '@blackout':
            set_bg('black', '')
            flush_line()
            continue
        if ln.startswith(('@fadein', '@bg ')):
            m = bg_re.match(ln)
            if m:
                set_bg(m.group(1), ln)
            continue
        if ln.startswith(('@image ', '@imageex ')):
            m = bg2_re.match(ln)
            if m:
                set_bg(m.group(1), ln)
            continue
        if ln.startswith('@date_title'):
            flush_page()
            m2 = re.search(r'date=(\d)', ln)
            cur_page['lines'].append([{'type': 'day', 'day': m2.group(1) if m2 else '?'}])
            continue
        if ln.startswith('@'):
            continue
        m = slot_re.match(ln)
        if m:
            key = m.group(1)
            t = slots.get(key)
            if t is None:
                continue
            stripped = t.strip()
            centered = (len(t) - len(t.lstrip(' '))) >= 6
            is_dialog = bool(stripped) and stripped[0] in '"\u201c「'
            spk = None
            if speaker:
                spk = SPEAKERS.get(speaker, speaker.capitalize())
                last_dialog_speaker = spk
            elif is_dialog and last_dialog_speaker and cur_line:
                # continuation of the current quoted run
                spk = last_dialog_speaker
            clean = TAG_RE.sub('', stripped)
            clean = smart_quotes(clean)
            if cur_page['bg'] is None and bg_state:
                cur_page['bg'] = dict(bg_state)
            cur_line.append({'type': 'text', 'text': clean,
                             'speaker': spk if is_dialog else None,
                             'centered': centered})
            speaker = None
            continue
    flush_page()
    if pages:
        pages_out = pages_out[:pages]
    return pages_out, len(slots)


def render_page(page, idx, title, bg_img=None):
    parts = []
    for line in page['lines']:
        if line[0].get('type') == 'day':
            parts.append(f'<p class="day-divider">Day {line[0]["day"]}</p>')
            continue
        # a "line" is a list of text beats
        html_bits = []
        spk = None
        for beat in line:
            if beat.get('speaker'):
                spk = beat['speaker']
            html_bits.append(beat['text'])
        body = '<br/>\n'.join(html_bits)
        cls = []
        if line[0].get('centered'):
            cls.append('centered')
        if spk:
            cls.append('dialog')
            speaker_html = f'<span class="speaker">{spk}</span>\n'
            body = speaker_html + body
        c = f' class="{" ".join(cls)}"' if cls else ''
        if spk:
            parts.append(f'<div{c}>{body}</div>')
        else:
            parts.append(f'<p{c}>{body}</p>')
    body_html = '\n'.join(parts)
    style = ''
    body_cls = ''
    if bg_img:
        style = (" style=\"background:#000 url('images/" + bg_img +
                 "') center center / cover no-repeat;\"")
        body_cls = ' class="with-bg"'
    elif bg_img == '':
        body_cls = ' class="with-bg"'
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <title>{title} — page {idx}</title>
  <link rel="stylesheet" type="text/css" href="css/style.css"/>
</head>
<body{body_cls}>
  <span epub:type="pagebreak" id="p{idx}" role="doc-pagebreak" aria-label="page {idx}"/>
  <section class="game-page"{style} epub:type="chapter">
    <div class="text-panel">
{body_html}
    </div>
  </section>
</body>
</html>
'''


CSS = '''/* Fate/stay night — EPUB sample */
@charset "utf-8";
body {
  font-family: "Georgia", "Times New Roman", serif;
  line-height: 1.65;
  margin: 0 5%;
  color: #1a1a1a;
  background: #fdfcf8;
}
body.with-bg {
  margin: 0;
  background: #000;
  color: #f2efe6;
}
section.game-page {
  padding: 0;
  min-height: 100vh;
  text-align: justify;
  hyphens: auto;
}
body.with-bg section.game-page {
  display: block;
}
div.text-panel {
  padding: 1.4em 1.6em 1.8em 1.6em;
  margin-top: 30vh;
  background: linear-gradient(to bottom,
    rgba(8, 9, 16, 0) 0%,
    rgba(8, 9, 16, 0.62) 14%,
    rgba(8, 9, 16, 0.78) 26%,
    rgba(8, 9, 16, 0.78) 96%,
    rgba(8, 9, 16, 0.62) 100%);
}
div.text-panel p {
  margin: 0 0 0.9em 0;
}
p {
  margin: 0 0 0.9em 0;
}
p.centered, div.centered {
  text-align: center;
  text-indent: 0;
  margin: 1.4em 0;
  font-style: italic;
}
div.dialog {
  margin: 0 0 1.05em 0;
  text-indent: 0;
  text-align: left;
}
span.speaker {
  display: block;
  font-size: 0.82em;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #7a1f1f;
  margin-bottom: 0.15em;
}
p.day-divider {
  text-align: center;
  text-indent: 0;
  font-variant: small-caps;
  letter-spacing: 0.25em;
  margin: 2em 0;
  color: #7a1f1f;
}
body.with-bg span.speaker, body.with-bg p.day-divider {
  color: #e8c9a0;
}
/* thin rule between game pages (plain pages only) */
section.game-page + section.game-page {
  border-top: 1px solid #d8d2c4;
}
body.with-bg section.game-page + section.game-page {
  border-top: none;
}
'''

TITLE_PAGE = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <title>Fate/stay night</title>
  <link rel="stylesheet" type="text/css" href="css/style.css"/>
</head>
<body>
  <section class="titlepage" epub:type="titlepage">
    <h1>Fate/stay night</h1>
    <p class="subtitle">Remaster — scenario text conversion</p>
    <p class="sample-note">Sample · Prologue, Day 1<br/>First two pages for review</p>
    <p class="credits">Original story: Kinoko Nasu / TYPE-MOON<br/>
    English localization: Aniplex / TYPE-MOON</p>
  </section>
</body>
</html>
'''

TITLE_CSS = '''
section.titlepage {
  text-align: center;
  margin-top: 24%;
  text-indent: 0;
}
section.titlepage h1 {
  font-weight: normal;
  letter-spacing: 0.08em;
  margin-bottom: 0.2em;
}
p.subtitle { color: #666; margin-top: 0; }
p.sample-note {
  margin-top: 3em;
  font-variant: small-caps;
  letter-spacing: 0.2em;
  color: #7a1f1f;
}
p.credits { margin-top: 4em; font-size: 0.85em; color: #888; }
'''


def build_epub(pages, out_path, title='Fate/stay night — Prologue I (sample)',
               resolver=None):
    oebps = f'{WORK}/OEBPS'
    if os.path.exists(f'{WORK}/OEBPS'):
        shutil.rmtree(f'{WORK}/OEBPS')
    os.makedirs(f'{oebps}/css', exist_ok=True)
    os.makedirs(f'{oebps}/images', exist_ok=True)
    with open(f'{oebps}/css/style.css', 'w', encoding='utf-8') as f:
        f.write(CSS + TITLE_CSS)
    with open(f'{oebps}/title.xhtml', 'w', encoding='utf-8') as f:
        f.write(TITLE_PAGE)

    manifest = ['  <item id="css" href="css/style.css" media-type="text/css"/>',
                '  <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['  <itemref idref="title"/>']
    toc = ['<nav epub:type="toc" id="toc">',
           '  <h1>Contents</h1>', '  <ol>', '    <li><a href="title.xhtml">Title</a></li>']
    used_images = {}
    for i, page in enumerate(pages, 1):
        fn = f'page{i:03d}.xhtml'
        bg_img = None
        bg = page.get('bg')
        if bg and resolver:
            if bg['name'] == 'black':
                bg_img = ''            # plain black section style
            else:
                got = resolver.get_jpg(bg['name'], bg.get('fliplr', False),
                                       bg.get('flipud', False))
                if got:
                    bg_img = got[0]
                    used_images[got[0]] = True
        with open(f'{oebps}/{fn}', 'w', encoding='utf-8') as f:
            f.write(render_page(page, i, title,
                                bg_img=bg_img or None))
        manifest.append(f'  <item id="p{i}" href="{fn}" media-type="application/xhtml+xml"/>')
        spine.append(f'  <itemref idref="p{i}"/>')
        first = next((b['text'] for line in page['lines'] for b in line
                      if b.get('type') == 'text'), f'Page {i}')
        toc.append(f'    <li><a href="{fn}">Page {i} — {first[:40]}…</a></li>')
    for fn in used_images:
        shutil.copy(os.path.join(resolver.cachedir, fn),
                    f'{oebps}/images/{fn}')
        iid = 'img_' + fn[:-4].replace('-', '_')
        manifest.append(f'  <item id="{iid}" href="images/{fn}" media-type="image/jpeg"/>')
    toc += ['  </ol>', '</nav>']
    with open(f'{oebps}/nav.xhtml', 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">\n'
                '<head><title>Contents</title>'
                '<link rel="stylesheet" type="text/css" href="css/style.css"/>'
                '</head>\n<body>\n' + '\n'.join(toc) + '\n</body>\n</html>\n')
    manifest.append('  <item id="nav" href="nav.xhtml" '
                    'media-type="application/xhtml+xml" properties="nav"/>')

    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:7f2b9a1e-fsn2-epub-sample</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>Kinoko Nasu / TYPE-MOON</dc:creator>
    <meta property="dcterms:modified">2026-10-03T00:00:00Z</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest)}
  </manifest>
  <spine>
{chr(10).join(spine)}
  </spine>
</package>
'''
    with open(f'{oebps}/content.opf', 'w', encoding='utf-8') as f:
        f.write(opf)
    os.makedirs(f'{WORK}/META-INF', exist_ok=True)
    with open(f'{WORK}/META-INF/container.xml', 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                '  <rootfiles>\n    <rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/>\n  </rootfiles>\n'
                '</container>\n')

    with zipfile.ZipFile(out_path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip',
                   compress_type=zipfile.ZIP_STORED)
        z.write(f'{WORK}/META-INF/container.xml', 'META-INF/container.xml',
                compress_type=zipfile.ZIP_DEFLATED)
        for root, _, files in os.walk(oebps):
            for fn in files:
                full = os.path.join(root, fn)
                arc = 'OEBPS/' + os.path.relpath(full, oebps).replace('\\', '/')
                z.write(full, arc, compress_type=zipfile.ZIP_DEFLATED)
    print(f'wrote {out_path}')


if __name__ == '__main__':
    pages, nslots = extract_chapter('プロローグ1日目', 'us', pages=2)
    print(f'chapter pages kept: {len(pages)} (text slots in DAT: {nslots})')
    resolver = ImageResolver()
    for p in pages:
        print('page bg:', p['bg'])
    build_epub(pages, os.path.join(HERE, 'FateStayNight_Prologue_sample.epub'),
               resolver=resolver)
