#!/usr/bin/env python3
"""Read an Adobe Illustrator .ai file's native data and report its structure.

Reads (never writes) a PDF-compatible .ai file and prints layers, sublayers, named
groups, live text (copy, font, size, leading, tracking, alignment, CMYK fill),
live rectangles (corner radii), gradients, placed images and the guides layer.
All positions are in points relative to the artboard's top-left corner, y down.

Usage:
    illreader.py FILE.ai                 # readable outline
    illreader.py FILE.ai --json OUT.json # full structured dump
    illreader.py FILE.ai --all           # outline including small unnamed paths
"""
import argparse
import base64
import json
import re
import sys
import zlib

warnings = []


# ---------------------------------------------------------------- extraction

def native_bytes(path):
    """Return Illustrator's native PostScript-like data from the .ai file."""
    native = _native_bytes(path)
    # files saved on classic-style Macs end lines with CR only; the parser splits on LF
    if b'\n' not in native[:200_000] and b'\r' in native[:200_000]:
        native = native.replace(b'\r', b'\n')
    return native


def _native_bytes(path):
    data = open(path, 'rb').read()
    refs = sorted((int(n), int(o)) for n, o in re.findall(rb'/AIPrivateData(\d+)\s+(\d+)\s+0\s+R', data))
    if not refs:
        if data.startswith(b'%!PS'):
            return data
        sys.exit('No Illustrator native data found. Save the .ai with "Create PDF Compatible File" on, '
                 'or this is not an Illustrator file.')
    objs = {int(m.group(1)): m.start() for m in re.finditer(rb'(?<![0-9])(\d+) 0 obj', data)}
    stream_hdr = re.compile(rb'obj(.*?)stream\r?\n', re.S)
    parts = []
    for _, num in refs:
        m = stream_hdr.search(data, objs.get(num, -1)) if num in objs else None
        if not m:
            sys.exit('Could not find native data object %d.' % num)
        length = int(re.search(rb'/Length (\d+)', m.group(1)).group(1))
        raw = data[m.end():m.end() + length]
        # filters apply in the order listed; CorelDRAW's .ai export hex-encodes before Flate
        for f in re.findall(rb'/(ASCIIHexDecode|FlateDecode)', m.group(1)):
            if f == b'ASCIIHexDecode':
                raw = bytes.fromhex(re.sub(rb'\s', b'', raw.split(b'>')[0]).decode('ascii'))
            else:
                raw = zlib.decompress(raw)
        parts.append(raw)
    blob = b''.join(parts)
    # a thumbnail header may come before the compressed payload (Illustrator 23 and earlier)
    z = blob.find(b'%AI24_ZStandard_Data', 0, 4_000_000)
    if z >= 0:
        import zstandard
        start = blob.index(b'(\xb5/\xfd', z)  # "(" is the first byte of the zstd magic
        out, rest, dec = [], blob[start:], zstandard.ZstdDecompressor()
        while rest.startswith(b'(\xb5/\xfd'):
            obj = dec.decompressobj()
            out.append(obj.decompress(rest))
            rest = obj.unused_data
        return b''.join(out)
    z = blob.find(b'%AI12_CompressedData', 0, 4_000_000)
    if z >= 0:
        obj = zlib.decompressobj()
        return obj.decompress(blob[z + len(b'%AI12_CompressedData'):])
    return blob


def lines_of(native):
    """Yield (line_bytes) while skipping binary %%BeginData sections."""
    i, n = 0, len(native)
    while i < n:
        j = native.find(b'\n', i)
        if j < 0:
            j = n
        line = native[i:j].rstrip(b'\r')
        i = j + 1
        if line.startswith(b'%%BeginData:') or line.startswith(b'%_%%BeginData:'):
            end = native.find(b'%%EndData', i)
            i = n if end < 0 else native.find(b'\n', end) + 1 or n
            continue
        if len(line) > 4000 or b'\x00' in line:
            continue  # stray binary payload, never drawing operators
        yield line


# ----------------------------------------------------- text engine (AI 2020+)

_NAME = re.compile(rb'/[^\s/\[\]<>()]*')
_ATOM = re.compile(rb'[^\s/\[\]<>()]+')


def _tokens(b):
    i, n = 0, len(b)
    esc = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8, ord('f'): 12}
    while i < n:
        c = b[i]
        if c in b' \t\r\n':
            i += 1
        elif b[i:i + 2] in (b'<<', b'>>'):
            yield (b[i:i + 2].decode(),)
            i += 2
        elif c in b'[]':
            yield (chr(c),)
            i += 1
        elif c == 0x28:
            i += 1
            depth, out = 1, bytearray()
            while True:
                ch = b[i]
                if ch == 0x5c:
                    nx = b[i + 1]
                    if nx in esc:
                        out.append(esc[nx])
                        i += 2
                    elif 0x30 <= nx <= 0x37:
                        j = i + 1
                        while j < i + 4 and 0x30 <= b[j] <= 0x37:
                            j += 1
                        out.append(int(b[i + 1:j], 8) & 255)
                        i = j
                    else:
                        out.append(nx)
                        i += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                out.append(ch)
                i += 1
            yield ('str', bytes(out))
        elif c == 0x2f:
            m = _NAME.match(b, i)
            yield ('name', m.group()[1:].decode('latin1'))
            i = m.end()
        else:
            m = _ATOM.match(b, i)
            t = m.group().decode('latin1')
            i = m.end()
            for conv in (int, float):
                try:
                    t = conv(t)
                    break
                except ValueError:
                    pass
            yield ('val', {'true': True, 'false': False}.get(t, t) if isinstance(t, str) else t)


