"""Colour blindness, simulated, for the palette tests: how far apart two colours look to someone with
deuteranopia, protanopia or tritanopia (or normal vision).

The model and the numbers are the usual ones for checking data colours: the Machado, Oliveira &
Fernandes (2009) simulation at full severity (the worst case: most colour-blind people see more
colour than this), in linear sRGB, and the distance between colours in OKLab x 100 (Delta E OK).
About 8 is a clear difference; under 6, two colours are easily taken for each other.
"""
import math

MACHADO = {
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "tritan": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}
KINDS = ("deutan", "protan", "tritan")


def _rgb(hex_code: str):
    h = hex_code.lstrip("#")[:6]
    return [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]


def _linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _gamma(v: float) -> float:
    v = min(1.0, max(0.0, v))
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def seen(hex_code: str, kind: str = None):
    """The colour (r, g, b in 0..1) as seen with `kind` of colour blindness (None: normal vision)."""
    rgb = _rgb(hex_code)
    if kind is None:
        return rgb
    lin = [_linear(v) for v in rgb]
    return [_gamma(sum(m * c for m, c in zip(row, lin))) for row in MACHADO[kind]]


def oklab(rgb):
    r, g, b = (_linear(v) for v in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def delta_e(a: str, b: str, kind: str = None) -> float:
    """How different two colours look (Delta E OK x 100) to someone with `kind` (None: normal)."""
    return 100 * math.dist(oklab(seen(a, kind)), oklab(seen(b, kind)))


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio (1 to 21)."""
    def lum(h):
        r, g, b_ = (_linear(v) for v in _rgb(h))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def closest_pair(colours: dict, kind: str = None, against: dict = None):
    """(Delta E, name, name) of the two colours that look most alike - among `colours`, and between
    them and `against` (greys, say: not compared with each other)."""
    names = list(colours.items())
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
    pairs += [(a, b) for a in names for b in (against or {}).items()]
    return min((delta_e(ha, hb, kind), na, nb) for (na, ha), (nb, hb) in pairs)
