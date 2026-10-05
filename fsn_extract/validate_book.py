import glob
import os
import re
import sys
import xml.dom.minidom as md

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from .config import TEMP
except ImportError:  # run as a plain script
    sys.path.insert(0, HERE)
    from config import TEMP  # noqa: E402


def check(WORK):
    """Validate one unpacked EPUB tree (the .../OEBPS dir). -> (errs, warns)"""
    errs = []
    warns = []

    files = glob.glob(os.path.join(WORK, '**', '*.xhtml'), recursive=True)
    # an EPUB without META-INF/container.xml is invalid — readers reject the file
    cont = os.path.join(os.path.dirname(WORK), 'META-INF', 'container.xml')
    if not os.path.exists(cont):
        errs.append(f'META-INF/container.xml missing (looked at {cont})')
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

    anchors = set()
    hrefs = []
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
    for img in sorted(all_imgs):
        if not os.path.exists(os.path.join(WORK, img)):
            errs.append(f'image missing on disk: {img}')
        elif img not in items.values():
            errs.append(f'image not in manifest: {img}')

    return errs, warns, len(files), len(hrefs), len(all_imgs)


def main():
    if len(sys.argv) > 1:
        works = [sys.argv[1]]
    else:
        # every volume's unpacked tree: <output>/temp/book_work_v*/OEBPS
        works = sorted(glob.glob(os.path.join(TEMP, 'book_work*', 'OEBPS')))
    if not works:
        sys.exit('nothing to validate: no book_work*/OEBPS trees found under '
                 + TEMP)

    tot_e = tot_w = 0
    for WORK in works:
        label = os.path.basename(os.path.dirname(WORK))
        errs, warns, nfiles, nhrefs, nimgs = check(WORK)
        print(f'== {label}: checked {nfiles} xhtml files, '
              f'{nhrefs} links, {nimgs} images')
        for e in errs[:20]:
            print('  E:', e)
        for w in warns[:10]:
            print('  W:', w)
        tot_e += len(errs)
        tot_w += len(warns)
    print(f'errors: {tot_e}')
    print(f'warnings: {tot_w}')
    sys.exit(1 if tot_e else 0)


if __name__ == '__main__':
    main()