def parse_textdoc(b):
    toks = list(_tokens(b))
    pos = 0

    def val():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t[0] == '<<':
            d = {}
            while toks[pos][0] != '>>':
                k = toks[pos][1]
                pos += 1
                d[k] = val()
            pos += 1
            return d
        if t[0] == '[':
            a = []
            while toks[pos][0] != ']':
                a.append(val())
            pos += 1
            return a
        if t[0] == 'str':
            s = t[1]
            return s[2:].decode('utf-16-be', 'replace') if s.startswith(b'\xfe\xff') else s
        if t[0] == 'name':
            return '/' + t[1]
        return t[1]

    root = {}
    while pos < len(toks):
        k = toks[pos][1]
        pos += 1
        root[k] = val()
    return root


def text_document(native):
    m = re.search(rb'/AI11TextDocument : /ASCII85Decode ,\r?\n(.*?)~>', native, re.S)
    if not m:
        return None
    a85 = b''.join(l[1:].strip() for l in m.group(1).splitlines() if l.startswith(b'%'))
    return parse_textdoc(base64.a85decode(a85, adobe=False))


JUSTIFY = {0: 'left', 1: 'right', 2: 'center', 3: 'justify', 4: 'justify', 5: 'justify', 6: 'justify-all'}
CAPS = {0: None, 1: 'small caps', 2: 'all caps'}


def _cmyk(paint):
    try:
        comps = paint['0']['1']
    except (KeyError, TypeError):
        return None
    if paint['0'].get('0') == 2 and len(comps) == 5:   # alpha, c, m, y, k
        return [round(v * 100, 1) for v in comps[1:]]
    if len(comps) == 4:                                  # alpha, r, g, b
        return {'rgb': [round(v * 255) for v in comps[1:]]}
    return None


def g(d, *path):
    """Safe nested lookup in the decoded text document."""
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and k < len(d):
            d = d[k]
        else:
            return None
    return d


def stories(td):
    """Decode every story: text, paragraphs and character runs."""
    if not td or not isinstance(td.get('0'), dict) or not isinstance(td.get('1'), dict):
        return [], []
    res = td['0']

    def items(d, *path):
        for k in path:
            d = d.get(k) if isinstance(d, dict) else None
        return d if isinstance(d, list) else []
    fonts = [g(f, '0', '0', '0') for f in items(res, '1', '0')]
    char_styles = [g(s_, '0', '6') or {} for s_ in items(res, '5', '0')]
    para_styles = [g(s_, '0', '5') or {} for s_ in items(res, '6', '0')]
    frames = [g(f, '0') or {} for f in items(res, '8', '0')]
    char_default, para_default = td['1'].get('2', {}), td['1'].get('3', {})
    out = []
    for s in items(td, '1', '1'):
        try:
            model = s['0']
            text = model['0']
            paras, pos = [], 0
            for run in model.get('5', {}).get('0', []):
                st = run['0']['0']
                attrs = dict(para_default)
                attrs.update(para_styles[st.get('6', 0)] if isinstance(st.get('6'), int) and st.get('6', 0) < len(para_styles) else {})
                attrs.update(st.get('5', {}))
                n = run['1']
                paras.append({'text': text[pos:pos + n].rstrip('\r'),
                              'align': JUSTIFY.get(attrs.get('0'), attrs.get('0')),
                              'first_indent': attrs.get('1', 0), 'left_indent': attrs.get('2', 0),
                              'right_indent': attrs.get('3', 0), 'space_before': attrs.get('4', 0),
                              'space_after': attrs.get('5', 0), 'auto_leading': attrs.get('7', 1.2)})
                pos += n
            runs, pos = [], 0
            for run in model.get('6', {}).get('0', []):
                st = run['0']['0']
                attrs = dict(char_default)
                parent = st.get('5', 0)
                if isinstance(parent, int) and parent < len(char_styles):
                    attrs.update(char_styles[parent])
                attrs.update(st.get('6', {}))
                n = run['1']
                size = attrs.get('1')
                leading = None if attrs.get('4', True) else attrs.get('5')
                runs.append({'text': text[pos:pos + n].replace('\r', '\\n'),
                             'font': fonts[attrs['0']] if isinstance(attrs.get('0'), int) and attrs['0'] < len(fonts) else attrs.get('0'),
                             'size': size, 'leading': leading, 'tracking': attrs.get('8', 0),
                             'caps': CAPS.get(attrs.get('12', 0)), 'fill_cmyk': _cmyk(attrs.get('53')),
                             'h_scale': attrs.get('6', 1.0), 'baseline_shift': attrs.get('9', 0)})
                pos += n
            # view: frame position, line boxes
            lines, frame_idx = [], None
            for pc in s.get('1', {}).get('2', []):
                for fr in pc.get('6', []):
                    frame_idx = fr.get('10', frame_idx)
                    _collect_lines(fr, lines)
            frame = frames[frame_idx] if isinstance(frame_idx, int) and frame_idx < len(frames) else {}
            out.append({'text': text.rstrip('\r'), 'paragraphs': paras, 'runs': runs,
                        'frame': frame, 'lines': lines})
        except (KeyError, TypeError, IndexError, AttributeError) as e:
            warnings.append('skipped a text story Illustrator stored in an unfamiliar way (%s)' % e)
            out.append({'text': '', 'paragraphs': [], 'runs': [], 'frame': {}, 'lines': []})
    return out, fonts


