#!/usr/bin/env python3
"""Work out the type specs of an outlined line from its ink box.

Outlined type has no font data left, only glyph shapes. Given the words on the line,
the font, and the line's ink box (from illreader.py's "OUTLINED TYPE" entries, in
artboard points, y down), this shapes the words with the real font file and reports:

  size      from the ink height (horizontal scaling doesn't change height)
  h-scale   from the ink width, assuming no extra tracking
  tracking  the alternative: the tracking that gives the same width at 100% scale
  baseline  where the baseline sits, from the ink bottom and the font's descent

Shadowed or doubled lettering (the reader marks its baseline "approx.") inflates the
ink height. For those, size from one clean letter instead: --glyph R 11.6 means the
"R" outline is 11.6 pt tall (take it from that glyph's path in the --json output).

Usage:
    measure_type.py --font Fraunces-Bold --text "WHY CHOOSE" --ink 63.9 28.4 305.1 54.1
    measure_type.py --font SantoroScriptJF --text "Real Good for You" --ink ... --glyph R 11.6
    measure_type.py --lines lines.json     # [{"font", "text", "ink": [x0,y0,x1,y1], "glyph"?: ["R", 11.6]}, ...]
    measure_type.py --find Santoro         # list matching installed fonts

--font takes a PostScript name (Fraunces-Bold), "Family Style" (Fraunces Bold) or a
font file path. Fonts are looked up in the system font folders for Windows, macOS
and Linux (and Windows' folders from WSL). To add others, such as a font manager's
folder, pass --font-dir DIR (repeatable) or set ILLREADER_FONT_DIRS to a list of
folders separated by the OS path separator (":" on macOS/Linux, ";" on Windows).
"""
import argparse
import glob
import json
import os
import sys

import uharfbuzz as hb
from fontTools.ttLib import TTFont

CACHE = os.path.expanduser('~/.cache/illreader-fonts.json')


def system_font_dirs():
    """The standard places each OS installs fonts, for this machine and user."""
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        windir = os.environ.get('WINDIR', r'C:\Windows')
        local = os.environ.get('LOCALAPPDATA', os.path.join(home, 'AppData', 'Local'))
        return [os.path.join(windir, 'Fonts'), os.path.join(local, 'Microsoft', 'Windows', 'Fonts')]
    if sys.platform == 'darwin':
        return ['/System/Library/Fonts', '/Library/Fonts', os.path.join(home, 'Library', 'Fonts')]
    dirs = [os.path.join(home, '.local', 'share', 'fonts'), os.path.join(home, '.fonts'),
            '/usr/local/share/fonts', '/usr/share/fonts']
    # under WSL, also see the fonts installed on the Windows side
    if os.path.isdir('/mnt/c/Windows/Fonts'):
        dirs += ['/mnt/c/Windows/Fonts'] + glob.glob('/mnt/c/Users/*/AppData/Local/Microsoft/Windows/Fonts')
    return dirs


def font_dirs(extra=()):
    """Folders from --font-dir and ILLREADER_FONT_DIRS first, then the system ones."""
    env = [d for d in os.environ.get('ILLREADER_FONT_DIRS', '').split(os.pathsep) if d]
    return [os.path.expanduser(d) for d in list(extra) + env] + system_font_dirs()


def font_index(dirs):
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}
    index, changed = {}, False
    for d in dirs:
        for path in glob.glob(d + '/**/*', recursive=True):
            if not path.lower().endswith(('.ttf', '.otf')) or '/.' in path:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            hit = cache.get(path)
            if not hit or hit['mtime'] != mtime:
                try:
                    names = TTFont(path, lazy=True, fontNumber=0)['name']
                    hit = {'mtime': mtime,
                           'ps': str(names.getDebugName(6) or ''),
                           'family': str(names.getDebugName(16) or names.getDebugName(1) or ''),
                           'style': str(names.getDebugName(17) or names.getDebugName(2) or '')}
                except Exception:
                    hit = {'mtime': mtime, 'ps': '', 'family': '', 'style': ''}
                changed = True
            index[path] = hit
    if changed:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(index, open(CACHE, 'w'))
    return index


def norm(s):
    return ''.join(ch for ch in s.lower() if ch.isalnum())


