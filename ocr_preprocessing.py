"""Bounded, local receipt preprocessing. Uncertain geometry is left intact."""
from __future__ import annotations

import re
from PIL import Image, ImageOps, ImageFilter


def quality_score(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    useful = sum(c.isalnum() or c in '.,:/%€()+-' for c in chars) / len(chars)
    words = re.findall(r'\b[^\W\d_]{3,}\b', text)
    keywords = len(set(re.findall(r'\b(?:rechnung|netto|brutto|mwst|ust|eur)\b', text.lower())))
    dates = bool(re.search(r'\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b', text))
    amounts = bool(re.search(r'\b\d+[.,]\d{2}\b', text))
    return round(25 * min(len(chars) / 600, 1) * useful + 20 * useful
                 + 15 * min(len(words) / 40, 1) + 5 * keywords + 5 * dates + 5 * amounts, 2)


def _line(points):
    """Least squares edge fit, rejecting shadows and non-straight boundaries."""
    if len(points) < 20:
        raise ValueError('Not enough edge evidence')
    xmean = sum(x for x, y in points) / len(points)
    ymean = sum(y for x, y in points) / len(points)
    slope = sum((x-xmean)*(y-ymean) for x, y in points) / sum((x-xmean)**2 for x, y in points)
    offset = ymean - slope*xmean
    if sum(abs(y-slope*x-offset) < 2 for x, y in points) / len(points) < .95:
        raise ValueError('Ambiguous edge')
    return slope, offset


def _projective_coefficients(corners, width, height):
    rows = []
    for (x, y), (u, v) in zip([(0, 0), (width, 0), (width, height), (0, height)], corners):
        rows.extend([[x, y, 1, 0, 0, 0, -u*x, -u*y, u],
                     [0, 0, 0, x, y, 1, -v*x, -v*y, v]])
    for i in range(8):
        pivot = max(range(i, 8), key=lambda j: abs(rows[j][i]))
        rows[i], rows[pivot] = rows[pivot], rows[i]
        divisor = rows[i][i]
        if abs(divisor) < 1e-10:
            raise ValueError('Degenerate perspective')
        rows[i] = [v/divisor for v in rows[i]]
        for j in range(8):
            if j != i:
                factor = rows[j][i]
                rows[j] = [v-factor*r for v, r in zip(rows[j], rows[i])]
    return tuple(row[-1] for row in rows)


def perspective(image):
    from PIL import ImageDraw, ImageChops
    from math import dist
    small = image.convert('L')
    small.thumbnail((320, 320))
    small = small.filter(ImageFilter.MedianFilter(5))
    w, h = small.size
    pixels = small.load()
    border = [pixels[x, y] for x in range(w) for y in (0, h-1)]
    border += [pixels[x, y] for y in range(h) for x in (0, w-1)]
    background = sorted(border)[len(border)//2]
    hist = small.histogram()
    bright = next(i for i in range(256) if sum(hist[:i+1]) >= w*h*.8)
    if bright-background < 40:
        return image, False
    threshold = (bright+background)/2
    mask = small.point(lambda v: 255 if v > threshold else 0)
    box = mask.getbbox()
    if not box:
        return image, False
    left, top, right, bottom = box
    if left < 3 or top < 3 or right > w-3 or bottom > h-3:
        return image, False
    binary = mask.load()
    try:
        ys = range(top+(bottom-top)//4, bottom-(bottom-top)//4)
        xs = range(left+(right-left)//4, right-(right-left)//4)
        horizontal = [[(x, next(y for y in order if binary[x, y])) for x in xs]
                      for order in (range(top, bottom), range(bottom-1, top-1, -1))]
        vertical = [[(y, next(x for x in order if binary[x, y])) for y in ys]
                    for order in (range(left, right), range(right-1, left-1, -1))]
        t, b = map(_line, horizontal)
        l, r = map(_line, vertical)
        def intersection(horizontal, vertical):
            a, c = horizontal
            d, e = vertical
            x = (d*c+e)/(1-d*a)
            return x, a*x+c
        corners = [intersection(t, l), intersection(t, r), intersection(b, r), intersection(b, l)]
        if any(not (2 < x < w-2 and 2 < y < h-2) for x, y in corners):
            return image, False
        expected = Image.new('L', small.size)
        ImageDraw.Draw(expected).polygon(corners, fill=255)
        area = expected.histogram()[255]
        overlap = ImageChops.multiply(expected, mask).histogram()[255]
        union = ImageChops.lighter(expected, mask).histogram()[255]
        if not .4 < area/(w*h) < .94 or overlap/union < .94:
            return image, False
        corners = [(x*image.width/w, y*image.height/h) for x, y in corners]
        width = round(max(dist(corners[0], corners[1]), dist(corners[3], corners[2])))
        height = round(max(dist(corners[0], corners[3]), dist(corners[1], corners[2])))
        coeffs = _projective_coefficients(corners, width, height)
        return image.transform((width, height), Image.Transform.PERSPECTIVE, coeffs,
                               Image.Resampling.BICUBIC), True
    except (ValueError, ZeroDivisionError, StopIteration):
        return image, False


def deskew(image):
    """Correct small tilts only when horizontal line evidence improves clearly."""
    probe = image.copy()
    probe.thumbnail((700, 700))
    probe = probe.point(lambda v: 0 if v < 160 else 255)
    def score(candidate):
        rows = candidate.resize((1, candidate.height), Image.Resampling.BOX).tobytes()
        return sum((a-b)**2 for a, b in zip(rows, rows[1:]))
    baseline = score(probe)
    if baseline == 0:
        return image, 0.0
    candidates = [(score(probe.rotate(angle/2, fillcolor=255)), angle/2)
                  for angle in range(-8, 9)]
    best, angle = max(candidates)
    if angle and abs(angle) < 4 and best > baseline * 1.3:
        return image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255), angle
    return image, 0.0


def prepare(image, orientation):
    info = dict(original_size=list(image.size), rotation=0, exif_rotation=0,
                perspective=False, upscaling=False, deskew=0.0, variant='processed', quality_score=0.0, warnings=[])
    original = image.copy()
    try:
        info['exif_rotation'] = {3: 180, 6: 90, 8: 270}.get(image.getexif().get(274), 0)
        original = ImageOps.exif_transpose(image).convert('RGB')
        processed = original.copy()
        # Bound memory and CPU cost for very large photographs.
        processed.thumbnail((4000, 4000), Image.Resampling.LANCZOS)
        try:
            processed, info['perspective'] = perspective(processed)
        except Exception as exc:
            info['warnings'].append(f'Perspektivkorrektur ausgelassen: {exc}')
        processed = ImageOps.grayscale(processed)
        processed = ImageOps.autocontrast(processed, cutoff=1)
        rotation, confidence = orientation(processed)
        if rotation in (90, 180, 270) and confidence >= 15:
            processed = processed.rotate(-rotation, expand=True, fillcolor=255)
            info['rotation'] = rotation
        processed, info['deskew'] = deskew(processed)
        if max(processed.size) < 1800:
            scale = min(3, 1800 / max(processed.size))
            processed = processed.resize(tuple(round(v*scale) for v in processed.size), Image.Resampling.LANCZOS)
            info['upscaling'] = True
        processed = processed.filter(ImageFilter.MedianFilter(3))
        processed = processed.filter(ImageFilter.UnsharpMask(radius=1, percent=110, threshold=3))
        return original, processed, info
    except Exception as exc:
        info['warnings'].append(f'Vorverarbeitung fehlgeschlagen; Original verwendet: {exc}')
        info.update(perspective=False, upscaling=False, rotation=0, variant='original')
        return original, original, info


def alternate(image):
    from PIL import ImageChops
    gray = image.convert('L')
    local_mean = gray.filter(ImageFilter.BoxBlur(20))
    # Local mean threshold handles uneven illumination without extra native libraries.
    difference = ImageChops.subtract(gray, local_mean, offset=128)
    return difference.point(lambda value: 255 if value > 113 else 0)