def _collect_lines(node, acc):
    if isinstance(node, dict):
        if node.get('99') == '/L':
            boxes = []
            _collect_glyph_boxes(node, boxes)
            if boxes:
                acc.append({'box': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                    max(b[2] for b in boxes), max(b[3] for b in boxes)],
                            'ascent': -node.get('14', 0)})
            return
        for v in node.values():
            _collect_lines(v, acc)
    elif isinstance(node, list):
        for v in node:
            _collect_lines(v, acc)


def _collect_glyph_boxes(node, acc):
    if isinstance(node, dict):
        if node.get('99') == '/G' and isinstance(node.get('8'), list):
            acc.append(node['8'])
        for v in node.values():
            _collect_glyph_boxes(v, acc)
    elif isinstance(node, list):
        for v in node:
            _collect_glyph_boxes(v, acc)


# ------------------------------------------------------------------ art tree

NUM = re.compile(rb'^-?\d*\.?\d+(?:[eE]-?\d+)?$')
GLUED = re.compile(rb'^([A-Za-z]{1,2})(-?\d*\.?\d+)$')


def clean_name(raw):
    name = re.sub(r'_0{3,}\d+_?$', '', raw)
    name = re.sub(r'_x([0-9A-Fa-f]{2,4})_', lambda m: chr(int(m.group(1), 16)), name)
    return re.sub(r'\s+', ' ', name.replace('_', ' ')).strip()


def split_ps(line):
    """Tokenize one PostScript line, keeping (strings) whole."""
    if b'(' not in line and b'[' not in line and b']' not in line:   # fast path: most path data
        out = []
        for tok in line.split():
            m = GLUED.match(tok) if tok[:1].isalpha() and len(tok) > 1 else None
            if m:
                out += [m.group(1), m.group(2)]
            else:
                out.append(tok)
        return out
    out, i, n = [], 0, len(line)
    while i < n:
        c = line[i:i + 1]
        if c in b' \t':
            i += 1
        elif c == b'(':
            depth, j = 1, i + 1
            while j < n and depth:
                if line[j:j + 1] == b'\\':
                    j += 2
                    continue
                if line[j:j + 1] == b'(':
                    depth += 1
                elif line[j:j + 1] == b')':
                    depth -= 1
                j += 1
            out.append(line[i:j])
            i = j
        elif c in b'[]':
            out.append(c)
            i += 1
        else:
            j = i
            while j < n and line[j:j + 1] not in b' \t[]()':
                j += 1
            if j == i:      # stray ")" with no opening "(": skip it, or we'd never advance
                i += 1
                continue
            tok = line[i:j]
            m = GLUED.match(tok)
            if m:   # Illustrator sometimes omits the space after an operator: "1 w0 R", "1 w442 12 m"
                out += [m.group(1), m.group(2)]
            else:
                out.append(tok)
            i = j
    return out


def bez_points(p0, p1, p2, p3, steps=12):
    pts = []
    for k in range(1, steps + 1):
        t = k / steps
        mt = 1 - t
        pts.append((mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1]))
    return pts


def new(kind, **kw):
    node = {'kind': kind, 'name': None, 'children': [], 'bbox': None, 'hidden': False}
    node.update(kw)
    return node