def find_font(name, index):
    if os.path.isfile(name):
        return name
    key = norm(name)
    for match in (lambda f: norm(f['ps']) == key,
                  lambda f: norm(f['family'] + f['style']) == key,
                  lambda f: norm(f['family']) == key and norm(f['style']) == 'regular'):
        hits = sorted(p for p, f in index.items() if match(f))
        if hits:
            # prefer static instances over variable fonts (default instance only)
            return sorted(hits, key=lambda p: 'variable' in p.lower())[0]
    return None


_fonts = {}


def shaped_ink(path, text):
    """Ink box of the shaped text in em units, y up: (x0, y0, x1, y1), glyph count."""
    if path not in _fonts:
        face = hb.Face(hb.Blob.from_file_path(path))
        _fonts[path] = (hb.Font(face), face.upem)
    font, upem = _fonts[path]
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {'kern': True, 'liga': True})
    x = 0
    xs, ys = [], []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        e = font.get_glyph_extents(info.codepoint)
        if e and e.width:
            left = x + pos.x_offset + e.x_bearing
            top = pos.y_offset + e.y_bearing
            xs += [left, left + e.width]
            ys += [top, top + e.height]
        x += pos.x_advance
    if not xs:
        return None
    return [min(xs) / upem, min(ys) / upem, max(xs) / upem, max(ys) / upem], len(buf.glyph_infos)


def measure(path, text, ink, glyph=None):
    x0, y0, x1, y1 = ink
    shape = shaped_ink(path, text)
    if not shape:
        return {'error': 'the font draws nothing for this text'}
    (ex0, ey0, ex1, ey1), n = shape
    if glyph:
        one = shaped_ink(path, glyph[0])
        if not one:
            return {'error': 'the font has no "%s"' % glyph[0]}
        size = float(glyph[1]) / (one[0][3] - one[0][1])
    else:
        size = (y1 - y0) / (ey1 - ey0)
    natural = (ex1 - ex0) * size
    gaps = max(len(text) - 1, 1)
    return {'size': round(size, 2),
            'h_scale': round((x1 - x0) / natural * 100, 1),
            'tracking_alt': round(((x1 - x0) - natural) / gaps / size * 1000),
            'baseline': round(y1 + ey0 * size, 2),
            'natural_width': round(natural, 2), 'ink_width': round(x1 - x0, 2)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--font')
    ap.add_argument('--text')
    ap.add_argument('--ink', nargs=4, type=float, metavar=('X0', 'Y0', 'X1', 'Y1'))
    ap.add_argument('--glyph', nargs=2, metavar=('CHAR', 'HEIGHT'), help='size from one clean letter\'s outline height')
    ap.add_argument('--lines', help='JSON list of {"font", "text", "ink", "glyph"?}')
    ap.add_argument('--find', help='list installed fonts whose name contains this')
    ap.add_argument('--font-dir', action='append', default=[], metavar='DIR',
                    help='also look for fonts here (repeatable); searched before the system folders')
    args = ap.parse_args()

    index = font_index(font_dirs(args.font_dir))
    if args.find:
        key = norm(args.find)
        for p, f in sorted(index.items(), key=lambda kv: kv[1]['ps']):
            if key in norm(f['ps'] + f['family']):
                print('%-40s %-28s %-14s %s' % (f['ps'], f['family'], f['style'], p))
        return
    if args.lines:
        jobs = json.load(open(args.lines))
    elif args.font and args.text and args.ink:
        jobs = [{'font': args.font, 'text': args.text, 'ink': args.ink, 'glyph': args.glyph}]
    else:
        ap.error('give --font, --text and --ink, or --lines, or --find')
    for j in jobs:
        path = find_font(j['font'], index)
        if not path:
            print('%s: font "%s" not found (install or activate it, add its folder with --font-dir, or try --find)' % (j['text'][:40], j['font']))
            continue
        r = measure(path, j['text'], j['ink'], j.get('glyph'))
        if 'error' in r:
            print('%s: %s' % (j['text'][:40], r['error']))
            continue
        print('"%s" in %s: size %.2f pt, h-scale %.1f%% (or 100%% with tracking %+d), baseline y %.2f'
              % (j['text'][:50], os.path.basename(path), r['size'], r['h_scale'], r['tracking_alt'], r['baseline'])
              + (' (sized from "%s"; baseline from the ink bottom, check it)' % j['glyph'][0] if j.get('glyph') else ''))


if __name__ == '__main__':
    sys.exit(main())
