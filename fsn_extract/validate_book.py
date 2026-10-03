import glob
import os
import re
import sys
import xml.dom.minidom as md

WORK = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'book_work', 'OEBPS')
errs = []
warns = []

files = glob.glob(os.path.join(WORK, '**', '*.xhtml'), recursive=True)
# an EPUB without META-INF/container.xml is invalid — readers reject the file
cont = os.path.join(os.path.dirname(WORK), 'META-INF', 'container.xml')
if not os.path.exists(cont):
    errs.append(f'META-INF/container.xml missing (looked at {cont})')
errs = errs
anchors = set()
hrefs = []
for f in files:
    rel = os.path.relpath(f, WORK).replace('\\', '/')
    try:
        md.parse(f)
    except Exception as e:
        errs.append(f'{rel}: not well-formed: {e}')
    src = open(f, encoding='utf-8').read()
    for m in re.finditer(r'id="([^"]+)"', src):
        anchors.add(rel + '#' + m.group(1))
    for m in re.finditer(r'href="([^"]+)"', src):
        hrefs.append((rel, m.group(1)))
    if '$$$' in src:
        warns.append(f'{rel}: unresolved $$$ slot')
    if re.search(r'\[(?:lr|line\d+|r)\]', src):
        warns.append(f'{rel}: leftover pacing tag')

missing_imgs = []
for rel, href in hrefs:
    if href.startswith(('http', 'mailto')):
        continue
    target = href.split('#')[0]
    if target:
        if not os.path.exists(os.path.join(WORK, target)):
            parent_dir = os.path.dirname(rel)
            t = os.path.normpath(os.path.join(parent_dir, target)).replace('\\', '/')
            if not os.path.exists(os.path.join(WORK, t)):
                errs.append(f'{rel}: broken link target {href}')
                continue
            href = t if '#' not in href else t + '#' + href.split('#', 1)[1]
    if '#' in href and href not in anchors:
        errs.append(f'{rel}: missing anchor {href}')

# manifest coverage
opf = open(os.path.join(WORK, 'content.opf'), encoding='utf-8').read()
spine_ids = re.findall(r'<itemref idref="([^"]+)"', opf)
items = dict(re.findall(r'<item id="([^"]+)" href="([^"]+)"', opf))
for sid in spine_ids:
    if sid not in items:
        errs.append(f'spine id {sid} missing from manifest')
for m in re.finditer(r'<img src="([^"]+)"', open(
        os.path.join(WORK, 'text', 'p0_c00.xhtml'), encoding='utf-8').read()):
    pass
# every image referenced anywhere (inline <img>, CSS background in xhtml,
# or generated rules in style.css) must be on disk and in the manifest
all_imgs = set()
scan_files = list(files) + glob.glob(os.path.join(WORK, '**', '*.css'),
                                     recursive=True)
for f in scan_files:
    src = open(f, encoding='utf-8').read()
    fdir = os.path.dirname(os.path.relpath(f, WORK))
    for m in re.finditer(r'src="([^"]+)"', src):
        p = m.group(1).split('#')[0]
        if p.endswith(('.jpg', '.png', '.webp')):
            all_imgs.add(os.path.normpath(
                os.path.join(fdir, p)).replace('\\', '/'))
    for m in re.finditer(r'url\(\'?([^)\']+?)\'?\)', src):
        p = m.group(1)
        if p.endswith(('.jpg', '.png', '.webp')):
            all_imgs.add(os.path.normpath(
                os.path.join(fdir, p)).replace('\\', '/'))
for img in all_imgs:
    if not os.path.exists(os.path.join(WORK, img)):
        errs.append(f'image missing on disk: {img}')
    if img not in items.values():
        errs.append(f'image not in manifest: {img}')

print(f'checked {len(files)} xhtml files, {len(hrefs)} links, {len(all_imgs)} images')
print(f'errors: {len(errs)}')
for e in errs[:20]:
    print('  E:', e)
print(f'warnings: {len(warns)}')
for w in warns[:10]:
    print('  W:', w)
sys.exit(1 if errs else 0)