def union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def walk_art(native):
    start = native.find(b'%AI5_BeginLayer')
    if start < 0:
        return [], {}
    root = new('root')
    stack = [root]
    last = None
    st = {'fill': None, 'stroke': None, 'sw': 1.0, 'grad': None, 'in_grad': False, 'hidden': False}
    path = {'pts': [], 'cur': None, 'clip': False}
    pending_layer = False
    text_rec = None
    raster = None
    ruler_guides = []
    body = native[start:].replace(b'\r\n', b'\n').replace(b'\r', b'\n')   # some lines end in a bare CR
    for line in lines_of(body):
        if raster is not None:
            if line.startswith(b'%AI5_EndRaster'):
                stack[-1]['children'].append(raster)
                last, raster = raster, None
                continue
            m = re.match(rb'^\((.*)\) \d+ XG$', line)
            if m:
                raster['file'] = m.group(1).decode('latin1').replace('\\\\', '\\')
            m = re.match(rb'^\[ ([-\d.e ]+) \] (\d+) (\d+) \d+ Xh$', line)
            if m:
                a, b, c, d, tx, ty = (float(v) for v in m.group(1).split())
                w, h = int(m.group(2)), int(m.group(3))
                xs = [tx, tx + a * w + c * h]
                ys = [ty, ty - (b * w + d * h)]
                raster['bbox'] = [min(xs), min(ys), max(xs), max(ys)]
                raster['pixels'] = [w, h]
            continue
        if text_rec is not None:
            m = re.match(rb'^(\d+) /(StoryIndex|FrameIndex) ,', line)
            if m:
                text_rec[m.group(2).decode()] = int(m.group(1))
            if line.strip() == b';':
                node = new('text', story=text_rec.get('StoryIndex'), frame=text_rec.get('FrameIndex'),
                           hidden=text_rec['hidden'])
                stack[-1]['children'].append(node)
                last, text_rec = node, None
            continue
        if line.startswith(b'%'):
            if line.startswith(b'%AI5_BeginLayer'):
                layer = new('layer')
                stack[-1]['children'].append(layer)
                stack.append(layer)
                last, pending_layer = layer, True
            elif line.startswith(b'%AI5_EndLayer'):
                if len(stack) > 1 and stack[-1]['kind'] == 'layer':
                    stack.pop()
            elif line.startswith(b'%AI5_BeginRaster'):
                raster = new('image', hidden=st['hidden'])
            elif line.startswith(b'%_'):
                m = re.search(rb'/XMLUID : \((.*?)\) ; \(AI10_ArtUID\)', line)
                if m and last is not None and last['kind'] != 'layer' and not last.get('name'):
                    last['name'] = clean_name(m.group(1).decode('latin1'))
                m = re.search(rb'^%_([-\d.e]+) /Real \(ai::(Rectangle|RoundedRectangle)::(CornerRadius)::(\d)\)', line)
                if m and last is not None:
                    last.setdefault('corner_radii', [0, 0, 0, 0])[int(m.group(4))] = round(float(m.group(1)), 3)
                m = re.search(rb'^%_\(ai::(\w+)\) /UnicodeString \(ai::LiveShape::HandlerName\)', line)
                if m and last is not None:
                    last['live_shape'] = m.group(1).decode()
            elif line.startswith(b'%AI3_EndPattern') or line.startswith(b'%%Trailer') or line.startswith(b'%%PageTrailer'):
                break
            continue
        s = line.strip()
        if pending_layer:
            m = re.match(rb'^([\d ]+) Lb$', s)
            if m:
                f = [int(v) for v in m.group(1).split()]
                stack[-1]['visible'] = bool(f[0])
                stack[-1]['printable'] = bool(f[3]) if len(f) > 3 else True
                continue
            m = re.match(rb'^\((.*)\) Ln$', s)
            if m:
                stack[-1]['name'] = m.group(1).decode('latin1')
                pending_layer = False
                continue
        m = re.match(rb'^([01]) Xw$', s)
        if m:
            st['hidden'] = m.group(1) == b'1'   # object visibility flag for what follows
            continue
        if s == b'/AI11Text :':
            text_rec = {'hidden': st['hidden']}
            continue
        if s in (b'u', b'q', b'*u'):
            node = new({b'u': 'group', b'q': 'clip group', b'*u': 'compound path'}[s], hidden=st['hidden'])
            stack[-1]['children'].append(node)
            stack.append(node)
            continue
        if s in (b'U', b'Q', b'*U'):
            if len(stack) > 1 and stack[-1]['kind'] != 'layer':
                last = stack.pop()
            continue
        if s == b'Bb' or s.endswith(b' Bb'):
            st['in_grad'] = True
            st['grad'] = st['sgrad'] = None
            continue
        if s == b'BB' or s.endswith(b' BB'):
            st['in_grad'] = False
            continue
        if s.endswith(b' Bg'):
            m = re.match(rb'^(\d+) \((.*?)\) ([-\d.e]+) ([-\d.e]+) ([-\d.e]+) ([-\d.e]+)', s)
            if m:
                g = {'gradient': m.group(2).decode('latin1'), 'angle': round(float(m.group(5)), 2)}
                st['sgrad' if m.group(1) == b'1' else 'grad'] = g   # 1 = stroke, 2 = fill
            continue
        toks = split_ps(s)
        nums = []
        for t in toks:
            if NUM.match(t):
                nums.append(float(t))
                continue
            op = t
            if op == b'm' and len(nums) >= 2:
                path = {'pts': [], 'cur': (nums[-2], nums[-1]), 'clip': False}
                path['pts'].append(path['cur'])
            elif op == b'*' and b'(N)' in toks and path['pts']:
                ruler_guides.append(path['pts'][:2])   # "(N) *" ends a ruler guide
                path = {'pts': [], 'cur': None, 'clip': False}
            elif op in (b'l', b'L') and len(nums) >= 2:
                path['cur'] = (nums[-2], nums[-1])
                path['pts'].append(path['cur'])
            elif op in (b'c', b'C') and len(nums) >= 6 and path['cur']:
                p1, p2, p3 = (nums[-6], nums[-5]), (nums[-4], nums[-3]), (nums[-2], nums[-1])
                path['pts'] += bez_points(path['cur'], p1, p2, p3)
                path['cur'] = p3
            elif op in (b'v', b'V') and len(nums) >= 4 and path['cur']:
                p2, p3 = (nums[-4], nums[-3]), (nums[-2], nums[-1])
                path['pts'] += bez_points(path['cur'], path['cur'], p2, p3)
                path['cur'] = p3
            elif op in (b'y', b'Y') and len(nums) >= 4 and path['cur']:
                p1, p3 = (nums[-4], nums[-3]), (nums[-2], nums[-1])
                path['pts'] += bez_points(path['cur'], p1, p3, p3)
                path['cur'] = p3
            elif op == b'W':
                path['clip'] = True
            elif op in (b'f', b'F', b's', b'S', b'b', b'B', b'n', b'N') and path['pts']:
                xs = [p[0] for p in path['pts']]
                ys = [p[1] for p in path['pts']]
                filled = op in (b'f', b'F', b'b', b'B')
                stroked = op in (b's', b'S', b'b', b'B')
                node = new('path', bbox=[min(xs), min(ys), max(xs), max(ys)], hidden=st['hidden'])
                if path['clip']:
                    node['clip'] = True
                if filled:
                    node['fill'] = st['grad'] if st['in_grad'] and st['grad'] else st['fill']
                if stroked:
                    node['stroke'] = st.get('sgrad') if st['in_grad'] and st.get('sgrad') else st['stroke']
                    node['stroke_width'] = st['sw']
                stack[-1]['children'].append(node)
                last = node
                path = {'pts': [], 'cur': None, 'clip': False}
            elif op == b'k' and len(nums) >= 4:
                st['fill'] = {'cmyk': [round(v * 100, 1) for v in nums[-4:]]}
            elif op == b'K' and len(nums) >= 4:
                st['stroke'] = {'cmyk': [round(v * 100, 1) for v in nums[-4:]]}
            elif op == b'g' and nums:
                st['fill'] = {'gray': round(nums[-1] * 100, 1)}
            elif op == b'G' and nums:
                st['stroke'] = {'gray': round(nums[-1] * 100, 1)}
            elif op in (b'Xa', b'XA') and len(nums) >= 3:
                key = 'fill' if op == b'Xa' else 'stroke'
                st[key] = {'rgb': [round(v * 255) for v in nums[-3:]]}
            elif op in (b'x', b'X'):
                strs = [t2 for t2 in toks if t2.startswith(b'(')]
                cm = [float(v) for v in toks[:4] if NUM.match(v)]
                key = 'fill' if op == b'x' else 'stroke'
                col = {'swatch': strs[0][1:-1].decode('latin1') if strs else None}
                if len(cm) == 4:
                    col['cmyk'] = [round(v * 100, 1) for v in cm]
                st[key] = col
            elif op == b'w' and nums:
                st['sw'] = round(nums[-1], 3)
            if not NUM.match(t):
                nums = []
    return root['children'], ruler_guides


def gradient_defs(native):
    """Return {name: [{'location', 'cmyk'|'rgb'|'gray'}]} from %AI5_BeginGradient blocks."""
    out = {}
    for m in re.finditer(rb'%AI5_BeginGradient: \((.*?)\)\r?\n(.*?)%AI5_EndGradient', native, re.S):
        stops = []
        for sm in re.finditer(rb'^%_([-\d.e ]+?) Bs\s*$', m.group(2), re.M):
            v = [float(x) for x in sm.group(1).split()]
            # layouts: c m y k 1 1 TYPE mid loc | r g b 1 1 TYPE mid loc | gray 1 1 TYPE mid loc
            ctype = int(v[-3]) if len(v) >= 3 else None
            stop = {'location': round(v[-1], 2), 'midpoint': round(v[-2], 2)}
            if ctype in (1,) and len(v) >= 8:
                stop['cmyk'] = [round(x * 100, 1) for x in v[:4]]
            elif ctype == 6 and len(v) >= 8:
                stop['cmyk'] = [round(x * 100, 1) for x in v[:4]]
            elif len(v) >= 9:
                stop['cmyk'] = [round(x * 100, 1) for x in v[:4]]
            else:
                stop['values'] = v[:-3]
            stops.append(stop)
        out[m.group(1).decode('latin1')] = sorted(stops, key=lambda s: s['location'])
    return out


def artboards(native):
    out = []
    end = native.find(b'%AI5_BeginLayer')
    head = native[:end if end > 0 else len(native)]
    pt = rb'%%_([-\d.e]+) ([-\d.e]+) /RealPointRelToROrigin\r?\n%%_ \(%s\) ,'
    # each artboard is one "%_/Dictionary :" chunk; key order varies between files
    for chunk in head.split(b'%_/Dictionary :'):
        m1 = re.search(pt % b'PositionPoint1', chunk)
        m2 = re.search(pt % b'PositionPoint2', chunk)
        if not (m1 and m2):
            continue
        nm = re.search(rb'%_\(([^)\r\n]*)\) /UnicodeString \(Name\) ,', chunk)
        x0, y0 = float(m1.group(1)), float(m1.group(2))
        x1, y1 = float(m2.group(1)), float(m2.group(2))
        out.append({'name': nm.group(1).decode('latin1') if nm else 'Artboard %d' % (len(out) + 1),
                    'art_x0': x0, 'art_y0': y0, 'width': round(x1 - x0, 3), 'height': round(y0 - y1, 3)})
    if not out:
        # Illustrator CS3/CS4 and older: one artboard, stored as crop marks (left bottom right top)
        m = re.search(rb'^%AI3_Cropmarks: ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)', head, re.M)
        if m:
            x0, y0, x1, y1 = (float(v) for v in m.groups())
            out.append({'name': 'Artboard 1', 'art_x0': x0, 'art_y0': y1,
                        'width': round(x1 - x0, 3), 'height': round(y1 - y0, 3)})
    return out


# ------------------------------------------------------------------- assembly

def to_page(b, ab):
    if b is None:
        return None
    return [round(b[0] - ab['art_x0'], 2), round(ab['art_y0'] - b[3], 2),
            round(b[2] - ab['art_x0'], 2), round(ab['art_y0'] - b[1], 2)]


def finish(nodes, ab, story_list, text_origin):
    """Compute bounds bottom-up, convert to page coordinates, attach text."""
    for n in nodes:
        finish(n['children'], ab, story_list, text_origin)
        if n['kind'] == 'text' and n.get('story') is not None and n['story'] < len(story_list):
            s = story_list[n['story']]
            fr = s['frame']
            t = fr.get('2', {}).get('2', [1, 0, 0, 1, 0, 0])
            ox, oy = text_origin

            def canvas_to_art(x, y):
                return x + t[4] - ox, oy - (y + t[5])
            boxes = []
            for ln in s['lines']:
                x0, y0 = canvas_to_art(ln['box'][0], ln['box'][1])
                x1, y1 = canvas_to_art(ln['box'][2], ln['box'][3])
                boxes.append([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])
            if boxes:
                n['bbox'] = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                             max(b[2] for b in boxes), max(b[3] for b in boxes)]
                first = s['lines'][0]
                bx, by = canvas_to_art(first['box'][0], first['box'][1] + first['ascent'])
                n['first_baseline'] = [round(bx - ab['art_x0'], 2), round(ab['art_y0'] - by, 2)]
                if len(s['lines']) > 1:
                    tops = [ln['box'][1] + ln['ascent'] for ln in s['lines']]   # baselines
                    n['line_pitches'] = [round(b - a, 2) for a, b in zip(tops, tops[1:])]
            pts = fr.get('1', {}).get('0') if isinstance(fr.get('1'), dict) else None
            if pts and len(pts) >= 4:
                xs, ys = [], []
                for k in range(0, len(pts) - 1, 2):
                    ax, ay = canvas_to_art(pts[k], pts[k + 1])
                    xs.append(ax)
                    ys.append(ay)
                n['frame_type'] = 'area'
                n['frame_bounds'] = to_page([min(xs), min(ys), max(xs), max(ys)], ab)
            else:
                n['frame_type'] = 'point'
            n['text'] = s['text']
            n['paragraphs'] = s['paragraphs']
            n['runs'] = s['runs']
            n['line_count'] = len(s['lines'])
        elif n['children']:
            clips = [c for c in n['children'] if c.get('clip')]
            if n['kind'] == 'clip group' and clips:
                n['bbox'] = clips[0]['bbox']
            else:
                bb = None
                kids = [c for c in n['children'] if not c.get('clip')]
                visible = [c for c in kids if not c.get('hidden')] or kids
                for c in visible:
                    bb = union(bb, c.get('bbox'))
                n['bbox'] = bb
    return nodes


def convert(nodes, ab):
    for n in nodes:
        n['page_bounds'] = to_page(n.get('bbox'), ab)
        convert(n['children'], ab)


# ------------------------------------------------------------ outlined type

def _glyph_leaves(n, acc):
    """Visible paths under n, with compound paths kept whole (an 'o' is one glyph)."""
    if n.get('hidden') or n['kind'] in ('text', 'image') or n.get('clip') or n.get('outlined_lines'):
        return
    if n['kind'] in ('path', 'compound path'):
        if n.get('page_bounds'):
            acc.append(n['page_bounds'])
        return
    for c in n['children']:
        _glyph_leaves(c, acc)


def _rows(boxes):
    hs = sorted(b[3] - b[1] for b in boxes)
    med = hs[len(hs) // 2] if hs else 0
    # art taller than a few letters (icon circles, panels) would swallow whole rows
    glyphs = [b for b in boxes if b[3] - b[1] <= 3 * med + 1]
    rows = []
    for b in sorted(glyphs, key=lambda b: b[1]):
        cy = (b[1] + b[3]) / 2
        for r in rows:
            if r['y0'] - 1 < cy < r['y1'] + 1:
                r['g'].append(b)
                r['y0'], r['y1'] = min(r['y0'], b[1]), max(r['y1'], b[3])
                break
        else:
            rows.append({'y0': b[1], 'y1': b[3], 'g': [b]})
    # dots of i and j form thin rows of their own: fold them into the line below
    rows.sort(key=lambda r: r['y0'])
    for r in [r for r in rows if r['y1'] - r['y0'] < 2.5]:
        below = [o for o in rows if o is not r and o['y1'] - o['y0'] >= 2.5 and 0 <= o['y0'] - r['y0'] < 6]
        if below:
            o = min(below, key=lambda o: o['y0'])
            o['g'] += r['g']
            o['y0'] = min(o['y0'], r['y0'])
            rows.remove(r)
    return rows


def _type_line(g):
    """A row of glyph outlines if it looks like set type, else None."""
    if len(g) < 3 or any(b[3] - b[1] > 80 or b[2] - b[0] > 80 for b in g):
        return None
    ink = [min(b[0] for b in g), min(b[1] for b in g), max(b[2] for b in g), max(b[3] for b in g)]
    bots = [round(b[3] * 4) / 4 for b in g]
    base = max(set(bots), key=bots.count)
    approx = False
    if bots.count(base) < max(2, len(g) * 0.4):
        # script faces and shadowed lettering don't share one baseline; accept long, low rows
        if len(g) < 8 or ink[2] - ink[0] < 4 * (ink[3] - ink[1]):
            return None
        base, approx = sorted(b[3] for b in g)[len(g) // 2], True
    on = [b for b in g if abs(b[3] - base) <= 0.3] or g
    letter = base - min(b[1] for b in on)
    # type runs sideways: a line is wider than tall, and letters are bigger than logo specks
    if letter < 2 or ink[2] - ink[0] < 2.5 * letter:
        return None
    line = {'ink': [round(v, 2) for v in ink], 'baseline': round(base, 2),
            'letter_height': round(letter, 2), 'glyphs': len(g)}
    if approx:
        line['baseline_approx'] = True
    return line


def detect_outlined(nodes):
    """Mark the innermost groups/layers that hold outlined type with their lines,
    so a text group sitting next to an icon group is reported on its own."""
    found = False
    for n in nodes:
        if n['kind'] not in ('group', 'layer') or n.get('hidden'):
            continue
        inner = detect_outlined(n['children'])
        # leaves already reported by inner groups are skipped, so a group's own loose
        # glyphs beside a reported sub-group still get their lines
        leaves = []
        _glyph_leaves(n, leaves)
        lines = [l for l in (_type_line(r['g']) for r in _rows(leaves)) if l] if leaves else []
        if lines and sum(l['glyphs'] for l in lines) >= 0.8 * len(leaves):
            n['outlined_lines'] = lines
        found = found or inner or bool(n.get('outlined_lines'))
    return found


# --------------------------------------------------------------------- output

def fmt_b(b):
    if not b:
        return 'no bounds'
    return 'x %.1f–%.1f, y %.1f–%.1f (%.1f × %.1f)' % (b[0], b[2], b[1], b[3], b[2] - b[0], b[3] - b[1])


def fmt_col(c):
    if not c:
        return None
    if 'gradient' in c:
        return 'gradient "%s" %s°' % (c['gradient'], c['angle'])
    if 'swatch' in c and c['swatch']:
        return 'swatch "%s"%s' % (c['swatch'], (' C%g M%g Y%g K%g' % tuple(c['cmyk'])) if 'cmyk' in c else '')
    if 'cmyk' in c:
        return 'C%g M%g Y%g K%g' % tuple(c['cmyk'])
    if 'gray' in c:
        return 'K%g' % c['gray']
    if 'rgb' in c:
        return 'RGB %s' % c['rgb']
    return str(c)


def interesting(n, show_all):
    if n['kind'] in ('layer', 'text', 'image') or n.get('name') or n.get('corner_radii') or n.get('outlined_lines'):
        return True
    if any(interesting(c, show_all) for c in n['children']):
        return True
    b = n.get('page_bounds')
    if show_all:
        return True
    return bool(b) and n['kind'] == 'path' and (b[2] - b[0]) >= 40 and (b[3] - b[1]) >= 20


def count_paths(n):
    return sum(1 if c['kind'] == 'path' else count_paths(c) for c in n['children'])


def outline(nodes, out, depth=0, show_all=False):
    shown = [n for n in nodes if interesting(n, show_all)]
    hidden = len(nodes) - len(shown)
    # collapse runs of siblings with the same name (e.g. 40 "Cluster" groups)
    i = 0
    while i < len(shown):
        n = shown[i]
        j = i
        while (j + 1 < len(shown) and shown[j + 1].get('name') and shown[j + 1]['name'] == n.get('name')
               and shown[j + 1]['kind'] == n['kind'] and n['kind'] != 'layer'):
            j += 1
        pad = '  ' * depth
        if j > i:
            bb = None
            for k in range(i, j + 1):
                bb = union(bb, shown[k].get('page_bounds') and [shown[k]['page_bounds'][0], shown[k]['page_bounds'][1],
                                                                  shown[k]['page_bounds'][2], shown[k]['page_bounds'][3]])
            out.append('%s- %s "%s" ×%d  [%s]' % (pad, n['kind'], n['name'], j - i + 1, fmt_b(bb)))
            i = j + 1
            continue
        label = n['kind'] + (' "%s"' % n['name'] if n.get('name') else '')
        if n['kind'] == 'layer':
            flags = []
            if n.get('visible') is False:
                flags.append('hidden')
            if n.get('printable') is False:
                flags.append('non-printing')
            out.append('%s- LAYER "%s"%s  [%s]' % (pad, n.get('name'), (' (' + ', '.join(flags) + ')') if flags else '',
                                                   fmt_b(n.get('page_bounds'))))
        elif n['kind'] == 'text':
            out.append('%s- TEXT%s (%s type)  [ink %s]' % (pad, ' (HIDDEN)' if n.get('hidden') else '', n.get('frame_type', '?'),
                                                       fmt_b(n.get('page_bounds'))))
            if n.get('frame_bounds'):
                out.append('%s    text box: %s' % (pad, fmt_b(n['frame_bounds'])))
            if n.get('first_baseline'):
                out.append('%s    first baseline at x %.2f, y %.2f; %d line(s)%s' % (
                    pad, n['first_baseline'][0], n['first_baseline'][1], n.get('line_count', 0),
                    ('; baseline-to-baseline %s' % ', '.join('%g' % v for v in n['line_pitches']))
                    if n.get('line_pitches') else ''))
            for p in n.get('paragraphs', []):
                extras = []
                for k in ('first_indent', 'left_indent', 'right_indent', 'space_before', 'space_after'):
                    if p.get(k):
                        extras.append('%s %g' % (k.replace('_', ' '), p[k]))
                out.append('%s    ¶ %s%s: "%s"' % (pad, p['align'], ('; ' + ', '.join(extras)) if extras else '',
                                                   p['text'][:90]))
            for r in n.get('runs', []):
                bits = ['%s %gpt' % (r['font'], r['size'])]
                bits.append('leading %s' % ('auto' if r['leading'] is None else '%g' % r['leading']))
                if r.get('tracking'):
                    bits.append('tracking %g' % r['tracking'])
                if r.get('caps'):
                    bits.append(r['caps'])
                if r.get('h_scale') not in (None, 1, 1.0):
                    bits.append('h-scale %g%%' % (r['h_scale'] * 100))
                col = r.get('fill_cmyk')
                if isinstance(col, list):
                    bits.append('C%g M%g Y%g K%g' % tuple(col))
                elif col:
                    bits.append(str(col))
                out.append('%s    run: %s — "%s"' % (pad, ', '.join(bits), r['text'][:50]))
        elif n['kind'] == 'image':
            out.append('%s- IMAGE%s %s  [%s]  %s px' % (pad, ' (HIDDEN)' if n.get('hidden') else '', n.get('file', '(embedded)'), fmt_b(n.get('page_bounds')),
                                                      n.get('pixels')))
        else:
            bits = [fmt_b(n.get('page_bounds'))]
            if n.get('fill'):
                bits.append('fill ' + fmt_col(n['fill']))
            if n.get('stroke'):
                bits.append('stroke %s %gpt' % (fmt_col(n['stroke']), n.get('stroke_width', 1)))
            if n.get('corner_radii') and any(n['corner_radii']):
                bits.append('corner radii %s' % n['corner_radii'])
            if n.get('clip'):
                bits.append('clipping path')
            if n['kind'] in ('group', 'clip group', 'compound path') and n['children']:
                bits.append('%d paths inside' % count_paths(n))
            out.append('%s- %s%s  [%s]' % (pad, label, ' (HIDDEN)' if n.get('hidden') else '', '; '.join(bits)))
        for k, l in enumerate(n.get('outlined_lines', []), 1):
            out.append('%s    OUTLINED TYPE line %d: ink %s; baseline y %.2f%s; letter height %.2f; %d glyphs' % (
                pad, k, fmt_b(l['ink']), l['baseline'], ' (approx.)' if l.get('baseline_approx') else '',
                l['letter_height'], l['glyphs']))
        if n['kind'] in ('layer', 'group', 'clip group') and n['children']:
            outline(n['children'], out, depth + 1, show_all)
        i += 1
    if hidden and depth > 0:
        out.append('%s  (+%d small unnamed objects)' % ('  ' * depth, hidden))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('--json', metavar='OUT', help='also write the full structure as JSON')
    ap.add_argument('--all', action='store_true', help='list small unnamed paths too')
    ap.add_argument('--dump-native', metavar='OUT', help='write the decompressed native data (for debugging)')
    args = ap.parse_args()

    native = native_bytes(args.file)
    if args.dump_native:
        open(args.dump_native, 'wb').write(native)
    header = native[:4000].decode('latin1', 'replace')
    fonts_hdr = re.findall(r'^%%(?:DocumentFonts:|\+) (\S+)', header.split('%%DocumentNeededFonts')[0], re.M)
    abs_ = artboards(native)
    if not abs_:
        sys.exit('No artboards found in the native data.')
    td = text_document(native)
    story_list, _ = stories(td)
    m = re.search(rb'([-\d.]+) ([-\d.]+) /RulerOrigin ,', native)
    text_origin = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
    tree, ruler_guides = walk_art(native)
    ab = abs_[0]
    finish(tree, ab, story_list, text_origin)
    convert(tree, ab)
    detect_outlined(tree)
    grads = gradient_defs(native)

    out = ['FILE: %s' % args.file,
           'Coordinates: points from the top-left of artboard "%s" (y down). Artboard size %.3f × %.3f pt.' % (
               ab['name'], ab['width'], ab['height'])]
    if len(abs_) > 1:
        out.append('Other artboards (top-left offset from the first; subtract it to get that artboard\'s own coordinates): ' + ', '.join(
            '%s %g × %g at x %g, y %g' % (a['name'], a['width'], a['height'], round(a['art_x0'] - ab['art_x0'], 2),
                                          round(ab['art_y0'] - a['art_y0'], 2)) for a in abs_[1:]))
    run_fonts = sorted({r['font'] for st_ in story_list for r in st_['runs'] if isinstance(r.get('font'), str)})
    out.append('Fonts in visible text (per Illustrator): ' + ', '.join(fonts_hdr))
    extra = [f for f in run_fonts if f not in fonts_hdr and f not in ('AdobeInvisFont',)]
    if extra:
        out.append('Fonts only in hidden or outlined-backup text: ' + ', '.join(extra))
    gl = []
    for (x0, y0), (x1, y1) in ((g[0], g[1]) for g in ruler_guides if len(g) == 2):
        if abs(x0 - x1) < 0.01:
            gl.append('vertical x=%.2f' % (x0 - ab['art_x0']))
        elif abs(y0 - y1) < 0.01:
            gl.append('horizontal y=%.2f' % (ab['art_y0'] - y0))
    if gl:
        out.append('Ruler guides: ' + ', '.join(gl))
    out.append('')
    outline(tree, out, 0, args.all)
    used = sorted({n['fill']['gradient'] for n in iter_nodes(tree) if isinstance(n.get('fill'), dict) and 'gradient' in n['fill']})
    if used:
        out.append('')
        out.append('GRADIENTS USED:')
        for g in used:
            stops = grads.get(g, [])
            out.append('  "%s": ' % g + ' → '.join(
                ('%s @%g%%' % ('C%g M%g Y%g K%g' % tuple(s['cmyk']) if 'cmyk' in s else s.get('values'), s['location']))
                for s in stops))
    if warnings:
        out.append('')
        out.append('WARNINGS:')
        out += ['  ' + w for w in sorted(set(warnings))]
    print('\n'.join(out))
    if args.json:
        json.dump({'file': args.file, 'artboards': abs_, 'fonts': fonts_hdr, 'layers': strip(tree),
                   'gradients': grads}, open(args.json, 'w'), indent=1)


def iter_nodes(nodes):
    for n in nodes:
        yield n
        yield from iter_nodes(n['children'])


def strip(nodes):
    res = []
    for n in nodes:
        d = {k: v for k, v in n.items() if k not in ('bbox', 'children', 'story', 'frame') and v is not None}
        if n['children']:
            d['children'] = strip(n['children'])
        res.append(d)
    return res


if __name__ == '__main__':
    main()
