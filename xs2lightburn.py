#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xs2lightburn 2.2
Converts xTool Studio (.xs, .xcs) and RDWorks (.rld) projects to LightBurn projects (.lbrn2).
Konwerter projektów xTool Studio (.xs, .xcs) i RDWorks (.rld) do projektów LightBurn (.lbrn2).

Transfers geometry (paths, circles, rectangles, components as groups), bitmaps,
layers (colors) and operations (cut, line engrave, fill, image engrave) with power,
speed, passes, line interval and dithering.

Run without arguments to open the window. Console mode:
    python3 xs2lightburn.py file.xs [file2.xcs file3.rld ...] [-o folder] [--mirror-x] [--mirror-y]
    python3 xs2lightburn.py --help

MIT License. https://github.com/bartwaw/XS_to_LBRN
"""

import base64
import json
import math
import os
import re
import struct
import sys
import threading
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

VERSION = "2.2"
MAX_ENTRY_BYTES = 100 * 1024 * 1024   # zabezpieczenie przed archiwum-bomba
MAX_TOTAL_BYTES = 1024 * 1024 * 1024

CONFIG_PATH = Path.home() / ".xs2lightburn.json"


def _locale_dirs():
    """Foldery z plikami jezykow: obok programu (.py albo .exe) i w paczce PyInstaller."""
    dirs = [Path(__file__).resolve().parent / "locales"]
    if getattr(sys, "frozen", False):
        dirs.insert(0, Path(sys.executable).resolve().parent / "locales")
        if getattr(sys, "_MEIPASS", None):
            dirs.append(Path(sys._MEIPASS) / "locales")
    return dirs


def _has_json(d):
    try:
        return any(d.glob("*.json"))
    except OSError:
        return False


LOCALES_DIR = next((d for d in _locale_dirs() if _has_json(d)), _locale_dirs()[0])
SOURCE_LANG = ("pl", "Polski")  # teksty w kodzie sa po polsku i sa kluczami tlumaczen
# znaczniki formatowania jak w Pythonie: %s, %d, %02d, %.1f, %% (takze "% d" z flaga spacji)
_SPEC = re.compile(r"%(?:%|\([^)]*\)|[-+ #0]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[diouxXeEfFgGcrsa])")
_CODE_RE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?")
LANG = SOURCE_LANG[0]
LOCALE_WARNINGS = []  # problemy z plikami jezykow (pokazywane w logu i w konsoli)
_TR = {}
_FALLBACK = {}
_CACHE = {}


def _(text):
    """Tlumaczenie z locales/<kod>.json; brakujace teksty: angielski, potem polski."""
    return _TR.get(text) or _FALLBACK.get(text) or text


def _warn_locale(msg):
    if msg not in LOCALE_WARNINGS:
        LOCALE_WARNINGS.append(msg)


def _read_locale_file(path):
    """JSON pliku jezyka albo None (problem trafia do LOCALE_WARNINGS)."""
    try:
        with open(str(path), encoding="utf-8-sig") as fh:  # -sig: plik z BOM z Notatnika
            raw = json.load(fh)
    except ValueError as e:
        _warn_locale("%s: %s" % (path.name, e))
        return None
    except OSError as e:
        _warn_locale("%s: %s" % (path.name, e.strerror or e))
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("strings"), dict):
        _warn_locale("%s: missing \"strings\" object" % path.name)
        return None
    return raw


def _valid_translation(key, value):
    """Czy tlumaczenie da sie sformatowac tak jak klucz (te same znaczniki %, w tej samej kolejnosci)."""
    spec = _SPEC.findall(key)
    if not spec:
        return True  # tekst bez formatowania: znak % w tlumaczeniu jest zwyklym znakiem
    if _SPEC.findall(value) != spec:
        return False
    args = tuple("x" if m[-1] in "srac" else 1 for m in spec if m != "%%")
    try:
        value % args
    except Exception:
        return False
    return True


def load_locale(code):
    """Slownik tekstow z locales/<code>.json. Pomija wpisy puste i takie, ktorych
    znaczniki % nie zgadzaja sie z kluczem (blad w tlumaczeniu nie wywroci programu)."""
    key = (str(LOCALES_DIR), code)
    if key in _CACHE:
        return _CACHE[key]
    path = LOCALES_DIR / ("%s.json" % code)
    raw = _read_locale_file(path) if path.is_file() else None
    out = {}
    bad = 0
    for k, v in ((raw or {}).get("strings") or {}).items():
        if isinstance(v, str) and v and _valid_translation(k, v):
            out[k] = v
        elif v != "":
            bad += 1
    if bad:
        _warn_locale("%s: ignored %d %s (placeholders such as %%s or %%d differ from the key, "
                     "or the value is not text)" % (path.name, bad, "entry" if bad == 1 else "entries"))
    _CACHE[key] = out
    return out


def available_languages():
    """[(kod, nazwa)]: polski (wbudowany) i kazdy poprawny plik locales/<kod>.json."""
    langs = [SOURCE_LANG]
    names = {SOURCE_LANG[1]}
    try:
        files = sorted(LOCALES_DIR.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        code = f.stem
        if code == SOURCE_LANG[0]:
            continue
        if not _CODE_RE.fullmatch(code):
            _warn_locale("%s: bad file name, use e.g. de.json or pt_BR.json" % f.name)
            continue
        raw = _read_locale_file(f)
        if raw is None:
            continue
        name = raw.get("language")
        name = name.strip() if isinstance(name, str) and name.strip() else code
        if name in names:
            name = "%s (%s)" % (name, code)
        names.add(name)
        langs.append((code, name))
    if len(langs) == 1:
        if files:
            _warn_locale("no usable language file in %s; the interface is Polish only." % LOCALES_DIR)
        else:
            _warn_locale("locales folder not found or empty (%s); the interface is Polish only. "
                         "Download the whole repository, including the locales folder." % LOCALES_DIR)
    return langs


def match_lang(code, codes):
    """Najlepszy dostepny kod: dokladny, bez regionu, ten sam jezyk z regionem, angielski."""
    code = str(code or "").replace("-", "_")
    if code in codes:
        return code
    short = code.split("_")[0].lower()
    if short in codes:
        return short
    for c in codes:
        if short and c.split("_")[0] == short:
            return c
    return "en" if "en" in codes else SOURCE_LANG[0]


def set_lang(code):
    """Ustawia jezyk (kod nieznany: najblizszy dostepny); zwraca uzyty kod."""
    global LANG, _TR, _FALLBACK
    _CACHE.clear()  # plik jezyka poprawiony przy otwartym oknie zadziala po zmianie jezyka
    code = match_lang(code, [c for c, _n in available_languages()])
    LANG = code
    _TR = {} if code == SOURCE_LANG[0] else load_locale(code)
    _FALLBACK = {} if code in (SOURCE_LANG[0], "en") else load_locale("en")
    return code


set_lang("en")


def system_lang():
    """Kod jezyka systemu (np. 'pl', 'pt_BR') albo '' gdy nieznany."""
    import locale
    if sys.platform == "win32":
        try:
            import ctypes
            name = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
            if name:
                return name
        except Exception:
            pass
    names = [os.environ.get(k, "") for k in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        names.append(locale.getlocale()[0] or "")
    except Exception:
        pass
    for n in names:  # pierwsza ustawiona zmienna decyduje
        n = n.split(":")[0].split(".")[0]
        if not n or n.upper() in ("C", "POSIX"):
            continue
        if n.lower().startswith("polish"):
            return "pl"
        n = locale.normalize(n.lower()).split(".")[0]
        if n.upper() in ("C", "POSIX"):
            continue
        parts = n.split("_")
        return parts[0].lower() + ("_" + parts[1].upper() if len(parts) > 1 and parts[1] else "")
    return ""


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return {}
        if "lightburn_secret" in cfg:  # wersje rozwojowe trzymaly tu sekret: usuwamy od razu
            cfg.pop("lightburn_secret", None)
            save_config(cfg)
        return cfg
    except Exception:
        return {}


def note_text(note):
    """Notatka bazy w biezacym jezyku (standardowa notatka w dowolnym jezyku jest tlumaczona)."""
    if note == DB_NOTE:
        return _(DB_NOTE)
    for code, _n in available_languages()[1:]:
        if note == load_locale(code).get(DB_NOTE):
            return _(DB_NOTE)
    return note


def mirror_settings(cfg):
    """Ustawienia lustra urzadzenia LightBurn (MirrorX, MirrorY) z konfiguracji.
    Migracja ze starych kluczy flip_x/flip_y (wersje do 1.5)."""
    if "mirror_x" in cfg or "mirror_y" in cfg:
        return bool(cfg.get("mirror_x", False)), bool(cfg.get("mirror_y", False))
    if "flip_x" in cfg or "flip_y" in cfg:
        return bool(cfg.get("flip_x", True)), not bool(cfg.get("flip_y", False))
    return False, False


def mirror_to_flip(mx, my):
    """MirrorX -> odbicie X wzgledem xTool; brak MirrorY -> odbicie Y (os Y w gore)."""
    return bool(mx), not bool(my)


_FILE_LOCK = threading.RLock()  # zapisy konfiguracji i sekretu z kilku watkow


def _write_private(path, text, private=False):
    """Zapis atomowy: unikalny plik tymczasowy obok celu i zamiana. private: prawa 0600
    od poczatku (POSIX); inaczej zwykle prawa jak przy nowym pliku."""
    import tempfile
    import time
    path = Path(os.path.realpath(str(path)))  # dowiazanie (np. dotfiles) zostaje dowiazaniem
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if not private:
            # prawa istniejacego pliku albo zwykle 0644 (os.umask jest wspolny dla watkow: nie ruszamy)
            try:
                mode = os.stat(str(path)).st_mode & 0o777
            except OSError:
                mode = 0o644
            os.chmod(tmp, mode)
        for attempt in range(4):
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError:
                # Windows: cel chwilowo otwarty (antywirus, indeksowanie, druga kopia programu)
                if os.name != "nt" or attempt == 3:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(cfg):
    with _FILE_LOCK:
        try:
            _write_private(CONFIG_PATH, json.dumps(dict(cfg), indent=2))
        except Exception:
            pass


def fmt(v):
    """Liczba do pliku LightBurn: bez notacji wykladniczej, bez zbednych zer."""
    if not math.isfinite(v):
        v = 0.0
    s = ("%.5f" % v).rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def jmap(obj):
    """Mapa xTool {'dataType':'Map','value':[[k,v],...]} lub zwykly dict -> dict."""
    if isinstance(obj, dict) and obj.get("dataType") == "Map":
        return {k: v for k, v in obj.get("value", [])}
    if isinstance(obj, dict):
        return obj
    return {}


def mat_mul(m, n):
    """Zlozenie: najpierw n, potem m (afiniczne [a,b,c,d,e,f])."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return [
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    ]


def apply_mat(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


# ----------------------------------------------------------------------------
# Parser sciezek SVG -> podsciezki z krzywymi Beziera
# ----------------------------------------------------------------------------

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM)


class SubPath:
    __slots__ = ("pts", "segs", "closed")

    def __init__(self, start):
        self.pts = [start]
        # segs[i]: ('L',None,None) albo ('C',c1,c2) - odcinek pts[i] -> pts[i+1]
        self.segs = []
        self.closed = False


def _arc_to_cubics(x1, y1, rx, ry, phi_deg, fa, fs, x2, y2):
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [((x1, y1), (x2, y2), (x2, y2))]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2.0
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2.0

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry,
              (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dth > 0:
        dth -= 2 * math.pi
    elif fs and dth < 0:
        dth += 2 * math.pi
    n = max(1, int(math.ceil(abs(dth) / (math.pi / 2) - 1e-9)))
    step = dth / n
    out = []
    t = 4.0 / 3.0 * math.tan(step / 4.0)
    a0 = th1
    for _ in range(n):
        a1 = a0 + step
        c0, s0 = math.cos(a0), math.sin(a0)
        c1_, s1 = math.cos(a1), math.sin(a1)
        p1 = (c0 - t * s0, s0 + t * c0)
        p2 = (c1_ + t * s1, s1 - t * c1_)
        p3 = (c1_, s1)

        def tr(p):
            x = rx * p[0]
            y = ry * p[1]
            return (cp * x - sp * y + cx, sp * x + cp * y + cy)

        out.append((tr(p1), tr(p2), tr(p3)))
        a0 = a1
    return out


def parse_svg_path(d, log=None, ident=""):
    """Zwraca liste SubPath (wspolrzedne bezwzgledne)."""
    toks = _TOKEN.findall(d or "")
    i = 0
    n = len(toks)
    subs = []
    cur = None
    cx = cy = 0.0
    sx = sy = 0.0
    cmd = None
    last_c2 = None   # do S/s
    last_q = None    # do T/t
    need_new = False
    broken = False

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    def is_cmd(t):
        return t.isalpha() and len(t) == 1

    def flag():
        # flagi luku moga byc sklejone ("A5 5 0 1110 0"): bierzemy jeden znak
        nonlocal i
        t = toks[i]
        if t[0] not in "01":
            raise ValueError(_("zla flaga luku"))
        if len(t) > 1 and t[1] not in "eE":
            toks[i] = t[1:]
        else:
            if t not in ("0", "1"):
                raise ValueError(_("zla flaga luku"))
            i += 1
        return int(t[0])

    def ensure():
        nonlocal cur, need_new
        if cur is None or need_new:
            cur = SubPath((cx, cy))
            subs.append(cur)
            need_new = False

    while i < n:
        t = toks[i]
        if is_cmd(t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur is not None and not need_new:
                    sp = cur
                    if len(sp.pts) > 1:
                        if (abs(sp.pts[-1][0] - sp.pts[0][0]) < 1e-9 and
                                abs(sp.pts[-1][1] - sp.pts[0][1]) < 1e-9):
                            sp.pts.pop()
                        else:
                            sp.segs.append(("L", None, None))
                        sp.closed = True
                    cx, cy = sx, sy
                    need_new = True
                last_c2 = last_q = None
                continue
            if cmd in "Mm" and i < n and not is_cmd(toks[i]):
                if i + 1 >= n or is_cmd(toks[i + 1]):
                    broken = True
                    break
                x = num()
                y = num()
                if cmd == "m":
                    x += cx
                    y += cy
                cx, cy = x, y
                sx, sy = x, y
                cur = SubPath((cx, cy))
                subs.append(cur)
                need_new = False
                cmd = "L" if cmd == "M" else "l"
                last_c2 = last_q = None
                continue
            continue
        if cmd is None:
            i += 1
            continue
        rel = cmd.islower()
        C = cmd.upper()
        try:
            if C == "L":
                x, y = num(), num()
                if rel:
                    x += cx
                    y += cy
                ensure()
                cur.segs.append(("L", None, None))
                cur.pts.append((x, y))
                cx, cy = x, y
                last_c2 = last_q = None
            elif C == "H":
                x = num()
                if rel:
                    x += cx
                ensure()
                cur.segs.append(("L", None, None))
                cur.pts.append((x, cy))
                cx = x
                last_c2 = last_q = None
            elif C == "V":
                y = num()
                if rel:
                    y += cy
                ensure()
                cur.segs.append(("L", None, None))
                cur.pts.append((cx, y))
                cy = y
                last_c2 = last_q = None
            elif C == "C":
                v = [num() for _ in range(6)]
                if rel:
                    v = [v[0] + cx, v[1] + cy, v[2] + cx, v[3] + cy,
                         v[4] + cx, v[5] + cy]
                ensure()
                cur.segs.append(("C", (v[0], v[1]), (v[2], v[3])))
                cur.pts.append((v[4], v[5]))
                last_c2 = (v[2], v[3])
                last_q = None
                cx, cy = v[4], v[5]
            elif C == "S":
                v = [num() for _ in range(4)]
                if rel:
                    v = [v[0] + cx, v[1] + cy, v[2] + cx, v[3] + cy]
                if last_c2 is not None:
                    c1 = (2 * cx - last_c2[0], 2 * cy - last_c2[1])
                else:
                    c1 = (cx, cy)
                ensure()
                cur.segs.append(("C", c1, (v[0], v[1])))
                cur.pts.append((v[2], v[3]))
                last_c2 = (v[0], v[1])
                last_q = None
                cx, cy = v[2], v[3]
            elif C == "Q":
                v = [num() for _ in range(4)]
                if rel:
                    v = [v[0] + cx, v[1] + cy, v[2] + cx, v[3] + cy]
                q = (v[0], v[1])
                ensure()
                c1 = (cx + 2.0 / 3.0 * (q[0] - cx), cy + 2.0 / 3.0 * (q[1] - cy))
                c2 = (v[2] + 2.0 / 3.0 * (q[0] - v[2]),
                      v[3] + 2.0 / 3.0 * (q[1] - v[3]))
                cur.segs.append(("C", c1, c2))
                cur.pts.append((v[2], v[3]))
                last_q = q
                last_c2 = None
                cx, cy = v[2], v[3]
            elif C == "T":
                x, y = num(), num()
                if rel:
                    x += cx
                    y += cy
                if last_q is not None:
                    q = (2 * cx - last_q[0], 2 * cy - last_q[1])
                else:
                    q = (cx, cy)
                ensure()
                c1 = (cx + 2.0 / 3.0 * (q[0] - cx), cy + 2.0 / 3.0 * (q[1] - cy))
                c2 = (x + 2.0 / 3.0 * (q[0] - x), y + 2.0 / 3.0 * (q[1] - y))
                cur.segs.append(("C", c1, c2))
                cur.pts.append((x, y))
                last_q = q
                last_c2 = None
                cx, cy = x, y
            elif C == "A":
                rx, ry, rot = num(), num(), num()
                fa, fs = flag(), flag()
                x, y = num(), num()
                if rel:
                    x += cx
                    y += cy
                ensure()
                for c1, c2, p in _arc_to_cubics(cx, cy, rx, ry, rot, fa, fs, x, y):
                    cur.segs.append(("C", c1, c2))
                    cur.pts.append(p)
                cx, cy = x, y
                last_c2 = last_q = None
            else:
                i += 1
        except (IndexError, ValueError):
            broken = True
            break
    if broken and log:
        log(_("  UWAGA: ścieżka %s ma błąd w danych, przeniesiono tylko jej poprawną część") % ident)
    return [s for s in subs if len(s.pts) > 1]


def transform_subpaths(subs, m):
    out = []
    for sp in subs:
        n = SubPath(apply_mat(m, *sp.pts[0]))
        n.pts = [apply_mat(m, *p) for p in sp.pts]
        n.segs = []
        for kind, c1, c2 in sp.segs:
            if kind == "C":
                n.segs.append(("C", apply_mat(m, *c1), apply_mat(m, *c2)))
            else:
                n.segs.append(("L", None, None))
        n.closed = sp.closed
        out.append(n)
    return out


# ----------------------------------------------------------------------------
# Model projektu
# ----------------------------------------------------------------------------


class Item:
    def __init__(self):
        self.kind = None        # 'path' | 'bitmap'
        self.id = ""
        self.layer = "#000000"  # kolor warstwy xTool
        self.matrix = [1, 0, 0, 1, 0, 0]
        self.subpaths = []      # dla path (wspolrzedne lokalne, przed macierza)
        self.png = b""          # dla bitmap
        self.ow = 0
        self.oh = 0
        self.ptype = None       # processingType
        self.params = {}
        self.negative = False
        self.zorder = 0
        self.group = None       # id komponentu xTool (grupa w LightBurn)
        self.off = False        # w xTool wylaczony z obrobki
        self.rd_spec = None     # RDWorks: gotowe parametry warstwy LightBurn


class Canvas:
    def __init__(self, name):
        raw = str(name)
        self.name = raw.replace("{panel}", _("Plansza ")).strip()
        self.file_name = raw.replace("{panel}", "Canvas ").strip()  # niezalezne od jezyka
        self.machine = ""
        self.layers = {}   # kolor -> {'name','order','visible'}
        self.items = []


def png_size(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        return struct.unpack(">II", b[16:24])
    return None


def decode_data_uri(s):
    if not isinstance(s, str) or "," not in s:
        return None
    head, data = s.split(",", 1)
    if not head.startswith("data:image"):
        return None
    try:
        return base64.b64decode(data)
    except Exception:
        return None


def ensure_png_or_jpg(b, log):
    if b[:8] == b"\x89PNG\r\n\x1a\n" or b[:3] == b"\xff\xd8\xff":
        return b
    try:
        from PIL import Image
        import io
    except ImportError:
        log(_("  UWAGA: obraz w formacie innym niż PNG/JPG, a brak biblioteki Pillow "
            "(pip install pillow); obraz pominięty"))
        return None
    try:
        im = Image.open(io.BytesIO(b))
        if im.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
            im = im.convert("RGBA")
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        log(_("  UWAGA: uszkodzony lub nieczytelny obraz; pominięty"))
        return None


def _pick_params(dv_data, ptype):
    """xcs: dv_data = {ptype: {materialType, parameter:{official|customize:{...}}}}"""
    block = dv_data.get(ptype) if isinstance(dv_data, dict) else None
    if not isinstance(block, dict):
        return {}
    par = block.get("parameter", {})
    if not isinstance(par, dict):
        return {}
    mt = block.get("materialType")
    if mt in par and isinstance(par[mt], dict):
        return dict(par[mt])
    for v in par.values():
        if isinstance(v, dict):
            return dict(v)
    return {}


# --- ksztalty proste xTool (bez dPath) --------------------------------------

_K = 0.5522847498307936


def _ellipse_subpath(cx, cy, rx, ry):
    sp = SubPath((cx + rx, cy))
    pts = [(cx, cy + ry), (cx - rx, cy), (cx, cy - ry), (cx + rx, cy)]
    ctr = [((cx + rx, cy + ry * _K), (cx + rx * _K, cy + ry)),
           ((cx - rx * _K, cy + ry), (cx - rx, cy + ry * _K)),
           ((cx - rx, cy - ry * _K), (cx - rx * _K, cy - ry)),
           ((cx + rx * _K, cy - ry), (cx + rx, cy - ry * _K))]
    for p, (c1, c2) in zip(pts, ctr):
        sp.segs.append(("C", c1, c2))
        sp.pts.append(p)
    sp.pts.pop()          # ostatni punkt = pierwszy
    sp.closed = True
    return sp


def _rect_subpath(w, h, r):
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    if r <= 1e-9:
        sp = SubPath((0.0, 0.0))
        for p in ((w, 0.0), (w, h), (0.0, h)):
            sp.segs.append(("L", None, None))
            sp.pts.append(p)
        sp.segs.append(("L", None, None))
        sp.closed = True
        return sp
    k = r * (1 - _K)
    sp = SubPath((r, 0.0))
    seq = [
        ("L", (w - r, 0.0)), ("C", ((w - k, 0.0), (w, k)), (w, r)),
        ("L", (w, h - r)), ("C", ((w, h - k), (w - k, h)), (w - r, h)),
        ("L", (r, h)), ("C", ((k, h), (0.0, h - k)), (0.0, h - r)),
        ("L", (0.0, r)), ("C", ((0.0, k), (k, 0.0)), (r, 0.0)),
    ]
    for item in seq:
        if item[0] == "L":
            sp.segs.append(("L", None, None))
            sp.pts.append(item[1])
        else:
            (c1, c2), p = item[1], item[2]
            sp.segs.append(("C", c1, c2))
            sp.pts.append(p)
    sp.pts.pop()
    sp.closed = True
    return sp


def primitive_subpaths(d, matrix):
    """CIRCLE/ELLIPSE/RECT bez dPath: geometria w ramce lokalnej 0..w, 0..h."""
    t = d.get("type")
    a, b, c, dd = matrix[:4]
    sx = math.hypot(a, b) or 1.0
    sy = math.hypot(c, dd) or 1.0
    try:
        w = float(d.get("width")) / sx
        h = float(d.get("height")) / sy
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if t in ("CIRCLE", "ELLIPSE"):
        return [_ellipse_subpath(w / 2.0, h / 2.0, w / 2.0, h / 2.0)]
    if t == "RECT":
        try:
            r = float(d.get("radius") or 0)
        except (TypeError, ValueError):
            r = 0.0
        return [_rect_subpath(w, h, r)]
    return None


# --- wspolna obrobka elementu (xcs i xs) -------------------------------------

def expand_displays(displays, resolve_ref, log, group=None, depth=0):
    """Rozwija komponenty xTool (COMPONENT) do elementow koncowych.
    Zwraca liste (display, id_grupy)."""
    out = []
    for d in displays:
        if not isinstance(d, dict):
            continue
        if d.get("type") == "COMPONENT":
            if d.get("visible") is False or d.get("visibleState") is False:
                log(_("  pominięto (ukryty komponent): %s") % str(d.get("id", ""))[:8])
                continue
            kids = d.get("flattenDisplayJSON")
            if kids is None and d.get("flattenDisplayJSONRef"):
                kids = resolve_ref(d.get("flattenDisplayJSONRef"))
            if not isinstance(kids, list) or depth > 20:
                log(_("  UWAGA: komponent %s bez elementów, pominięty")
                    % str(d.get("id", ""))[:8])
                continue
            out.extend(expand_displays(kids, resolve_ref, log,
                                       group or d.get("id"), depth + 1))
        else:
            out.append((d, group))
    return out


def make_item(d, group, log, get_dpath, get_image):
    it = Item()
    it.id = str(d.get("id", ""))
    it.group = group
    z = d.get("zOrder", 0)
    it.zorder = z if isinstance(z, (int, float)) else 0
    it.layer = str(d.get("layerTag") or d.get("layerColor") or "#000000").lower()
    if d.get("visible") is False or d.get("visibleState") is False:
        log(_("  pominięto (ukryty element): %s") % it.id[:8])
        return None
    tr = d.get("transform")
    try:
        if isinstance(tr, list) and len(tr) == 6:
            it.matrix = [float(v) for v in tr]
        else:
            raise ValueError
    except (TypeError, ValueError):
        log(_("  UWAGA: element %s bez położenia/skali, przyjęto domyślne") % it.id[:8])
        it.matrix = [1, 0, 0, 1, 0, 0]
    t = d.get("type")
    is_img = t == "BITMAP" or ("base64" in d or "resourcePath" in d) and "dPath" not in d
    if not is_img:
        dpath = get_dpath(d)
        if dpath:
            it.subpaths = parse_svg_path(dpath, log, it.id[:8])
        else:
            it.subpaths = primitive_subpaths(d, it.matrix) or []
        if not it.subpaths:
            if dpath is None and t not in ("CIRCLE", "ELLIPSE", "RECT"):
                log(_("  UWAGA: nieobsługiwany typ %s (%s), pominięty (np. tekst: zamień w xTool na ścieżkę)") % (t, it.id[:8]))
            else:
                log(_("  UWAGA: pusty kształt %s (%s), pominięty") % (t, it.id[:8]))
            return None
        it.kind = "path"
        return it
    it.kind = "bitmap"
    png = get_image(d)
    if not png:
        log(_("  UWAGA: brak danych obrazu %s, pominięty") % it.id[:8])
        return None
    it.png = png
    try:
        it.ow = int(d.get("originWidth") or 0)
        it.oh = int(d.get("originHeight") or 0)
    except (TypeError, ValueError):
        it.ow = it.oh = 0
    it.negative = bool(d.get("colorInverted"))
    adj = []
    defaults = {"sharpness": (0, 50)}
    for k in ("brightness", "contrast", "sharpness", "saturation", "temperature", "tone"):
        v = d.get(k)
        if isinstance(v, (int, float)) and v not in defaults.get(k, (0,)):
            adj.append("%s=%s" % (k, v))
    gv = d.get("grayValue")
    if isinstance(gv, list) and gv not in ([0, 255], []):
        adj.append("grayValue=%s" % gv)
    if d.get("filterList") or d.get("filterList_V2") or d.get("mask"):
        adj.append(_("filtry/maska"))
    if adj:
        log(_("  UWAGA: obraz %s ma korekty xTool (%s); przeniesiono obraz bez nich")
            % (it.id[:8], ", ".join(adj)))
    return it


# --- czytnik .xcs -------------------------------------------------------------

def machine_label(dev):
    if not isinstance(dev, dict):
        return ""
    name = str(dev.get("extName") or dev.get("extId") or dev.get("deviceCode") or "")
    pw = dev.get("power")
    if isinstance(pw, list) and pw and isinstance(pw[0], (int, float)):
        name += " %s W" % fmt(float(pw[0]))
    return name.strip()


def read_xcs(path, log):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    dev = data.get("device") or {}
    dmap = jmap(dev.get("data"))
    canvases = []

    def get_dpath(d):
        return d.get("dPath")

    def get_image(d):
        return decode_data_uri(d.get("base64"))

    for ci, cv in enumerate(data.get("canvas") or []):
        c = Canvas(cv.get("title") or ("{panel}%d" % (ci + 1)))
        c.machine = machine_label(dev)
        for color, ld in (cv.get("layerData") or {}).items():
            c.layers[color.lower()] = ld if isinstance(ld, dict) else {}
        entry = dmap.get(cv.get("id")) or {}
        mode = entry.get("mode")
        if mode and mode != "LASER_PLANE":
            log(_("  UWAGA: tryb obróbki %s (nie płaski); sprawdź projekt ręcznie") % mode)
        per_display = jmap(entry.get("displays"))
        for d, grp in expand_displays(cv.get("displays") or [], lambda r: None, log):
            it = make_item(d, grp, log, get_dpath, get_image)
            if it is None:
                continue
            dv = per_display.get(it.id)
            if dv:
                if dv.get("processIgnore"):
                    it.off = True
                it.ptype = dv.get("processingType")
                it.params = _pick_params(dv.get("data"), it.ptype)
            else:
                it.ptype = d.get("processingType")
                it.params = _pick_params(d.get("data"), it.ptype)
            c.items.append(it)
        canvases.append(c)
    return canvases


# --- czytnik .xs (ZIP) --------------------------------------------------------

class ZipReader:
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        total = 0
        for zi in self.z.infolist():
            total += zi.file_size
            if zi.file_size > MAX_ENTRY_BYTES or total > MAX_TOTAL_BYTES:
                self.z.close()
                raise ValueError(_("Podejrzanie duże dane w archiwum: ") + zi.filename)
        self.names = set(self.z.namelist())

    def read(self, name):
        return self.z.read(name)

    def json(self, name):
        return json.loads(self.z.read(name).decode("utf-8"))

    def has(self, name):
        return name in self.names

    def glob(self, prefix, suffix):
        return sorted(n for n in self.names
                      if n.startswith(prefix) and n.endswith(suffix))

    def close(self):
        self.z.close()


def read_xs(path, log):
    z = ZipReader(path)
    try:
        return _read_xs(z, log)
    finally:
        z.close()


def _read_xs(z, log):
    proj = z.json("project.json")
    canvas_ids = (proj.get("modules") or {}).get("canvases") or []
    if not canvas_ids:
        canvas_ids = sorted({n.split("/")[1][:-5] for n in z.names
                             if n.startswith("canvases/") and n.endswith(".json")
                             and n.count("/") == 1})
    profiles = {}
    if z.has("profiles.json"):
        profiles = z.json("profiles.json").get("profiles") or {}
    dev_id = proj.get("activeDeviceId")
    dev = None
    if dev_id and z.has("devices/device-%s.json" % dev_id):
        dev = z.json("devices/device-%s.json" % dev_id)
    else:
        for n in z.glob("devices/", ".json"):
            dev = z.json(n)
            break
    vec = {}

    def vector(bucket, h):
        if bucket not in vec:
            m = {}
            for n in z.glob("vectors/%s/data-" % bucket, ".json"):
                m.update(z.json(n).get("entries") or {})
            vec[bucket] = m
        return vec[bucket].get(h)

    def get_dpath(d):
        if d.get("dPath"):
            return d["dPath"]
        ref = d.get("vectorRef")
        if isinstance(ref, dict):
            return vector(str(ref.get("bucketType", "svg")), ref.get("vectorHash"))
        return None

    def get_image(d):
        rp = d.get("resourcePath")
        if isinstance(rp, str) and z.has(rp):
            return z.read(rp)
        if d.get("base64"):
            return decode_data_uri(d["base64"])
        return None

    def resolve_ref(ref):
        if isinstance(ref, str) and z.has(ref):
            try:
                return z.json(ref)
            except Exception:
                return None
        return None

    canvases = []
    for cid in canvas_ids:
        if not z.has("canvases/%s.json" % cid):
            log(_("  UWAGA: brak opisu planszy %s, pominięta") % str(cid)[:8])
            continue
        meta = z.json("canvases/%s.json" % cid)
        c = Canvas(meta.get("title") or str(cid)[:8])
        c.machine = machine_label(dev)
        for color, ld in (meta.get("layerData") or {}).items():
            c.layers[color.lower()] = ld if isinstance(ld, dict) else {}
        chunks = (meta.get("chunkLayout") or {}).get("chunkIndexes")
        if isinstance(chunks, list):
            files = ["canvases/%s/displays-%s.json" % (cid, k) for k in chunks]
        else:
            files = z.glob("canvases/%s/displays-" % cid, ".json")
        displays = []
        for f in files:
            if z.has(f):
                displays.extend(z.json(f).get("displays") or [])
            else:
                log(_("  UWAGA: w pliku brakuje fragmentu %s") % f)
        ops = {}
        proc = ((dev or {}).get("processing") or {}).get(cid) or {}
        mode_name = proc.get("activeMode")
        mode = (proc.get("modes") or {}).get(mode_name) or {}
        if mode_name and mode_name != "LASER_PLANE":
            log(_("  UWAGA: tryb obróbki %s (nie płaski); sprawdź projekt ręcznie") % mode_name)
        ignored = set(mode.get("ignoredDisplayIds") or [])
        patches = mode.get("patches") or {}
        for b in mode.get("bindings") or []:
            prof = profiles.get(b.get("baseProfileId")) or {}
            params = dict(prof.get("values") or {})
            for pid in b.get("patchIds") or []:
                params.update((patches.get(pid) or {}).get("overrides") or {})
            for did in b.get("displayIds") or []:
                ops[did] = (prof.get("processingType"), params)
        for d, grp in expand_displays(displays, resolve_ref, log):
            it = make_item(d, grp, log, get_dpath, get_image)
            if it is None:
                continue
            if it.id in ignored:
                it.off = True
            if it.id in ops:
                it.ptype, it.params = ops[it.id]
            c.items.append(it)
        canvases.append(c)
    return canvases


# --- czytnik RDWorks (.rld) ----------------------------------------------------
#
# Format RDWorks 8 rozpracowany na plikach testowych (brak oficjalnej dokumentacji):
#  - naglowek zmiennej dlugosci (zaczyna sie od "\x0fRDFILEVER"), w nim rozmiar stolu (mm);
#  - tabela A: warstwy wektorowe, rekordy co 1151 bajtow (czarna + 256 kolorow palety);
#  - tabela B: te same warstwy dla bitmap, rekordy co 1009 bajtow;
#  - lista obiektow: naglowek obiektu, kolor warstwy, ramka i odcinki (linia / krzywa Beziera)
#    albo osadzony plik BMP. Wspolrzedne w mm, os Y w gore (uklad stolu).

RLD_MAGIC = b"\x0fRDFILEVER"
_RLD_A = 1151
_RLD_B = 1009
_RLD_PALETTE = ("0000ff", "ff0000", "00ff00", "fa8072", "ffff00")
_RLD_OBJ = re.compile(rb"[\x01\x02]\x00[\x01\x02]\x00\x00[\x01-\x20]\x00\x00\x00", re.S)
_RLD_MIN_INTERVAL = 0.02  # mm; mniej to prawie zawsze blad wpisu (np. "0,15" w RDWorks)


def is_rld(path):
    try:
        with open(str(path), "rb") as fh:
            return fh.read(len(RLD_MAGIC)) == RLD_MAGIC
    except OSError:
        return False


def _rld_tables(d):
    """Polozenie tabel A i B (po kolorach palety, ktore RDWorks ma zawsze te same)."""
    pal = [bytes.fromhex(c) for c in _RLD_PALETTE]
    start = 0
    while True:
        p = d.find(pal[0], start, 20000)
        if p < 0:
            raise ValueError(_("nie rozpoznano tabeli warstw RDWorks (nieobsługiwana wersja pliku)"))
        if all(d[p + _RLD_A * k:p + _RLD_A * k + 3] == pal[k] for k in range(len(pal))):
            a0 = p - 327
            break
        start = p + 1
    b0 = a0 + 256 * _RLD_A + 26
    if a0 - _RLD_A < 0 or b0 + 256 * _RLD_B > len(d) or not all(
            d[b0 + _RLD_B * k + 159:b0 + _RLD_B * k + 162] == pal[k] for k in range(len(pal))):
        raise ValueError(_("nie rozpoznano tabeli warstw bitmap RDWorks (nieobsługiwana wersja pliku)"))
    return a0, b0


def _fin(v):
    return v == v and abs(v) < 1e6


def _rld_f64(d, off, default=None):
    try:
        v = struct.unpack_from("<d", d, off)[0]
    except struct.error:
        return default
    return v if _fin(v) else default


def _rld_i32(d, off, default=0):
    try:
        return struct.unpack_from("<i", d, off)[0]
    except struct.error:
        return default


def _rld_layers(d, a0, b0):
    """{kolor 'rrggbb': (indeks palety, rekord A, rekord B)}. Czarna warstwa ma indeks -1."""
    out = {}
    for k in range(-1, 256):
        base = a0 + _RLD_A * k
        col = d[base + 327:base + 330].hex()
        if col in out:
            continue
        out[col] = (k, base, b0 + _RLD_B * k if k >= 0 else None)
    return out


def _rld_speed(d, off, warn):
    v = _rld_f64(d, off, None)
    if v is None or not 0 < v <= 20000:
        warn(_("nieprawidłowa prędkość warstwy RDWorks (%s): użyto 100 mm/s")
             % ("?" if v is None else fmt(v)))
        return 100.0
    return v


def _rld_vector_spec(d, base, warn):
    """Parametry warstwy wektorowej (pola potwierdzone na plikach testowych)."""
    speed = _rld_speed(d, base + 335, warn)
    mn = _rld_f64(d, base + 343, 0.0)
    mx = _rld_f64(d, base + 351, 0.0)
    passes = _rld_i32(d, base + 623, 1) if d[base + 619] else 1
    mode = d[base + _RLD_A]          # 1 = Cut, 0 = Scan
    spec = {"kind": "fill" if mode == 0 else "cut", "power": max(0.0, min(100.0, mx)),
            "min_power": max(0.0, min(100.0, min(mn, mx))), "speed": speed,
            "passes": max(1, min(100, passes)), "off": 0 if d[base + 331] else 1,
            "air": 1 if d[base + 375] else 0, "kerf": 0.0}
    if mode not in (0, 1):
        spec["off"] = 1
        warn(_("nieznany tryb obróbki RDWorks (%d): warstwa ma wyłączone wyjście, sprawdź ją "
               "w LightBurnie") % mode)
    if mode == 0:
        iv = _rld_f64(d, base + _RLD_A + 17, 0.1)
        spec.update({"interval": iv if iv and 0 < iv <= 2 else 0.1, "bidir": 1, "angle": 0.0,
                     "cross": 0, "offset": 0})
    return spec


def _rld_bitmap_spec(d, base, gray_levels, warn):
    """Parametry warstwy bitmapowej (tabela B)."""
    speed = _rld_speed(d, base + 167, warn)
    mn = _rld_f64(d, base + 175, 0.0)
    mx = _rld_f64(d, base + 183, 0.0)
    passes = _rld_i32(d, base + 455, 1) if d[base + 451] else 1
    iv = _rld_f64(d, base + _RLD_B + 1, 0.1)
    if any(_rld_i32(d, base + off) for off in (983, 987, 991)):
        # trzy przelaczniki (Negative Engrave, Optimized Scan, Ramp Effect) w nieustalonej
        # jeszcze kolejnosci: negatywu nie zgadujemy
        warn(_("warstwa bitmapy ma włączone Negative Engrave, Optimized Scan albo Ramp Effect; "
               "te ustawienia nie są jeszcze przenoszone, sprawdź negatyw w LightBurnie"))
    return {"kind": "image", "power": max(0.0, min(100.0, mx)),
            "min_power": max(0.0, min(100.0, min(mn, mx))), "speed": speed,
            "passes": max(1, min(100, passes)), "off": 0 if d[base + 163] else 1,
            "air": 1 if d[base + 207] else 0, "kerf": 0.0,
            "interval": iv if iv and 0 < iv <= 2 else 0.1, "bidir": 1,
            "dither": "threshold" if gray_levels <= 2 else "jarvis", "negative": 0}


def _rld_bbox(d, o):
    if o + 32 > len(d):
        return None
    x0, x1, y0, y1 = struct.unpack_from("<4d", d, o)
    if not all(_fin(v) for v in (x0, x1, y0, y1)) or x1 < x0 or y1 < y0:
        return None
    return x0, x1, y0, y1


def _rld_path(d, h, end):
    """Obiekt wektorowy pod offsetem h albo None. Zwraca (kolor, odcinki, zamkniety, koniec)."""
    box = _rld_bbox(d, h + 13)
    if box is None:
        return None
    x0, x1, y0, y1 = box
    o = h + 45
    if o + 8 > end:
        return None
    closed, ns = struct.unpack_from("<ii", d, o)
    o += 8
    if closed not in (0, 1) or not (0 < ns <= 2000000) or o + 74 * ns > end:
        return None
    segs = []
    size = (x1 - x0) + (y1 - y0)
    tol = max(1e-3, size * 1e-6)
    slack = max(1.0, size * 0.5)
    for _k in range(ns):
        t = struct.unpack_from("<H", d, o)[0]
        v = struct.unpack_from("<8d", d, o + 6)
        o += 74
        # przy liniach punkty kontrolne to smieci z pamieci RDWorks: sprawdzamy tylko konce
        if t not in (0, 3) or not all(_fin(x) for x in (v if t == 3 else v[:4])):
            return None
        if not (x0 - slack <= v[0] <= x1 + slack and y0 - slack <= v[1] <= y1 + slack):
            return None
        if segs and (abs(segs[-1][1][2] - v[0]) > tol or abs(segs[-1][1][3] - v[1]) > tol):
            return None
        segs.append((t, v))
    return d[h + 9:h + 12].hex(), segs, bool(closed), o, tol


def _png_bytes(w, h, ctype, raw):
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _gray_levels(lines):
    """2, gdy obraz ma tylko czern i biel (dithering Threshold); 3, gdy ma inne odcienie."""
    for ln in lines:
        if ln.translate(None, b"\x00\xff"):
            return 3
    return 2


def _rgb_png(w, h, rgb_rows):
    """rgb_rows: lista (r, g, b) bajtow. Obraz bez kolorow zapisuje jako PNG w skali szarosci."""
    if all(r == g == b for r, g, b in rgb_rows):
        lines = [r for r, _g, _b in rgb_rows]
        return (_png_bytes(w, h, 0, b"".join(b"\x00" + ln for ln in lines)), w, h,
                _gray_levels(lines))
    out = []
    for r, g, b in rgb_rows:
        line = bytearray(3 * w)
        line[0::3], line[1::3], line[2::3] = r, g, b
        out.append(b"\x00" + bytes(line))
    return _png_bytes(w, h, 2, b"".join(out)), w, h, 256


def _bmp_to_png(bmp):
    """BMP (1/4/8 bit z paleta albo 24/32 bit, bez kompresji) -> PNG bez Pillow.
    Zwraca (png, szerokosc, wysokosc, poziomy: 2 = tylko czern i biel, 3 = odcienie szarosci,
    256 = kolorowy)."""
    if bmp[:2] != b"BM" or len(bmp) < 54:
        raise ValueError("bmp")
    off = struct.unpack_from("<I", bmp, 10)[0]
    hsz, w, h, _pl, bpp, comp = struct.unpack_from("<IiiHHI", bmp, 14)
    if hsz < 40 or comp not in (0, 3) or w <= 0 or h == 0 or bpp not in (1, 4, 8, 24, 32) \
            or w * abs(h) > 4e8:
        raise ValueError("bmp")
    alpha_mask = None
    if comp == 3:
        # BI_BITFIELDS: przyjmujemy tylko zwykly uklad B, G, R (+ A)
        if bpp != 32 or len(bmp) < (70 if hsz >= 56 else 66) or \
                struct.unpack_from("<III", bmp, 54) != (0xff0000, 0xff00, 0xff):
            raise ValueError("bmp")
        if hsz >= 56:
            alpha_mask = struct.unpack_from("<I", bmp, 66)[0]
            if alpha_mask not in (0, 0xff000000):
                raise ValueError("bmp")
    bottom_up = h > 0
    h = abs(h)
    stride = ((w * bpp + 31) // 32) * 4
    if off + stride * h > len(bmp):
        raise ValueError("bmp")
    starts = [off + (h - 1 - y if bottom_up else y) * stride for y in range(h)]
    if bpp > 8:
        step = bpp // 8
        pxs = [bmp[s:s + w * step] for s in starts]
        use_alpha = False
        if step == 4 and alpha_mask != 0:
            # kanal alfa (decyzja dla calego obrazu, nie dla wiersza): przezroczyste piksele
            # na bialo, inaczej zostalyby wypalone; sam zerowy kanal = brak alfy (XRGB)
            alphas = [px[3::4] for px in pxs]
            opaque = b"\xff" * w
            use_alpha = any(any(a) for a in alphas) and any(a != opaque for a in alphas)
        rgb = []
        for i, px in enumerate(pxs):
            b, g, r = px[0::step], px[1::step], px[2::step]
            if use_alpha:
                a = alphas[i]
                r = bytes(255 - ((255 - c) * al) // 255 for c, al in zip(r, a))
                g = bytes(255 - ((255 - c) * al) // 255 for c, al in zip(g, a))
                b = bytes(255 - ((255 - c) * al) // 255 for c, al in zip(b, a))
            rgb.append((r, g, b))
        return _rgb_png(w, h, rgb)
    n = struct.unpack_from("<I", bmp, 46)[0] or (1 << bpp)
    pbase = 14 + hsz
    pal = []
    for i in range(min(n, 1 << bpp)):
        b, g, r = bmp[pbase + 4 * i:pbase + 4 * i + 3].ljust(3, b"\x00")
        pal.append((r, g, b))
    while len(pal) < 256:
        pal.append((0, 0, 0))
    if bpp == 8:
        idx_rows = [bmp[s:s + w] for s in starts]
    else:
        per = 8 // bpp
        mask = (1 << bpp) - 1
        table = [bytes((byte >> (8 - bpp * (k + 1))) & mask for k in range(per))
                 for byte in range(256)]
        idx_rows = [b"".join(table[c] for c in bmp[s:s + stride])[:w] for s in starts]
    lr = bytes(p[0] for p in pal)
    lg = bytes(p[1] for p in pal)
    lb = bytes(p[2] for p in pal)
    return _rgb_png(w, h, [(r.translate(lr), r.translate(lg), r.translate(lb)) for r in idx_rows])


def read_rld(path, log):
    """Projekt RDWorks (.rld) -> lista z jedna plansza."""
    d = Path(path).read_bytes()
    if not d.startswith(RLD_MAGIC):
        raise ValueError(_("to nie jest plik RDWorks (.rld)"))
    try:
        return _read_rld(path, d, log)
    except (struct.error, IndexError):
        raise ValueError(_("uszkodzony plik RDWorks (.rld)"))


def _read_rld(path, d, log):
    ver = d[1:16].decode("ascii", "replace")
    a0, b0 = _rld_tables(d)
    layers = _rld_layers(d, a0, b0)
    bed_w = _rld_f64(d, 20, 0.0) or 0.0
    bed_h = _rld_f64(d, 28, 0.0) or 0.0
    if not (0 < bed_h < 100000):
        bed_h = 0.0
    cv = Canvas(Path(path).stem)
    cv.machine = "RDWorks %s%s" % (ver.replace("RDFILEVER", ""),
                                   (", %s x %s mm" % (fmt(bed_w), fmt(bed_h))) if bed_h else "")
    warned = set()

    def warn(msg):
        if msg not in warned:
            warned.add(msg)
            log(_("  UWAGA: ") + msg)

    pos = b0 + 256 * _RLD_B
    end = len(d)
    specs = {}
    unknown = set()
    bad = 0
    small_iv = set()
    n = 0
    while True:
        m = _RLD_OBJ.search(d, pos)
        if not m:
            break
        h = m.start()
        if d[h] == 1:
            r = _rld_path(d, h, end)
            if r is None:
                if _rld_bbox(d, h + 13) is not None:
                    bad += 1
                pos = h + 1
                continue
            col, segs, closed, pos, tol = r
            sp = SubPath((segs[0][1][0], bed_h - segs[0][1][1]))
            for t, v in segs:
                if t == 3:
                    sp.segs.append(("C", (v[4], bed_h - v[5]), (v[6], bed_h - v[7])))
                else:
                    sp.segs.append(("L", None, None))
                sp.pts.append((v[2], bed_h - v[3]))
            if closed:
                if len(sp.pts) > 2 and abs(sp.pts[-1][0] - sp.pts[0][0]) <= tol \
                        and abs(sp.pts[-1][1] - sp.pts[0][1]) <= tol:
                    sp.pts.pop()          # ostatni punkt = pierwszy: LightBurn domyka sam
                elif len(sp.pts) > 1:
                    sp.segs.append(("L", None, None))  # domkniecie odcinkiem
                sp.closed = len(sp.pts) > 1
            it = Item()
            it.kind = "path"
            it.subpaths = [sp]
            levels = 0
        else:
            o = h + 13
            box = _rld_bbox(d, o)
            if box is None or o + 38 > end or d[o + 32:o + 34] != b"BM":
                pos = h + 1
                continue
            x0, x1, y0, y1 = box
            size = struct.unpack_from("<I", d, o + 34)[0]
            if not (x1 > x0 and y1 > y0) or size < 54 or o + 32 + size > end:
                bad += 1
                pos = h + 1
                continue
            col = d[h + 9:h + 12].hex()
            pos = o + 32 + size
            try:
                png, w, hh, levels = _bmp_to_png(d[o + 32:o + 32 + size])
            except (ValueError, struct.error, IndexError):
                warn(_("bitmapa w nieobsługiwanym formacie (np. skompresowana), pominięta"))
                continue
            it = Item()
            it.kind = "bitmap"
            it.png = png
            it.ow, it.oh = w, hh
            it.matrix = [(x1 - x0) / w, 0, 0, (y1 - y0) / hh, x0, bed_h - y1]
        n += 1
        it.id = "rld%d" % n
        it.layer = "#" + col
        it.zorder = n
        it.ptype = "RLD"
        lay = layers.get(col)
        if lay is None:
            unknown.add(col)
            spec = {"kind": "image" if it.kind == "bitmap" else "cut", "power": 0.0,
                    "min_power": 0.0, "speed": 100.0, "passes": 1, "off": 1, "kerf": 0.0}
            if it.kind == "bitmap":
                spec.update({"interval": 0.1, "bidir": 1, "dither": "jarvis", "negative": 0})
        elif it.kind == "bitmap":
            if lay[2] is None:
                warn(_("bitmapa na czarnej warstwie: parametry warstwy bitmapowej nie są jeszcze "
                       "rozpoznane, użyto parametrów wektorowych tej warstwy"))
                spec = _rld_vector_spec(d, lay[1], warn)
                spec.update({"kind": "image", "interval": spec.get("interval", 0.1), "bidir": 1,
                             "dither": "threshold" if levels <= 2 else "jarvis", "negative": 0})
                for k in ("angle", "cross", "offset"):
                    spec.pop(k, None)
            else:
                spec = _rld_bitmap_spec(d, lay[2], levels, warn)
        else:
            spec = _rld_vector_spec(d, lay[1], warn)
        if spec.get("interval") is not None and spec["interval"] < _RLD_MIN_INTERVAL:
            small_iv.add("#%s %s mm" % (col, fmt(spec["interval"])))
        it.rd_spec = spec
        cv.layers.setdefault(it.layer, {"name": it.layer, "order": lay[0] if lay else 999,
                                        "visible": True})
        cv.items.append(it)
        specs[(col, spec["kind"])] = spec
    if unknown:
        warn(_("obiekty na warstwach spoza palety RDWorks (%s): wyłączone wyjście i moc 0")
             % ", ".join("#" + c for c in sorted(unknown)))
    if small_iv:
        warn(_("bardzo mały odstęp linii (%s); sprawdź wartość w RDWorks (\"0,15\" z przecinkiem "
               "RDWorks zapisuje jako 0,0025)") % ", ".join(sorted(small_iv)))
    if bad:
        warn(_("nie dało się odczytać części obiektów (%d; plik uszkodzony lub ucięty albo nieznany "
               "typ obiektu); sprawdź projekt") % bad)
    if not cv.items:
        warn(_("w pliku RDWorks nie znaleziono obiektów"))
    if cv.items:
        log(_("  RDWorks: liczba obiektów %d, warstwy: %s") % (len(cv.items), ", ".join(
            "#%s %s" % (c, _(OP_LABEL[k])) for c, k in specs)))
    return [cv]


def read_project(path, log):
    p = str(path)
    if is_rld(p):
        return read_rld(p, log)
    if p.lower().endswith(".rld"):
        raise ValueError(_("nieobsługiwana wersja pliku RDWorks (.rld)"))
    if zipfile.is_zipfile(p):
        return read_xs(p, log)
    with open(p, "rb") as fh:
        if fh.read(2) == b"PK":
            raise ValueError(_("uszkodzone archiwum .xs (plik ucięty lub niekompletny)"))
    return read_xcs(p, log)


# ----------------------------------------------------------------------------
# Mapowanie operacji xTool -> warstwy LightBurn
# ----------------------------------------------------------------------------

KIND_RANK = {"image": 0, "fill": 1, "line": 2, "cut": 3}
KIND_NAME = {"image": "Grawer obrazu", "fill": "Wypełnienie",
             "line": "Grawer linią", "cut": "Cięcie"}

DITHER = {
    "jarvis": "jarvis", "stucki": "stucki", "atkinson": "atkinson",
    "grayscale": "grayscale", "gray": "grayscale", "greyscale": "grayscale",
    # nazwy trybow potwierdzone w plikach zapisanych przez LightBurn 2.x
    "floyd": "dithered", "floydsteinberg": "dithered", "dither": "dithered",
    "dithered": "dithered",
    "bayer": "ordered", "ordered": "ordered",
    "binary": "threshold", "threshold": "threshold", "blackwhite": "threshold",
    "halftone": "halftone", "newsprint": "newsprint", "sketch": "sketch",
}


def _num(v, default):
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except (TypeError, ValueError):
        pass
    return default


# ----------------------------------------------------------------------------
# Baza materialow (CO2 100 W, Ruida) - punkt startowy, edytowalna
# ----------------------------------------------------------------------------

MATERIALS_PATH = Path.home() / ".xs2lightburn_materialy.json"
DB_VERSION = 1
OPS = ("cut", "line", "fill", "image")
OP_LABEL = {"cut": "Cięcie", "line": "Grawer linią", "fill": "Wypełnienie", "image": "Obraz"}

# Przyklady sa celowo po angielsku we wszystkich jezykach: te same nazwy w bazie i w --material.
_S_NOVA = ("Thunder Laser, public table Nova DC Glass 100W "
           "(thunderlaser.com/laser-settings/co2-laser-machine.html)")
_S_AU = ("Thunder Laser, public 100 W table, 2\" head (thunderlaser.com.au, "
         "parameters-for-different-materials-100watt.pdf)")

DB_NOTE = ("Baza zawiera tylko PRZYKŁADY. Wpisz własne, sprawdzone parametry swojego lasera "
           "(przycisk Dodaj albo Importuj). Rzeczywista moc rury, soczewka, dmuch i partia "
           "materiału zmieniają wynik: zrób test (LightBurn: Laser Tools > Material Test). "
           "Moc min w wierszach cięcia to moc w narożnikach (Ruida).")


def _op(speed, mn, mx, passes=1, interval=None, dither=None, air=True, kerf=0.0):
    d = {"speed": speed, "min": mn, "max": mx, "passes": passes, "air": air}
    if interval is not None:
        d["interval"] = interval
    if dither is not None:
        d["dither"] = dither
    if kerf:
        d["kerf"] = kerf
    return d


def _p(group, name, thick, ops, notes="", source=""):
    return {"group": group, "name": name, "thickness": thick, "ops": ops,
            "notes": notes, "source": source}


def _default_profiles():
    """Przyklady (laser CO2 100 W, rura szklana, publiczne tabele Thunder Laser), po angielsku."""
    ex = ("EXAMPLE from a public table for a 100 W CO2 laser (glass tube, 2\" lens), "
          "not tested on your machine. Replace with your own values.")
    grp = "Examples"
    cut_fill = "Cut: Nova 100W 50 mm/s, 10-65%. Engrave: 500 mm/s 20% at 0.08 mm."
    return [
        _p(grp, "Example: birch plywood 3 mm", 3,
           {"cut": _op(50, 10, 65), "fill": _op(500, 20, 20, interval=0.08)},
           ex + " " + cut_fill, _S_NOVA + "; " + _S_AU),
        _p(grp, "Example: acrylic (PMMA) 3 mm", 3,
           {"cut": _op(50, 10, 65), "fill": _op(500, 20, 20, interval=0.08)},
           ex + " " + cut_fill, _S_NOVA + "; " + _S_AU),
        _p(grp, "Example: anodized aluminum (engrave only)", 0,
           {"fill": _op(500, 30, 30, interval=0.08),
            "image": _op(500, 30, 30, interval=0.08, dither="jarvis")},
           ex + " Engrave: 500 mm/s 30% at 0.08 mm. Cut layers get output off.", _S_AU),
    ]


def _clean_op(op, kind):
    """Walidacja jednej operacji; zwraca slownik albo None."""
    if not isinstance(op, dict):
        return None
    try:
        speed = float(op.get("speed"))
        mx = float(op.get("max"))
        mn = float(op.get("min", mx))
        passes = int(float(op.get("passes", 1)))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(speed) and 0 < speed <= 3000 and 0 <= mn <= mx <= 100
            and 1 <= passes <= 100):
        return None
    air = op.get("air", True)
    if isinstance(air, str):
        air = air.strip().lower() not in ("false", "0", "nie", "no", "off", "")
    out = {"speed": speed, "min": mn, "max": mx, "passes": passes, "air": bool(air)}
    if kind in ("fill", "image"):
        iv = _num(op.get("interval"), 0.1)
        if not 0 < iv <= 2:
            return None
        out["interval"] = iv
    if kind == "image":
        d = DITHER.get(re.sub(r"[^a-z]", "", str(op.get("dither", "jarvis")).lower()))
        out["dither"] = d or "jarvis"
    kerf = _num(op.get("kerf"), 0.0)
    if abs(kerf) > 2:
        return None
    if kerf:
        out["kerf"] = kerf
    return out


def clean_profile(p):
    if not isinstance(p, dict) or not str(p.get("name", "")).strip():
        return None
    ops = {}
    raw_ops = p.get("ops") if isinstance(p.get("ops"), dict) else {}
    for k in OPS:
        o = _clean_op(raw_ops.get(k), k)
        if o:
            ops[k] = o
    return {"group": str(p.get("group") or _("Inne")).strip(), "name": str(p["name"]).strip(),
            "thickness": _num(p.get("thickness"), 0.0), "ops": ops,
            "notes": str(p.get("notes") or ""), "source": str(p.get("source") or "")}


def load_materials(log=None, path=None, skipped=None, create=True):
    """Wczytuje baze; przy braku pliku tworzy go z przykladami (create=True).
    skipped: lista, do ktorej trafiaja opisy pominietych wpisow."""
    path = Path(path) if path else MATERIALS_PATH
    if not path.exists() and not create:
        raise ValueError(_("nie ma pliku %s") % path)
    if not path.exists():
        db = {"version": DB_VERSION, "note": _(DB_NOTE), "profiles": _default_profiles()}
        save_materials(db, path)
        if log:
            log(_("Utworzono bazę materiałów: %s") % path)
        return db
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(_("baza materiałów %s jest uszkodzona (%s); popraw plik albo usuń go, "
                         "żeby odtworzyć domyślną") % (path, e))
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles", []), list):
        raise ValueError(_("baza materiałów %s ma zły format (oczekiwano listy 'profiles'); "
                         "popraw plik albo usuń go, żeby odtworzyć domyślną") % path)
    profs = []
    names = set()
    for p in raw.get("profiles") or []:
        c = clean_profile(p)
        if c is None or c["name"] in names:
            what = str(p.get("name") if isinstance(p, dict) else p)[:60]
            why = _("zdublowana nazwa") if c is not None else _("błędne lub brakujące wartości")
            if skipped is not None:
                skipped.append("%s (%s)" % (what, why))
            if log:
                log(_("  UWAGA: pominięto wpis bazy '%s': %s") % (what, why))
            continue
        nops = len([k for k in OPS if isinstance((p.get("ops") or {}).get(k), dict)]) \
            if isinstance(p.get("ops"), dict) else 0
        if len(c["ops"]) < nops:
            msg = _("%s (część operacji ma błędne wartości i została pominięta)") % c["name"]
            if skipped is not None:
                skipped.append(msg)
            if log:
                log(_("  UWAGA: wpis bazy ") + msg)
        names.add(c["name"])
        profs.append(c)
    return {"version": DB_VERSION, "note": str(raw.get("note") or DB_NOTE), "profiles": profs}


def backup_materials(path=None):
    """Kopia .bak przed pierwszym nadpisaniem w sesji edytora."""
    path = Path(path) if path else MATERIALS_PATH
    if path.exists():
        bak = path.with_name(path.name + ".bak")
        bak.write_bytes(path.read_bytes())
        return bak
    return None


def save_materials(db, path=None):
    path = Path(path) if path else MATERIALS_PATH
    write_atomic(path, json.dumps(db, ensure_ascii=False, indent=2))


def profile_sort_key(p):
    """Sortowanie naturalne: 3 mm przed 10 mm."""
    parts = re.split(r"(\d+(?:[.,]\d+)?)", p["name"].lower())
    key = [float(x.replace(",", ".")) if i % 2 else x for i, x in enumerate(parts)]
    return (p["group"].lower(), key)


def find_profile(db, name):
    if not name:
        return None
    for p in db.get("profiles", []):
        if p["name"] == name:
            return p
    return None


def apply_material(spec, profile, warn):
    """Nadpisuje parametry warstwy wartosciami z profilu materialu."""
    # Warstwy "bez obrobki" tez dostaja wartosci z bazy (wyjscie zostaje wylaczone),
    # zeby po recznym wlaczeniu w LightBurnie nie poszly z moca z innej maszyny.
    if profile is None:
        return spec
    kind = spec["kind"]
    op = profile["ops"].get(kind)
    if spec.get("kerf"):
        warn(_("kerf z xTool pominięty (inna maszyna); kerf bierze się z bazy materiałów"))
    spec["kerf"] = 0.0
    if spec.get("noop"):
        # element bez operacji w xTool: nie zgadujemy, warstwa wylaczona z moca 0
        spec["off"] = 1
        spec["power"] = 0.0
        spec["min_power"] = 0.0
        return spec
    if not op:
        spec["off"] = 1
        spec["missing"] = 1
        spec["power"] = 0.0
        spec["min_power"] = 0.0
        warn(_("materiał '%s' nie ma ustawień dla operacji '%s': warstwa ma wyłączone wyjście "
             "i moc 0; uzupełnij bazę albo ustaw parametry ręcznie w LightBurnie")
             % (profile["name"], _(OP_LABEL[kind])))
        return spec
    spec["power"] = op["max"]
    spec["min_power"] = op["min"]
    spec["speed"] = op["speed"]
    spec["passes"] = op["passes"]
    spec["air"] = 1 if op["air"] else 0
    if kind in ("fill", "image"):
        spec["interval"] = op["interval"]
    if kind == "image":
        spec["dither"] = op["dither"]
    if op.get("kerf"):
        spec["kerf"] = op["kerf"]
    return spec


def layer_spec(it, log, warned, material=None):
    """Zwraca slownik opisujacy warstwe LightBurn dla elementu."""

    def warn(msg):
        if it.off:
            return
        if msg not in warned:
            warned.add(msg)
            log(_("  UWAGA: ") + msg)

    rd = getattr(it, "rd_spec", None)
    if rd is not None:
        # RDWorks: parametry warstwy odczytane wprost z pliku
        spec = dict(rd)
        if material is not None:
            apply_material(spec, material, warn)
        return spec
    pt = it.ptype
    p = it.params or {}
    kind = {"VECTOR_CUTTING": "cut", "VECTOR_ENGRAVING": "line",
            "FILL_VECTOR_ENGRAVING": "fill", "BITMAP_ENGRAVING": "image"}.get(pt)
    if kind is None:
        if pt:
            warn(_("nieznana operacja %s, przeniesiono jako %s")
                 % (pt, _("grawer obrazu") if it.kind == "bitmap" else _("cięcie")))
        kind = "image" if it.kind == "bitmap" else "cut"
        if not p:
            p = {"power": 0, "speed": 100, "repeat": 1}
    if it.kind == "bitmap":
        kind = "image"
    elif kind == "image":
        kind = "line"
    power = max(0.0, min(100.0, _num(p.get("power"), 0.0)))
    speed = _num(p.get("speed"), 100.0)
    if speed <= 0:
        speed = 100.0
    passes = int(_num(p.get("repeat"), 1)) or 1
    spec = {"kind": kind, "power": power, "speed": speed, "passes": max(1, passes),
            "off": 1 if it.off else 0}
    if not pt and material is not None:
        spec["noop"] = 1
    if kind in ("fill", "image"):
        dens = _num(p.get("density"), 100.0)
        if dens <= 0:
            dens = 100.0
        spec["interval"] = round(10.0 / dens, 5)   # linie/cm -> mm
        mode = str(p.get("bitmapScanMode", "zMode")).lower()
        spec["bidir"] = 1 if mode in ("zmode", "") else 0
        if mode not in ("zmode", ""):
            warn(_("tryb skanowania '%s' przeniesiony jako jednokierunkowy") % p.get("bitmapScanMode"))
    if kind == "fill":
        spec["angle"] = _num(p.get("scanAngle"), 0.0)
        spec["cross"] = 1 if p.get("crossAngle") else 0
        ft = str(p.get("fill_type") or "regular").lower()
        spec["offset"] = 1 if "offset" in ft else 0
        if ft != "regular" and not spec["offset"]:
            warn(_("typ wypełnienia '%s' nieobsługiwany, użyto zwykłego") % ft)
        if p.get("ramp"):
            warn(_("rampa mocy przy wypełnieniu nie została przeniesiona"))
    if kind == "image":
        key = re.sub(r"[^a-z]", "", str(p.get("bitmapMode", "jarvis")).lower())
        d = DITHER.get(key)
        if d is None:
            warn(_("tryb bitmapy '%s' nieznany, użyto jarvis") % p.get("bitmapMode"))
            d = "jarvis"
        spec["dither"] = d
        spec["negative"] = 1 if it.negative else 0
    spec["kerf"] = 0.0
    if p.get("enableKerf"):
        kd = _num(p.get("kerfDistance"), 0.0)
        if kd:
            spec["kerf"] = kd
            if material is None:
                warn(_("kerf przeniesiony do LightBurn (np. %s mm); sprawdź na próbce kierunek "
                     "przesunięcia") % fmt(kd))
    for k, label in (("defocus", _("defokus")),
                     ("enableBreakPoint", _("mostki (breakpoints)")),
                     ("enableOverCut", "overcut"),
                     ("cuttingDrop", _("obniżanie Z między przejściami"))):
        if p.get(k):
            warn(_("%s: włączone w xTool, w LightBurn ustaw ręcznie") % label)
    if material is not None:
        apply_material(spec, material, warn)
    return spec


def spec_key(color, spec):
    return (color,) + tuple(sorted(spec.items()))


# ----------------------------------------------------------------------------
# Zapis LightBurn (.lbrn2)
# ----------------------------------------------------------------------------

def vert_str(v):
    """Wierzcholek w skladni LightBurn; brak punktu kontrolnego = 'c0x1'/'c1x1'."""
    s = "V%s %s" % (fmt(v["x"]), fmt(v["y"]))
    c0, c1 = v["c0"], v["c1"]
    s += ("c0x%sc0y%s" % (fmt(c0[0]), fmt(c0[1]))) if c0 is not None else "c0x1"
    s += ("c1x%sc1y%s" % (fmt(c1[0]), fmt(c1[1]))) if c1 is not None else "c1x1"
    return s


def build_path_shape(subs, cut_index, ind):
    verts = []
    prims = []
    for sp in subs:
        base = len(verts)
        n = len(sp.pts)
        for (x, y) in sp.pts:
            verts.append({"x": x, "y": y, "c0": None, "c1": None})
        for k, (kind, c1, c2) in enumerate(sp.segs):
            a = base + k
            b = base + ((k + 1) % n)
            if kind == "C":
                verts[a]["c0"] = c1   # LightBurn: c0 = kontrolka wychodzaca
                verts[b]["c1"] = c2   # c1 = kontrolka wchodzaca
                prims.append("B%d %d" % (a, b))
            else:
                prims.append("L%d %d" % (a, b))
    return (
        '%s<Shape Type="Path" CutIndex="%d">\n'
        '%s    <XForm>1 0 0 1 0 0</XForm>\n'
        '%s    <VertList>%s</VertList>\n'
        '%s    <PrimList>%s</PrimList>\n'
        '%s</Shape>\n' % (ind, cut_index, ind, ind, "".join(vert_str(v) for v in verts),
                          ind, "".join(prims), ind)
    )


def _flip_image(png, fx, fy):
    """Odbija piksele (Pillow). Zwraca None, gdy Pillow niedostepny."""
    try:
        from PIL import Image, ImageOps
        import io
    except Exception:
        return None
    try:
        im = Image.open(io.BytesIO(png))
        im.load()
        if im.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"):
            im = im.convert("RGBA")
        if fx < 0:
            im = ImageOps.mirror(im)
        if fy < 0:
            im = ImageOps.flip(im)
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


def build_bitmap_shape(it, cut_index, frame, flip, ind, serial, log, state):
    png = ensure_png_or_jpg(it.png, log)
    if png is None:
        return None
    size = png_size(png)
    ow, oh = (size if size else (it.ow, it.oh))
    if not ow or not oh:
        log(_("  UWAGA: nieznane wymiary obrazu %s, pominięty") % it.id[:8])
        return None
    a, b, c, d, e, f = it.matrix
    sx = math.hypot(a, b)
    sy = math.hypot(c, d)
    if sx == 0 or sy == 0:
        log(_("  UWAGA: zerowa skala obrazu %s, pominięty") % it.id[:8])
        return None
    w, h = ow * sx, oh * sy
    fx, fy = flip
    lin = [a / sx, b / sx, c / sy, d / sy]
    # Obraz w odbitej ramce. LightBurn zapisuje go z odbitymi pikselami
    # i dodatnia skala (potwierdzone na plikach z LightBurn 2.2):
    # macierz F*L*F + piksele odbite. Bez Pillow: macierz F*L (ujemna skala),
    # wynik wizualnie ten sam.
    if fx < 0 or fy < 0:
        flipped = _flip_image(png, fx, fy)
        if flipped is not None:
            png = flipped
            lin = [fx * lin[0] * fx, fy * lin[1] * fx, fx * lin[2] * fy, fy * lin[3] * fy]
        else:
            if not state.get("nopil"):
                state["nopil"] = True
                log(_("  UWAGA: nie udało się odwrócić pikseli obrazu (brak biblioteki Pillow "
                    "albo nietypowy format); obraz odbity przez ujemną skalę, wygląd ten sam"))
            lin = [fx * lin[0], fy * lin[1], fx * lin[2], fy * lin[3]]
    cx, cy = apply_mat(frame, *apply_mat(it.matrix, ow / 2.0, oh / 2.0))
    xform = " ".join(fmt(v) for v in (lin[0], lin[1], lin[2], lin[3], cx, cy))
    return (
        '%s<Shape Type="Bitmap" CutIndex="%d" W="%s" H="%s" Gamma="1" Contrast="0" '
        'Brightness="0" EnhanceAmount="0" EnhanceRadius="0" EnhanceDenoise="0" '
        'File="" SourceHash="%d" Data="%s">\n'
        '%s    <XForm>%s</XForm>\n'
        '%s</Shape>\n' % (ind, cut_index, fmt(w), fmt(h), serial,
                          base64.b64encode(png).decode("ascii"), ind, xform, ind)
    )


def cutsetting_xml(idx, name, spec, priority):
    kind = spec["kind"]
    tag, typ = {"cut": ("CutSetting", "Cut"), "line": ("CutSetting", "Cut"),
                "fill": ("CutSetting", "Scan"),
                "image": ("CutSetting_Img", "Image")}[kind]
    if kind == "fill" and spec.get("offset"):
        typ = "Offset"
    pw = fmt(spec["power"])
    lines = [
        '    <%s type="%s">' % (tag, typ),
        '        <index Value="%d"/>' % idx,
        '        <name Value=%s/>' % quoteattr(name),
        '        <minPower Value="%s"/>' % fmt(spec.get("min_power", spec["power"])),
        '        <maxPower Value="%s"/>' % pw,
        '        <maxPower2 Value="%s"/>' % pw,
        '        <speed Value="%s"/>' % fmt(spec["speed"]),
        '        <priority Value="%d"/>' % priority,
        '        <numPasses Value="%d"/>' % spec["passes"],
    ]
    if kind in ("fill", "image"):
        lines.append('        <interval Value="%s"/>' % fmt(spec["interval"]))
        lines.append('        <bidir Value="%d"/>' % spec["bidir"])
    if kind == "image" and spec["interval"] > 0:
        # LightBurn zapisuje obok odstepu linii DPI = 25,4 / odstep (np. 0,11 mm -> 231)
        lines.append('        <dpi Value="%d"/>' % int(round(25.4 / spec["interval"])))
    if kind == "fill":
        lines.append('        <angle Value="%s"/>' % fmt(spec["angle"]))
        lines.append('        <crossHatch Value="%d"/>' % spec["cross"])
    if kind == "image":
        lines.append('        <ditherMode Value="%s"/>' % spec["dither"])
        if spec["negative"]:
            # tag potwierdzony w plikach zapisanych przez LightBurn 2.x
            lines.append('        <negative Value="1"/>')
    if spec.get("kerf"):
        lines.append('        <kerf Value="%s"/>' % fmt(spec["kerf"]))
    if spec.get("air", 1) == 0:
        lines.append('        <runBlower Value="0"/>')
    if spec.get("off"):
        lines.append('        <doOutput Value="0"/>')
    lines.append('    </%s>' % tag)
    return "\n".join(lines) + "\n"


def _world_points(it):
    if it.kind == "path":
        for sp in transform_subpaths(it.subpaths, it.matrix):
            for p in sp.pts:
                yield p
            for kind, c1, c2 in sp.segs:
                if kind == "C":
                    yield c1
                    yield c2
    else:
        size = png_size(it.png) or (it.ow, it.oh)
        ow, oh = size
        for px, py in ((0, 0), (ow, 0), (0, oh), (ow, oh)):
            yield apply_mat(it.matrix, px, py)


# Paleta warstw LightBurn C00-C29 (do doboru numeru warstwy po kolorze i do podgladu).
# Potwierdzone na plikach LightBurna: czarny -> C00, czerwony -> C02, #00BEFE -> C14.
LB_PALETTE = [
    "#000000", "#0000FF", "#FF0000", "#00E000", "#D0D000", "#FF8000", "#00E0E0",
    "#FF00FF", "#B4B4B4", "#0000A0", "#A00000", "#00A000", "#A0A000", "#C08000",
    "#00A0FF", "#A000A0", "#808080", "#7D87B9", "#BB7784", "#4A6FE3", "#D33F6A",
    "#8CD78C", "#F0B98D", "#F6C4E1", "#FA9ED4", "#500A78", "#B45A00", "#004754",
    "#86FA88", "#FFDB66",
]


def _rgb(c):
    c = str(c).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def nearest_palette_index(color):
    r, g, b = _rgb(color)
    best, bi = None, 0
    for i, pc in enumerate(LB_PALETTE):
        pr, pg, pb = _rgb(pc)
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if best is None or d < best:
            best, bi = d, i
    return bi


class Plan:
    """Wynik analizy planszy: warstwy LightBurn i przypisane do nich elementy."""

    def __init__(self):
        self.layers = []    # dict: idx, prio, name, color, spec, count
        self.entries = []   # (item, spec, idx)
        self.frame = [1, 0, 0, 1, 0, 0]
        self.flip = (1, 1)


def layer_name(color, spec):
    pw = fmt(spec["power"])
    if "min_power" in spec and spec["min_power"] != spec["power"]:
        pw = "%s-%s" % (fmt(spec["min_power"]), pw)
    nm = "%s %s %s%% %s" % (color.lstrip("#").upper(), _(KIND_NAME[spec["kind"]]),
                            pw, fmt(spec["speed"]))
    if spec["passes"] > 1:
        nm += " x%d" % spec["passes"]
    if spec.get("kerf"):
        nm += " kerf %s" % fmt(spec["kerf"])
    if spec["kind"] in ("fill", "image") and spec.get("interval"):
        nm += " %slpcm" % fmt(round(10.0 / spec["interval"], 1))
    if spec.get("missing"):
        nm += _(" (BRAK W BAZIE)")
    elif spec.get("noop") and spec["off"]:
        nm += _(" (BRAK OPERACJI)")
    elif spec["off"]:
        nm += _(" (bez obróbki)")
    return nm


def plan_canvas(cv, log, flip_x=False, flip_y=True, material=None):
    """Analiza planszy wspolna dla zapisu i podgladu. Zwraca Plan albo None."""
    warned = set()
    hidden = {c for c, ld in cv.layers.items() if ld.get("visible") is False}
    entries = []   # (item, spec)
    nhid = 0
    for it in sorted(cv.items, key=lambda x: x.zorder):
        if it.layer in hidden:
            nhid += 1
            continue
        entries.append((it, layer_spec(it, log, warned, material)))
    if nhid:
        log(_("  pominięto %d elementów z ukrytych warstw xTool") % nhid)
    if not entries:
        return None
    n_off = sum(1 for it, _ in entries if it.off)
    if n_off:
        log(_("  %d elementów wyłączonych z obróbki w xTool: przeniesione na warstwy "
            "'(bez obróbki)' z wyłączonym wyjściem (Output)") % n_off)
    n_noop = sum(1 for it, _ in entries if not it.ptype)
    if n_noop:
        log(_("  UWAGA: %d elementów nie ma operacji w xTool; dostały warstwę z mocą 0%s")
            % (n_noop, _(" i wyłączonym wyjściem") if material else ""))

    # Odbicie "w miejscu": obrys projektu zostaje tam, gdzie byl.
    pts = [p for it, _ in entries for p in _world_points(it)]
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    fx = -1 if flip_x else 1
    fy = -1 if flip_y else 1

    def lorder(color):
        o = (cv.layers.get(color) or {}).get("order", 999)
        return o if isinstance(o, (int, float)) else 999

    def build_keys():
        keys = {}
        for it, spec in entries:
            keys.setdefault(spec_key(it.layer, spec), (it.layer, spec))
        return sorted(keys.items(),
                      key=lambda kv: (kv[1][1]["off"], lorder(kv[1][0]), kv[1][0],
                                      KIND_RANK[kv[1][1]["kind"]], kv[1][1]["speed"]))

    ordered = build_keys()
    if len(ordered) > 30 and n_off:
        merged_vec = {"kind": "line", "power": 0.0, "speed": 100.0, "passes": 1,
                      "off": 1, "kerf": 0.0}
        merged_img = {"kind": "image", "power": 0.0, "speed": 100.0, "passes": 1,
                      "off": 1, "kerf": 0.0, "interval": 0.1, "bidir": 1,
                      "dither": "jarvis", "negative": 0}
        for n_, (it, spec) in enumerate(entries):
            if it.off:
                it.layer = "#000000"
                entries[n_] = (it, merged_img if it.kind == "bitmap" else merged_vec)
        ordered = build_keys()
        log(_("  UWAGA: za dużo warstw; elementy bez obróbki połączono we wspólne warstwy "
            "z wyłączonym wyjściem (osobno wektory i obrazy)"))
    if len(ordered) > 30:
        raise ValueError(_("projekt wymaga %d warstw, a LightBurn ma 30 (C00-C29)")
                         % len(ordered))

    # Numer warstwy: najblizszy kolor palety LightBurn; przy zajetym nastepny wolny.
    # Kolejnosc przydzialu: aktywne wektory, potem obrazy, na koncu "bez obrobki",
    # zeby ciecie/grawer dostaly kolor swojej warstwy xTool (jak przy imporcie w LightBurn).
    used = set()
    index_of = {}
    plan = Plan()
    order_assign = sorted(enumerate(ordered),
                          key=lambda e: (e[1][1][1]["off"], e[1][1][1]["kind"] == "image", e[0]))
    for prio, (k, (color, spec)) in order_assign:
        idx = nearest_palette_index(color)
        while idx in used:
            idx = (idx + 1) % 30
        used.add(idx)
        index_of[k] = idx
        plan.layers.append({"idx": idx, "prio": prio, "name": layer_name(color, spec),
                            "color": color, "spec": spec, "count": 0})
    plan.layers.sort(key=lambda L: L["prio"])
    by_idx = {L["idx"]: L for L in plan.layers}
    for it, spec in entries:
        idx = index_of[spec_key(it.layer, spec)]
        if it.kind == "path" or (png_size(it.png) or (it.ow and it.oh)):
            by_idx[idx]["count"] += 1
        plan.entries.append((it, spec, idx))
    plan.frame = [fx, 0, 0, fy, (minx + maxx) if fx < 0 else 0,
                  (miny + maxy) if fy < 0 else 0]
    plan.flip = (fx, fy)
    return plan


# --- reczne zmiany parametrow warstw (z okna programu) -------------------------

OVERRIDE_FIELDS = ("power", "min_power", "speed", "passes", "interval", "dither", "air",
                   "kerf", "off")


def validate_layer_fields(kind, raw):
    """raw: slownik tekstow z formularza. Zwraca (pola, None) albo (None, komunikat)."""
    def num(key, label):
        v = str(raw.get(key, "")).strip().replace(",", ".")
        try:
            f = float(v)
        except ValueError:
            raise ValueError(_("%s: wpisz liczbę") % label)
        if not math.isfinite(f):
            raise ValueError(_("%s: wpisz liczbę") % label)
        return f
    try:
        out = {}
        out["power"] = num("power", _("Moc max"))
        out["min_power"] = num("min_power", _("Moc min"))
        out["speed"] = num("speed", _("Prędkość"))
        p = num("passes", _("Przejścia"))
        if p != int(p) or not 1 <= p <= 100:
            raise ValueError(_("Przejścia: liczba całkowita 1-100"))
        out["passes"] = int(p)
        if not 0 <= out["power"] <= 100 or not 0 <= out["min_power"] <= 100:
            raise ValueError(_("Moc: zakres 0-100%"))
        if out["min_power"] > out["power"]:
            raise ValueError(_("Moc min nie może być większa od mocy max"))
        if not 0 < out["speed"] <= 20000:
            raise ValueError(_("Prędkość: zakres 0-20000 mm/s"))
        if kind in ("fill", "image"):
            iv = num("interval", _("Odstęp linii"))
            if not 0 < iv <= 2:
                raise ValueError(_("Odstęp linii: zakres 0-2 mm"))
            out["interval"] = iv
        if kind == "image":
            d = DITHER.get(re.sub(r"[^a-z]", "", str(raw.get("dither", "")).lower()))
            if d is None:
                raise ValueError(_("Dither: nieznany tryb"))
            out["dither"] = d
        kf = num("kerf", "Kerf") if str(raw.get("kerf", "")).strip() else 0.0
        if abs(kf) > 2:
            raise ValueError(_("Kerf: zakres -2..2 mm"))
        out["kerf"] = kf
        out["air"] = 1 if raw.get("air") else 0
        out["off"] = 0 if raw.get("output") else 1
    except ValueError as e:
        return None, str(e)
    return out, None


def effective_layer(L, fields):
    """Kopia warstwy planu z nalozonymi recznymi zmianami."""
    if not fields:
        return L
    sp = dict(L["spec"])
    sp.update({k: v for k, v in fields.items() if k in OVERRIDE_FIELDS})
    if not sp["off"]:
        sp.pop("missing", None)
        sp.pop("noop", None)
    L2 = dict(L)
    L2["spec"] = sp
    L2["name"] = layer_name(L["color"], sp)
    L2["edited"] = True
    return L2


def apply_overrides(plan, ov):
    """ov: {numer warstwy: pola}. Zmienia plan w miejscu."""
    if not ov:
        return plan
    plan.layers = [effective_layer(L, ov.get(L["idx"])) for L in plan.layers]
    return plan


def convert_canvas(cv, log, flip_x=False, flip_y=True, material=None, overrides=None):
    """Zwraca (tekst_xml, liczba_ksztaltow, plan)."""
    plan = plan_canvas(cv, log, flip_x, flip_y, material)
    if plan is None:
        return None, 0, None
    if overrides:
        apply_overrides(plan, overrides)
        log(_("  ręcznie zmienione warstwy: %s") % ", ".join(
            "C%02d" % L["idx"] for L in plan.layers if L.get("edited")))
    frame = plan.frame
    cut_xml = [cutsetting_xml(L["idx"], L["name"], L["spec"], L["prio"])
               for L in sorted(plan.layers, key=lambda L: L["idx"])]
    state = {}
    nbmp = 0
    groups = {}
    order = []
    count = 0
    for it, spec, ci in plan.entries:
        ind = "        " if it.group else "    "
        if it.kind == "path":
            subs = transform_subpaths(transform_subpaths(it.subpaths, it.matrix), frame)
            sh = build_path_shape(subs, ci, ind)
        else:
            sh = build_bitmap_shape(it, ci, frame, plan.flip, ind, nbmp, log, state)
            nbmp += 1
        if not sh:
            continue
        count += 1
        if it.group:
            if it.group not in groups:
                groups[it.group] = []
                order.append(("g", it.group))
            groups[it.group].append(sh)
        else:
            order.append(("s", sh))
    shapes = []
    for kind, val in order:
        if kind == "s":
            shapes.append(val)
        else:
            shapes.append('    <Shape Type="Group">\n'
                          '        <XForm>1 0 0 1 0 0</XForm>\n'
                          '        <Children>\n' + "".join(groups[val]) +
                          '        </Children>\n'
                          '    </Shape>\n')
    note = _("Przekonwertowano z %s (xs2lightburn %s)") % (
        "RDWorks" if (cv.machine or "").startswith("RDWorks") else "xTool Studio", VERSION)
    # Naglowek MirrorX/MirrorY = ustawienia urzadzenia LightBurn, z ktorych wynika odbicie:
    # MirrorX -> odbicie X; brak MirrorY -> os Y w gore (odbicie Y wzgledem xTool).
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<LightBurnProject AppVersion="2.2.01" FormatVersion="1" MaterialHeight="0" '
        'MirrorX="%s" MirrorY="%s">\n' % ("True" if flip_x else "False",
                                          "False" if flip_y else "True")
        + "".join(cut_xml) + "".join(shapes) +
        '    <Notes ShowOnLoad="0" Notes=%s/>\n' % quoteattr(note) +
        '</LightBurnProject>\n'
    )
    return xml, count, plan


def safe_stem(name):
    s = re.sub(r'[\\/:*?"<>|{}\x00-\x1f]+', "_", str(name)).strip(" ._")
    return s[:60] or "canvas"


def unique_path(p):
    if not p.exists():
        return p
    for i in range(2, 10000):
        q = p.with_name("%s (%d)%s" % (p.stem, i, p.suffix))
        if not q.exists():
            return q
    raise ValueError(_("za dużo plików o nazwie %s w folderze zapisu") % p.name)


def write_atomic(target, text):
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(target))


def friendly_error(e):
    if isinstance(e, zipfile.BadZipFile):
        return _("uszkodzony plik .xs (archiwum ZIP nie daje się odczytać)")
    if isinstance(e, (json.JSONDecodeError, UnicodeDecodeError)):
        return _("to nie jest poprawny plik .xs/.xcs/.rld (uszkodzone dane)")
    if isinstance(e, KeyError):
        return _("w pliku brakuje wymaganej części: %s") % e
    if isinstance(e, PermissionError):
        return _("brak uprawnień do zapisu lub odczytu: %s") % getattr(e, "filename", "")
    if isinstance(e, MemoryError):
        return _("za mało pamięci (plik za duży)")
    return str(e) or e.__class__.__name__


def convert_file(path, out_dir, log, flip_x=False, flip_y=True, overwrite=False,
                 material=None, overrides=None):
    """Konwertuje jeden plik; zwraca liste zapisanych plikow .lbrn2.
    overrides: {numer planszy (od 0, liczac wszystkie): {numer warstwy: pola}}."""
    path = Path(path)
    log(_("Plik: %s") % path)
    log(_("  Materiał: %s") % (material["name"] if material else
                            _("nie wybrano (parametry przeniesione z projektu)")))
    if not path.is_file():
        raise ValueError(_("nie ma takiego pliku"))
    canvases = read_project(path, log)
    if not canvases:
        raise ValueError(_("brak plansz w projekcie"))
    out_dir = Path(out_dir) if out_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for i, cv in enumerate(canvases):
        xml, nshapes, plan = convert_canvas(cv, log, flip_x, flip_y, material,
                                            (overrides or {}).get(i))
        if not nshapes:
            log(_("  plansza '%s': brak elementów do zapisu") % cv.name)
            continue
        target = out_dir / output_name(path, i, len(canvases), cv)
        if not overwrite:
            target = unique_path(target)
        write_atomic(target, xml)
        written.append(target)
        log(_("  zapisano: %s (%d kształtów, %d warstw)") % (target, nshapes, len(plan.layers)))
        for L in plan.layers:
            idx, nm, sp = L["idx"], L["name"], L["spec"]
            extra = ""
            if sp["kind"] in ("fill", "image"):
                extra = _(", odstęp linii %s mm") % fmt(sp["interval"])
            if sp["kind"] == "image":
                extra += ", dither %s" % sp["dither"]
            log(_("    C%02d  %s, przejść %d%s") % (idx, nm, sp["passes"], extra))
    return written


# ----------------------------------------------------------------------------
# Biblioteki materialow LightBurna (.clb i REST API) -> baza konwertera
# ----------------------------------------------------------------------------

# cale slowa: "Cut outline" albo "linia ciecia" to nadal ciecie
_LINE_WORDS = re.compile(r"\b(scor\w*|mark|marking|engrav\w*|etch\w*|grawer\w*|znakow\w*)\b",
                         re.I)


def _lib_kind(ltype, thick, label):
    """Operacja konwertera dla wpisu biblioteki: cut, line, fill, image albo None."""
    t = re.sub(r"[^a-z]", "", str(ltype or "").lower())
    if t == "image":
        return "image"
    if t in ("scan", "offset", "fill", "offsetfill"):
        return "fill"
    if t == "cut":
        # wektor bez grubosci materialu albo opisany jako grawer/znakowanie = grawer linia
        return "line" if thick is None or _LINE_WORDS.search(label or "") else "cut"
    return None


def _lib_thick(v):
    t = _num(v, -1.0)
    return t if t > 0 else None


def library_to_profiles(entries, libname, source):
    """Wpisy biblioteki LightBurna -> (profile bazy, pominiete).
    entries: [{"material", "thick" (None = bez grubosci), "label", "type", "op"}].
    Profil = material i grubosc; wpisy bez grubosci (np. grawer) dochodza do kazdej grubosci."""
    skipped = []
    group = "LightBurn: %s" % libname if libname else "LightBurn"
    by_mat = {}
    for e in entries:
        by_mat.setdefault(str(e.get("material") or "").strip() or "?", []).append(e)
    profiles = []
    used = set()

    def unique(name):
        base, i = name, 2
        while name in used:
            name, i = "%s %d" % (base, i), i + 1
        used.add(name)
        return name

    for mat, es in by_mat.items():
        shared = [e for e in es if e.get("thick") is None]
        thicks = []
        for e in es:
            if e.get("thick") is not None and e["thick"] not in thicks:
                thicks.append(e["thick"])
        buckets = [(t, [e for e in es if e.get("thick") == t]) for t in sorted(thicks)]
        if not buckets:
            buckets = [(None, [])]
        # wpisy bez grubosci: wspolne dla kazdej grubosci; ich wlasne dublety -> osobne profile
        shared_ops, extra = {}, []
        for e in shared:
            k = _lib_kind(e.get("type"), None, e.get("label"))
            op = _clean_op(e.get("op"), k) if k else None
            if k is None:
                skipped.append("%s: %s (%s)" % (mat, e.get("label") or "?",
                                                _("nieobsługiwany typ warstwy")))
            elif op is None:
                skipped.append("%s: %s (%s)" % (mat, e.get("label") or "?", _("złe wartości")))
            elif k in shared_ops:
                extra.append((None, k, op, e))
            else:
                shared_ops[k] = (op, e)
        for t, bes in buckets:
            name = mat if t is None else "%s %s mm" % (mat, fmt(t))
            ops = {}
            for e in bes:
                k = _lib_kind(e.get("type"), t, e.get("label"))
                op = _clean_op(e.get("op"), k) if k else None
                if k is None:
                    skipped.append("%s: %s (%s)" % (name, e.get("label") or "?",
                                                    _("nieobsługiwany typ warstwy")))
                elif op is None:
                    skipped.append("%s: %s (%s)" % (name, e.get("label") or "?", _("złe wartości")))
                elif k in ops:
                    extra.append((t, k, op, e))
                else:
                    ops[k] = (op, e)
            for k, v in shared_ops.items():
                ops.setdefault(k, v)
            if not ops:
                continue
            notes = "; ".join("%s: %s" % (k, v[1].get("label")) for k, v in ops.items()
                              if v[1].get("label"))
            profiles.append(clean_profile(_p(group, unique(name), t or 0.0,
                                             {k: v[0] for k, v in ops.items()}, notes, source)))
        # kolejny wpis tej samej operacji (np. dwa ciecia 3 mm): osobny profil z opisem w nazwie
        for t, k, op, e in extra:
            base = mat if t is None else "%s %s mm" % (mat, fmt(t))
            ops = {k: op}
            for k2, v in shared_ops.items():
                if k2 != k:
                    ops.setdefault(k2, v[0])
            label = e.get("label") or k
            profiles.append(clean_profile(_p(group, unique("%s (%s)" % (base, label)), t or 0.0,
                                             ops, "%s: %s" % (k, label), source)))
    return [p for p in profiles if p], skipped


def clb_entries(data):
    """Wpisy z pliku biblioteki LightBurna (.clb, XML). Zwraca (nazwa biblioteki, wpisy)."""
    import xml.etree.ElementTree as ET
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")  # LightBurn zapisuje UTF-8
        except UnicodeDecodeError:
            raise ValueError(_("to nie jest plik biblioteki LightBurna (.clb)"))
    # LightBurn nie uzywa DOCTYPE/ENTITY; odrzucenie chroni przed "billion laughs" na starym expat
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", data, re.I):
        raise ValueError(_("plik .clb zawiera niedozwolone definicje XML"))
    data = re.sub(r"^\s*<\?xml[^>]*\?>", "", data)  # deklaracja kodowania niepotrzebna dla str
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(_("to nie jest poprawny plik biblioteki LightBurna (.clb): %s") % e)
    if root.tag != "LightBurnLibrary":
        raise ValueError(_("to nie jest plik biblioteki LightBurna (.clb)"))
    entries = []
    for m in root.iter("Material"):
        mat = m.get("name") or ""
        for ent in m.findall("Entry"):
            thick = _lib_thick(ent.get("Thickness"))
            title = ent.get("NoThickTitle") or ""
            desc = ent.get("Desc") or ""
            label = " ".join(x for x in (title if thick is None else "", desc) if x).strip()
            for cs in ent:
                if cs.tag not in ("CutSetting", "CutSetting_Img"):
                    continue
                v = {c.tag: c.get("Value") for c in cs}
                ltype = cs.get("type") or ("Image" if cs.tag == "CutSetting_Img" else "")
                mx = _num(v.get("maxPower"), None)
                op = {"speed": v.get("speed"), "max": mx,
                      "min": min(_num(v.get("minPower"), mx if mx is not None else 0), mx)
                      if mx is not None else None,
                      "passes": v.get("numPasses", 1),
                      "air": v.get("runBlower", "1") not in ("0", "False", "false")}
                iv = _num(v.get("interval"), None)
                if iv is None and _num(v.get("dpi"), 0) > 0:
                    iv = round(25.4 / _num(v.get("dpi"), 254), 4)
                if iv is not None:
                    op["interval"] = iv
                if v.get("ditherMode"):
                    op["dither"] = v["ditherMode"]
                if _num(v.get("kerf"), 0):
                    op["kerf"] = _num(v.get("kerf"), 0)
                entries.append({"material": mat, "thick": thick, "label": label,
                                "type": ltype, "op": op})
                break  # pierwszy zapis wpisu = warstwa glowna (podwarstwy pomijamy)
    return root.get("DisplayName") or "", entries


def clb_to_profiles(path):
    """Plik .clb -> (profile, pominiete)."""
    path = Path(path)
    name, entries = clb_entries(path.read_bytes())
    return library_to_profiles(entries, name or path.stem, _("biblioteka LightBurna %s") % path.name)


_SPEED_TO_MMS = {"mm/s": 1.0, "mm/min": 1 / 60.0, "in/s": 25.4, "in/min": 25.4 / 60.0}


def api_library_entries(lib, units):
    """Odpowiedz GET /api/material-library + GET /api/units -> wpisy biblioteki.
    Wartosci z API sa w jednostkach wyswietlania LightBurna; tu przeliczane na mm i mm/s."""
    units = units if isinstance(units, dict) else {}
    sp = _SPEED_TO_MMS.get(str(units.get("units") or "").strip())
    dist = {"mm": 1.0, "in": 25.4}.get(str(units.get("distance_units") or "").strip())
    if sp is None or dist is None:
        # bez pewnych jednostek nie zgadujemy: zla jednostka to wielokrotnie inna energia
        raise ValueError(_("LightBurn nie podał jednostek (prędkość: %s, odległość: %s); "
                         "zaktualizuj LightBurna.") % (units.get("units"), units.get("distance_units")))
    if not isinstance(lib, dict) or lib.get("product") != "lightburn":
        raise ValueError(_("LightBurn zwrócił nieoczekiwaną odpowiedź (to nie jest biblioteka lasera)"))
    entries = []
    for e in lib.get("entries") or []:
        if not isinstance(e, dict) or not isinstance(e.get("cut"), dict):
            continue
        cut = e["cut"]
        prm = cut.get("params") if isinstance(cut.get("params"), dict) else {}
        thick = _lib_thick(e.get("thickness"))
        label = " ".join(x for x in ((e.get("no_thick_title") or "") if thick is None else "",
                                     e.get("description") or "") if x).strip()
        if cut.get("kind") == "image":
            ltype = "image"
        else:
            ltype = cut.get("mode") or ""
        mx = _num(prm.get("max_power"), None)
        op = {"speed": _num(prm.get("speed"), 0) * sp, "max": mx,
              "min": min(_num(prm.get("min_power"), mx), mx) if mx is not None else None,
              "passes": prm.get("num_passes", 1)}
        gantry = prm.get("gantry") if isinstance(prm.get("gantry"), dict) else {}
        if "run_blower" in gantry:
            op["air"] = bool(gantry.get("run_blower"))
        if _num(prm.get("interval"), 0) > 0:
            op["interval"] = _num(prm.get("interval"), 0) * dist
        elif _num(prm.get("dpi"), 0) > 0:
            op["interval"] = round(25.4 / _num(prm.get("dpi"), 254), 4)
        if prm.get("dither_mode"):
            op["dither"] = prm["dither_mode"]
        if _num(prm.get("kerf"), 0):
            op["kerf"] = _num(prm.get("kerf"), 0) * dist
        entries.append({"material": e.get("material") or "", "thick": thick, "label": label,
                        "type": ltype, "op": op})
    return entries


# ----------------------------------------------------------------------------
# LightBurn REST API (LightBurn 2.2 i nowsze): otwieranie projektu, biblioteka
# ----------------------------------------------------------------------------

LB_URL = "http://127.0.0.1:19520"
LB_APP_NAME = "xs2lightburn"
# state: potwierdzenie importu; project: biblioteka materialow; upload: pliki.
# Program nie czyta obrazu z kamery (GET /api/overlay), choc uprawnienie "project" go obejmuje.
LB_CAPS = ["state", "project", "upload"]


class LightBurnError(Exception):
    """Blad komunikacji z LightBurnem; tekst jest gotowy dla uzytkownika."""

    def __init__(self, msg, code=None):
        Exception.__init__(self, msg)
        self.code = code


class LightBurnAuthError(LightBurnError):
    pass


def lb_token(secret, now=None):
    """Token Bearer: hex(HMAC-SHA256(sekret, biezaca minuta Unix))."""
    import hashlib
    import hmac
    import time
    minute = int(time.time() if now is None else now) // 60
    return hmac.new(secret.encode("utf-8"), str(minute).encode("ascii"),
                    hashlib.sha256).hexdigest()


def lb_url(cfg=None):
    return str((cfg or {}).get("lightburn_url") or LB_URL).rstrip("/")


def _lb_open(req, timeout):
    import urllib.request
    # LightBurn dziala lokalnie: zmienne HTTP_PROXY z otoczenia nie moga przekierowac zapytan
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def _lb_err_text(payload):
    try:
        return str(json.loads(payload.decode("utf-8")).get("error") or "")[:200]
    except Exception:
        return ""


def lb_request(cfg, method, path, secret=None, body=None, headers=None, timeout=10):
    """Zapytanie do LightBurna; zwraca (status, bajty). Bledy jako LightBurnError."""
    import http.client
    import socket
    import urllib.error
    import urllib.request
    h = dict(headers or {})
    if secret:
        h["Authorization"] = "Bearer " + lb_token(secret)
    data = body
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(lb_url(cfg) + path, data=data, method=method, headers=h)
    try:
        with _lb_open(req, timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        detail = _lb_err_text(e.read() or b"")
        if e.code == 401:
            raise LightBurnAuthError(_("LightBurn odrzucił autoryzację."), 401)
        raise LightBurnError(_("LightBurn zwrócił błąd %d%s") % (
            e.code, (": " + detail) if detail else ""), e.code)
    except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        raise LightBurnError(_("Brak połączenia z LightBurnem (%s). Uruchom LightBurn 2.2 lub "
                             "nowszy; REST API jest w nim włączone domyślnie.") % reason)


def lb_json(cfg, method, path, secret=None, body=None, timeout=10):
    st, data = lb_request(cfg, method, path, secret, body, timeout=timeout)
    try:
        return json.loads(data.decode("utf-8")) if data else {}
    except ValueError:
        raise LightBurnError(_("LightBurn zwrócił nieoczekiwaną odpowiedź."))


LB_SECRET_PATH = Path.home() / ".xs2lightburn_lightburn.json"  # sekrety (jak hasla), osobno
_PAIR_LOCK = threading.Lock()  # jedno okno zgody naraz


def _secrets_load():
    try:
        raw = json.loads(LB_SECRET_PATH.read_text(encoding="utf-8"))
        sec = raw.get("secrets") if isinstance(raw, dict) else None
        return {k: v for k, v in (sec or {}).items() if isinstance(v, str) and v}
    except (OSError, ValueError, AttributeError):
        return {}


def lb_secret(cfg):
    """Zapamietany sekret dla adresu LightBurna z cfg (albo None)."""
    with _FILE_LOCK:
        return _secrets_load().get(lb_url(cfg))


def lb_set_secret(cfg, secret):
    """Zapisuje (albo usuwa, gdy secret=None) sekret dla adresu LightBurna."""
    with _FILE_LOCK:
        sec = _secrets_load()
        if secret:
            sec[lb_url(cfg)] = secret
        else:
            sec.pop(lb_url(cfg), None)
        _write_private(LB_SECRET_PATH, json.dumps({"secrets": sec}, indent=2), private=True)


def _lb_is_local(cfg):
    import urllib.parse
    host = (urllib.parse.urlsplit(lb_url(cfg)).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def lb_pair(cfg, log, old=None):
    """Parowanie: LightBurn pokazuje okno zgody (30 s). Zwraca sekret i go zapamietuje.
    old: sekret, ktory wlasnie nie zadzialal (inny watek mogl juz sparowac na nowo)."""
    with _PAIR_LOCK:
        cur = lb_secret(cfg)
        if cur and cur != old:
            return cur  # inny watek wlasnie sparowal
        if cur and cur == old:
            try:
                lb_set_secret(cfg, None)
            except OSError:
                pass
        log(_("LightBurn: zatwierdź połączenie programu xs2lightburn w oknie LightBurna (masz 30 s)."))
        try:
            res = lb_json(cfg, "POST", "/api/connect",
                          body={"application_name": LB_APP_NAME, "capabilities": LB_CAPS},
                          timeout=45)
        except LightBurnError as e:
            if e.code == 403 and not _lb_is_local(cfg):
                raise LightBurnError(_("połączenie z LightBurnem można zatwierdzić tylko na komputerze, "
                                     "na którym działa LightBurn."), 403)
            if e.code == 403:
                raise LightBurnError(_("połączenie nie zostało zatwierdzone w LightBurnie (odrzucone "
                                     "albo minęło 30 s)."), 403)
            raise
        secret = res.get("secret") if isinstance(res, dict) else None
        if not secret or not isinstance(secret, str):
            raise LightBurnError(_("LightBurn zwrócił nieoczekiwaną odpowiedź."))
        try:
            lb_set_secret(cfg, secret)
            log(_("LightBurn: połączenie zatwierdzone i zapamiętane."))
        except OSError as e:
            log(_("LightBurn: nie udało się zapamiętać połączenia (%s); przy następnym razie "
                  "LightBurn znowu poprosi o zgodę.") % friendly_error(e))
        return secret


def lb_call(cfg, log, fn):
    """fn(sekret) z automatycznym parowaniem. Przy 401: jeszcze raz z nowym tokenem (zmiana
    minuty), potem ponowne parowanie."""
    import time
    secret = lb_secret(cfg)
    if not secret:
        return fn(lb_pair(cfg, log))
    try:
        return fn(secret)
    except LightBurnAuthError:
        pass
    time.sleep(1)
    try:
        return fn(secret)
    except LightBurnAuthError:
        pass
    log(_("LightBurn: zapamiętane połączenie wygasło albo zostało cofnięte; łączę ponownie."))
    return fn(lb_pair(cfg, log, old=secret))


def lb_material_profiles(cfg, log):
    """Biblioteka wybrana w LightBurnie -> (profile, pominiete, nazwa biblioteki)."""
    def get(secret):
        units = lb_json(cfg, "GET", "/api/units", secret)
        lib = lb_json(cfg, "GET", "/api/material-library", secret)
        return units, lib
    units, lib = lb_call(cfg, log, get)
    name = str(lib.get("name") or "") if isinstance(lib, dict) else ""
    entries = api_library_entries(lib, units)
    if not entries:
        raise LightBurnError(_("W LightBurnie nie jest wybrana żadna biblioteka materiałów albo jest "
                             "pusta (okno Library, lista Select Library)."))
    profs, skipped = library_to_profiles(entries, name or "LightBurn",
                                         _("biblioteka LightBurna %s") % (name or "?"))
    return profs, skipped, name


class _LbEvents(object):
    """Strumien zdarzen (SSE) czekajacy na file_imported. Otwierany przed wyslaniem pliku."""

    def __init__(self, cfg, secret, timeout):
        import urllib.request
        self.result = None
        self.done = threading.Event()
        self.ready = threading.Event()
        self.resp = None
        req = urllib.request.Request(lb_url(cfg) + "/api/events", method="GET", headers={
            "Authorization": "Bearer " + lb_token(secret), "Accept": "text/event-stream"})
        self.thread = threading.Thread(target=self._run, args=(req, timeout), daemon=True)
        self.thread.start()

    def _run(self, req, timeout):
        try:
            self.resp = _lb_open(req, timeout)
            event, data = "", []
            while not self.done.is_set():
                raw = self.resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    self.ready.set()
                    continue
                if not line:
                    if event == "file_imported":
                        try:
                            self.result = json.loads("\n".join(data) or "{}")
                        except ValueError:
                            self.result = {}
                        break
                    event, data = "", []
                    continue
                self.ready.set()
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
        except Exception:
            pass
        finally:
            self.ready.set()
            self.done.set()

    def close(self):
        """Konczy nasluch bez czekania (zamkniecie w tle: readline moze jeszcze blokowac)."""
        self.done.set()
        resp = self.resp

        def _close():
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass

        threading.Thread(target=_close, daemon=True).start()


def lb_send(cfg, log, name, data, mode="open", wait=None):
    """Wysyla projekt .lbrn2 do LightBurna. mode: 'open' (zastepuje projekt) albo 'import'
    (wstawia do biezacego). Zwraca True (potwierdzone), False (blad) albo None (brak potwierdzenia)."""
    path = "/api/file/open" if mode == "open" else "/api/file/upload"
    if wait is None:
        # przy otwieraniu LightBurn moze najpierw zapytac o zapis biezacego projektu
        wait = 120 if mode == "open" else 30
    import unicodedata
    # naglowek HTTP: tylko ASCII (polskie litery bez ogonkow, reszta jako _)
    safe = "".join(c if 32 <= ord(c) < 127 else "_"
                   for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))

    def send(secret):
        ev = _LbEvents(cfg, secret, wait + 5)
        ev.ready.wait(3)
        try:
            st, resp = lb_request(cfg, "POST", path, secret, body=data, timeout=60, headers={
                "Content-Type": "application/octet-stream", "X-Filename": safe})
            if st != 202:
                try:
                    ok = json.loads(resp.decode("utf-8")).get("status") == "accepted"
                except Exception:
                    ok = False
                if not ok:
                    raise LightBurnError(_("LightBurn nie przyjął pliku (odpowiedź %d). Czy pod tym "
                                         "adresem działa LightBurn 2.2 lub nowszy?") % st)
            ev.done.wait(wait)
            return ev.result
        finally:
            ev.close()

    res = lb_call(cfg, log, send)
    if res is None:
        return None
    if not isinstance(res, dict):
        res = {}
    if res.get("success"):
        return True
    log(_("LightBurn: import nieudany: %s") % (res.get("error") or "?"))
    return False


def output_name(path, index, count, cv):
    """Nazwa pliku .lbrn2 dla planszy (tak samo jak przy zapisie na dysk)."""
    suffix = "" if count == 1 else "_%d_%s" % (index + 1, safe_stem(cv.file_name))
    return Path(path).stem + suffix + ".lbrn2"



# ----------------------------------------------------------------------------
# Podglad (dane, bez tkinter)
# ----------------------------------------------------------------------------

KIND_SHORT = {"image": "Obraz", "fill": "Wypełnienie", "line": "Linia", "cut": "Cięcie"}


def layer_row(L):
    """Wiersz tabeli podgladu dla warstwy planu."""
    sp = L["spec"]
    kind = sp["kind"]
    if kind == "fill" and sp.get("offset"):
        op = _("Wypełnienie offset")
    else:
        op = _(KIND_SHORT[kind])
    dens = ""
    if kind in ("fill", "image") and sp.get("interval"):
        dens = "%s mm (%s lpcm)" % (fmt(sp["interval"]), fmt(round(10.0 / sp["interval"], 1)))
    angle = ""
    if kind == "fill":
        angle = fmt(sp.get("angle", 0)) + (_("° + krzyż") if sp.get("cross") else "°")
    miss = sp.get("missing") or (sp.get("noop") and sp["off"])
    if sp.get("missing"):
        out = _("BRAK W BAZIE")
    elif miss:
        out = _("BRAK OPERACJI")
    else:
        out = _("NIE") if sp["off"] else _("tak")
    lay = "C%02d%s" % (L["idx"], " *" if L.get("edited") else "")
    if miss:
        return (lay, L["color"].upper(), op, "0", "0", "-", "-", "-", "-", "-",
                "-", "-", out, str(L["count"]))
    return (
        lay, L["color"].upper(), op,
        fmt(sp.get("min_power", sp["power"])), fmt(sp["power"]), fmt(sp["speed"]),
        str(sp["passes"]), dens, angle, sp.get("dither", "") if kind == "image" else "",
        fmt(sp["kerf"]) if sp.get("kerf") else "", _("nie") if sp.get("air", 1) == 0 else _("tak"),
        out, str(L["count"]),
    )


def _flatten_cubic(p0, c1, c2, p3, n):
    out = []
    for k in range(1, n + 1):
        t = k / float(n)
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * p3[1]))
    return out


def preview_geometry(plan):
    """Lista elementow do narysowania (wspolrzedne xTool, mm, os Y w dol):
    ('poly', idx, [punkty], zamknieta) albo ('img', idx, png, [4 narozniki], prosty_bool)."""
    out = []
    for it, spec, idx in plan.entries:
        if it.kind == "path":
            for sp in transform_subpaths(it.subpaths, it.matrix):
                pts = [sp.pts[0]]
                n = len(sp.pts)
                for k, (kind, c1, c2) in enumerate(sp.segs):
                    a = sp.pts[k]
                    b = sp.pts[(k + 1) % n]
                    if kind == "C":
                        L_ = math.dist(a, c1) + math.dist(c1, c2) + math.dist(c2, b)
                        pts.extend(_flatten_cubic(a, c1, c2, b, max(2, min(24, int(L_ / 0.5)))))
                    else:
                        pts.append(b)
                out.append(("poly", idx, pts, sp.closed))
        else:
            size = png_size(it.png) or (it.ow, it.oh)
            ow, oh = size
            if not ow or not oh:
                continue
            corners = [apply_mat(it.matrix, x, y) for x, y in ((0, 0), (ow, 0), (ow, oh), (0, oh))]
            a, b, c, d = it.matrix[:4]
            straight = abs(b) < 1e-9 and abs(c) < 1e-9 and a > 0 and d > 0
            out.append(("img", idx, it.png, corners, straight))
    return out


def analyze_file(path, log, flip_x=False, flip_y=True, material=None):
    """Do podgladu: lista (nazwa planszy, maszyna, plan, numer planszy w pliku).
    Numer planszy liczy wszystkie plansze, tak jak convert_file (klucz recznych zmian)."""
    res = []
    for i, cv in enumerate(read_project(Path(path), log)):
        plan = plan_canvas(cv, log, flip_x, flip_y, material)
        if plan is not None:
            res.append((cv.name, cv.machine, plan, i))
    return res


def _mix_white(color, k=0.65):
    r, g, b = _rgb(color)
    return "#%02x%02x%02x" % tuple(int(v + (255 - v) * k) for v in (r, g, b))


# ----------------------------------------------------------------------------
# Interfejs
# ----------------------------------------------------------------------------

def run_background(widget, fn, done):
    """fn() w osobnym watku; done(wynik, wyjatek) w watku okna (gdy okno jeszcze istnieje)."""
    res = {}

    def runner():
        try:
            res["ok"] = fn()
        except Exception as e:  # przekazywany do done()
            res["err"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    def poll():
        try:
            if not widget.winfo_exists():
                return
        except Exception:
            return
        if t.is_alive():
            widget.after(150, poll)
        else:
            done(res.get("ok"), res.get("err"))

    widget.after(150, poll)
    return t


def open_material_editor(root, db, on_saved, readonly=False, cfg=None):
    """Okno edycji bazy materialow. on_saved(db) wywolywane po zapisie.
    cfg: wspolna konfiguracja okna glownego (polaczenie z LightBurnem).
    Zwraca okno (atrybut _close: zamkniecie z pytaniem o zapis)."""
    import copy
    import tkinter as tk
    from tkinter import messagebox, ttk

    work = copy.deepcopy(db)
    win = tk.Toplevel(root)
    win.title(_("Baza materiałów: %s") % MATERIALS_PATH)
    sw, sh_ = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("%dx%d" % (min(1150, sw - 40), min(640, sh_ - 80)))
    state = {"dirty": False, "backed": False}

    top = ttk.Frame(win, padding=(8, 6))
    top.pack(fill="x")
    ttk.Label(top, text=note_text(work.get("note", "")), wraplength=1100, justify="left",
              foreground="#555").pack(anchor="w")

    cols = (_("Grupa"), _("Nazwa"), _("Grubość mm"), _("Cięcie"), _("Grawer linią"), _("Wypełnienie"), _("Obraz"))
    widths = (120, 290, 85, 150, 130, 150, 150)
    tf = ttk.Frame(win, padding=(8, 0))
    tf.pack(fill="both", expand=True)
    tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
    for c, w in zip(cols, widths):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="w" if c in (_("Grupa"), _("Nazwa")) else "center")
    sb = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    def op_txt(o):
        if not o:
            return "-"
        pw = fmt(o["max"]) if o["min"] == o["max"] else "%s-%s" % (fmt(o["min"]), fmt(o["max"]))
        t = "%s%% %s mm/s" % (pw, fmt(o["speed"]))
        if o.get("passes", 1) > 1:
            t += " x%d" % o["passes"]
        return t

    def refresh(select=None):
        tree.delete(*tree.get_children())
        work["profiles"].sort(key=profile_sort_key)
        for i, p in enumerate(work["profiles"]):
            tree.insert("", "end", iid=str(i), values=(
                p["group"], p["name"], fmt(p["thickness"]) if p["thickness"] else "-",
                op_txt(p["ops"].get("cut")), op_txt(p["ops"].get("line")),
                op_txt(p["ops"].get("fill")), op_txt(p["ops"].get("image"))))
            if select is not None and p["name"] == select:
                tree.selection_set(str(i))
                tree.see(str(i))
        if not readonly:
            win.title(_("Baza materiałów%s: %s") % (_(" (niezapisane zmiany)") if state["dirty"] else "",
                                                 MATERIALS_PATH))

    def selected():
        s = tree.selection()
        return work["profiles"][int(s[0])] if s else None

    def edit_dialog(prof, title):
        d = tk.Toplevel(win)
        d.title(title)
        d.transient(win)
        d.grab_set()
        f = ttk.Frame(d, padding=10)
        f.pack(fill="both", expand=True)
        v = {}
        r = 0
        for key, label in (("name", _("Nazwa")), ("group", _("Grupa")),
                           ("thickness", _("Grubość mm (0 = tylko grawer)"))):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w")
            v[key] = tk.StringVar(value=fmt(prof[key]) if key == "thickness" else prof[key])
            ttk.Entry(f, textvariable=v[key], width=50).grid(row=r, column=1, columnspan=8,
                                                              sticky="we", pady=1)
            r += 1
        hdr = (_("Operacja"), _("Używaj"), _("Prędkość mm/s"), _("Moc min %"), _("Moc max %"), _("Przejścia"),
               _("Odstęp linii mm"), "Dither", _("Dmuch"), _("Kerf mm"))
        for c, h in enumerate(hdr):
            ttk.Label(f, text=h, font=("TkDefaultFont", 9, "bold")).grid(row=r, column=c,
                                                                       padx=3, pady=(8, 2))
        r += 1
        opv = {}
        for k in OPS:
            o = prof["ops"].get(k) or {}
            e = {"use": tk.BooleanVar(value=bool(o)),
                 "speed": tk.StringVar(value=fmt(o.get("speed", 100))),
                 "min": tk.StringVar(value=fmt(o.get("min", 10))),
                 "max": tk.StringVar(value=fmt(o.get("max", 20))),
                 "passes": tk.StringVar(value=str(o.get("passes", 1))),
                 "interval": tk.StringVar(value=fmt(o.get("interval", 0.08))),
                 "dither": tk.StringVar(value=o.get("dither", "jarvis")),
                 "air": tk.BooleanVar(value=o.get("air", k in ("cut", "line"))),
                 "kerf": tk.StringVar(value=fmt(o.get("kerf", 0)))}
            opv[k] = e
            ttk.Label(f, text=_(OP_LABEL[k])).grid(row=r, column=0, sticky="w")
            ttk.Checkbutton(f, variable=e["use"]).grid(row=r, column=1)
            for c, key in enumerate(("speed", "min", "max", "passes"), start=2):
                ttk.Entry(f, textvariable=e[key], width=9).grid(row=r, column=c, padx=2)
            if k in ("fill", "image"):
                ttk.Entry(f, textvariable=e["interval"], width=9).grid(row=r, column=6, padx=2)
            if k == "image":
                ttk.Combobox(f, textvariable=e["dither"], width=10, state="readonly",
                             values=sorted(set(DITHER.values()))).grid(row=r, column=7, padx=2)
            ttk.Checkbutton(f, variable=e["air"]).grid(row=r, column=8)
            ttk.Entry(f, textvariable=e["kerf"], width=7).grid(row=r, column=9, padx=2)
            r += 1
        ttk.Label(f, text=_("Notatki")).grid(row=r, column=0, sticky="nw", pady=(8, 0))
        notes = tk.Text(f, height=5, width=80, wrap="word")
        notes.insert("1.0", prof.get("notes", ""))
        notes.grid(row=r, column=1, columnspan=9, sticky="we", pady=(8, 0))
        r += 1
        ttk.Label(f, text=_("Źródło")).grid(row=r, column=0, sticky="nw")
        src = tk.Text(f, height=3, width=80, wrap="word")
        src.insert("1.0", prof.get("source", ""))
        src.grid(row=r, column=1, columnspan=9, sticky="we")
        r += 1
        result = {}

        def ok():
            name = v["name"].get().strip()
            if not name:
                messagebox.showerror(_("Błąd"), _("Podaj nazwę materiału."), parent=d)
                return
            if any(p is not prof and p["name"] == name for p in work["profiles"]):
                messagebox.showerror(_("Błąd"), _("Materiał o tej nazwie już jest w bazie."), parent=d)
                return
            ops = {}
            for k, e in opv.items():
                if not e["use"].get():
                    continue
                raw = {"speed": e["speed"].get().replace(",", "."),
                       "min": e["min"].get().replace(",", "."),
                       "max": e["max"].get().replace(",", "."),
                       "passes": e["passes"].get(), "air": e["air"].get(),
                       "interval": e["interval"].get().replace(",", "."),
                       "dither": e["dither"].get(), "kerf": e["kerf"].get().replace(",", ".")}
                err = None
                try:
                    sp_, mn_, mx_ = float(raw["speed"]), float(raw["min"]), float(raw["max"])
                    raw["passes"] = int(raw["passes"])
                    kf_ = float(raw["kerf"] or 0)
                    iv_ = float(raw["interval"]) if k in ("fill", "image") else 0.1
                except ValueError:
                    err = _("wpisz liczby (dopuszczalny przecinek dziesiętny)")
                else:
                    if not 0 < sp_ <= 3000:
                        err = _("prędkość musi być w zakresie 0-3000 mm/s")
                    elif not (0 <= mn_ <= 100 and 0 <= mx_ <= 100):
                        err = _("moc musi być w zakresie 0-100%")
                    elif mn_ > mx_:
                        err = _("moc min nie może być większa od mocy max")
                    elif not 1 <= raw["passes"] <= 100:
                        err = _("liczba przejść 1-100")
                    elif not 0 < iv_ <= 2:
                        err = _("odstęp linii 0-2 mm")
                    elif abs(kf_) > 2:
                        err = _("kerf w zakresie -2..2 mm")
                c = None if err else _clean_op(raw, k)
                if c is None:
                    messagebox.showerror(_("Błąd"), "%s: %s." % (_(OP_LABEL[k]), err or _("złe wartości")),
                                         parent=d)
                    return
                ops[k] = c
            if not ops:
                messagebox.showerror(_("Błąd"), _("Zaznacz przynajmniej jedną operację."), parent=d)
                return
            try:
                th_ = float(v["thickness"].get().replace(",", ".") or 0)
                if not 0 <= th_ <= 200:
                    raise ValueError
            except ValueError:
                messagebox.showerror(_("Błąd"), _("Grubość: liczba 0-200 mm."), parent=d)
                return
            result["p"] = {"group": v["group"].get().strip() or _("Inne"), "name": name,
                           "thickness": th_,
                           "ops": ops, "notes": notes.get("1.0", "end").strip(),
                           "source": src.get("1.0", "end").strip()}
            d.destroy()

        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=10, sticky="e", pady=(10, 0))
        ttk.Button(bf, text=_("Anuluj"), command=d.destroy).pack(side="right")
        ttk.Button(bf, text="OK", command=ok).pack(side="right", padx=6)
        d.wait_window()
        return result.get("p")

    def add():
        p = edit_dialog({"group": _("Inne"), "name": "", "thickness": 3, "ops": {},
                         "notes": "", "source": _("Własny test")}, _("Nowy materiał"))
        if p:
            work["profiles"].append(p)
            state["dirty"] = True
            refresh(p["name"])

    def edit(_e=None):
        cur = selected()
        if not cur:
            return
        p = edit_dialog(cur, _("Edycja: %s") % cur["name"])
        if p:
            cur.clear()
            cur.update(p)
            state["dirty"] = True
            refresh(p["name"])

    def dup():
        cur = selected()
        if not cur:
            return
        p = copy.deepcopy(cur)
        base = p["name"] + _(" (kopia)")
        name, i = base, 2
        while any(x["name"] == name for x in work["profiles"]):
            name, i = "%s %d" % (base, i), i + 1
        p["name"] = name
        work["profiles"].append(p)
        state["dirty"] = True
        refresh(name)

    def delete():
        cur = selected()
        if cur and messagebox.askyesno(_("Usuń"), _("Usunąć materiał '%s'?") % cur["name"], parent=win):
            work["profiles"].remove(cur)
            state["dirty"] = True
            refresh()

    def restore():
        if not messagebox.askyesno(_("Przykłady"), _("Dopisać przykładowe materiały? Twoje materiały i zmiany "
                                   "zostają, dopisane zostaną tylko przykłady, których nazw "
                                   "nie ma w bazie."), parent=win):
            return
        have = {p["name"] for p in work["profiles"]}
        n = 0
        for p in _default_profiles():
            if p["name"] not in have:
                work["profiles"].append(clean_profile(p))
                n += 1
        state["dirty"] = state["dirty"] or n > 0
        refresh()
        messagebox.showinfo(_("Przykłady"), _("Dopisano %d materiałów.") % n, parent=win)

    def merge(other, sk, title):
        """Dodaje profile z importu; pyta o dublety nazw."""
        have = {p["name"]: p for p in work["profiles"]}
        dup = [p for p in other if p["name"] in have]
        overwrite = False
        if dup:
            ans = messagebox.askyesnocancel(
                title, _("%d materiałów o tych samych nazwach już jest w bazie.\n\n"
                         "Tak: zastąp je importowanymi.\nNie: dodaj importowane z "
                         "dopiskiem (import).") % len(dup), parent=win)
            if ans is None:
                return
            overwrite = ans
        n = 0
        for p in other:
            if p["name"] in have:
                if overwrite:
                    have[p["name"]].clear()
                    have[p["name"]].update(p)
                    n += 1
                    continue
                base = p["name"] + " (import)"
                name, i = base, 2
                while any(x["name"] == name for x in work["profiles"]):
                    name, i = "%s %d" % (base, i), i + 1
                p["name"] = name
            work["profiles"].append(p)
            n += 1
        state["dirty"] = state["dirty"] or n > 0
        refresh()
        msg = _("Zaimportowano %d materiałów.") % n
        if n and not readonly:
            msg += "\n" + _("Sprawdź wartości i kliknij Zapisz.")
        if sk:
            msg += "\n\n" + _("Pominięto błędne wpisy:") + "\n" + "\n".join(sk[:15])
            if len(sk) > 15:
                msg += "\n" + _("... i %d innych") % (len(sk) - 15)
        messagebox.showinfo(title, msg, parent=win)

    def import_db():
        from tkinter import filedialog
        fn = filedialog.askopenfilename(
            parent=win, title=_("Importuj materiały"),
            filetypes=[(_("Baza konwertera lub biblioteka LightBurna"), "*.json *.clb"),
                       (_("Baza konwertera"), "*.json"), (_("Biblioteka LightBurna"), "*.clb"),
                       (_("Wszystkie pliki"), "*.*")])
        if not fn:
            return
        sk = []
        try:
            if fn.lower().endswith(".clb"):
                other, sk = clb_to_profiles(fn)
            else:
                other = load_materials(path=fn, skipped=sk, create=False)["profiles"]
        except Exception as e:
            messagebox.showerror(_("Import"), friendly_error(e), parent=win)
            return
        if not other:
            messagebox.showinfo(_("Import"), _("W pliku nie ma materiałów do zaimportowania.") +
                                ("\n\n" + "\n".join(sk[:15]) if sk else ""), parent=win)
            return
        merge(other, sk, _("Import"))

    def import_lb():
        conf = cfg if cfg is not None else load_config()
        lb_btn.state(["disabled"])
        status.configure(text=_("Łączenie z LightBurnem... Przy pierwszym połączeniu zatwierdź je "
                                "w oknie LightBurna (masz 30 s)."))

        def done(res, err):
            lb_btn.state(["!disabled"])
            status.configure(text="")
            if err is not None:
                messagebox.showerror(_("Import z LightBurna"), friendly_error(err), parent=win)
                return
            profs, sk, name = res
            if not profs:
                messagebox.showinfo(_("Import z LightBurna"),
                                    _("W bibliotece nie ma materiałów do zaimportowania.") +
                                    ("\n\n" + "\n".join(sk[:15]) if sk else ""), parent=win)
                return
            merge(profs, sk, _("Import z LightBurna"))

        run_background(win, lambda: lb_material_profiles(conf, lambda m: None), done)

    def export_db():
        from tkinter import filedialog
        fn = filedialog.asksaveasfilename(parent=win, title=_("Eksportuj materiały"),
                                          defaultextension=".json",
                                          initialfile="xs2lightburn_materials.json",
                                          filetypes=[("JSON", "*.json")])
        if not fn:
            return
        try:
            save_materials(work, fn)
        except Exception as e:
            messagebox.showerror(_("Eksport"), friendly_error(e), parent=win)
            return
        messagebox.showinfo(_("Eksport"), _("Zapisano %d materiałów do:\n%s")
                            % (len(work["profiles"]), fn), parent=win)

    def save():
        if readonly:
            messagebox.showerror(_("Zapis zablokowany"), _("Plik bazy %s jest uszkodzony, więc zapis "
                                 "jest zablokowany, żeby go nie nadpisać. Popraw plik ręcznie "
                                 "albo usuń go i uruchom program ponownie (powstanie baza "
                                 "domyślna).") % MATERIALS_PATH, parent=win)
            return False
        try:
            if not state["backed"]:
                backup_materials()
                state["backed"] = True
            save_materials(work)
        except Exception as e:
            messagebox.showerror(_("Błąd zapisu"), friendly_error(e), parent=win)
            return False
        state["dirty"] = False
        refresh(selected()["name"] if selected() else None)
        on_saved(copy.deepcopy(work))
        return True

    def close():
        if state["dirty"] and not readonly:
            ans = messagebox.askyesnocancel(_("Niezapisane zmiany"), _("Zapisać zmiany w bazie?"),
                                            parent=win)
            if ans is None:
                return
            if ans and not save():
                return
        win.destroy()

    bar = ttk.Frame(win, padding=8)
    bar.pack(fill="x")
    for txt, cmd in ((_("Dodaj..."), add), (_("Edytuj..."), edit), (_("Duplikuj"), dup),
                     (_("Usuń"), delete), (_("Importuj..."), import_db)):
        ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=(0, 6))
    lb_btn = ttk.Button(bar, text=_("Z LightBurna..."), command=import_lb)
    lb_btn.pack(side="left", padx=(0, 6))
    for txt, cmd in ((_("Eksportuj..."), export_db), (_("Dopisz przykłady"), restore)):
        ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=(0, 6))
    ttk.Button(bar, text=_("Zamknij"), command=close).pack(side="right")
    ttk.Button(bar, text=_("Zapisz"), command=save).pack(side="right", padx=6)
    status = ttk.Label(win, padding=(8, 0, 8, 0), foreground="#0a5", text="")
    status.pack(anchor="w", fill="x")
    info = ttk.Label(win, padding=(8, 0, 8, 8), foreground="#555", wraplength=1100,
                     justify="left", text="")
    info.pack(anchor="w", fill="x")

    def show_info(_e=None):
        cur = selected()
        info.configure(text=(_("%s\nŹródło: %s") % (cur["notes"], cur["source"])) if cur else "")

    tree.bind("<<TreeviewSelect>>", show_info)
    tree.bind("<Double-Button-1>", edit)
    win.protocol("WM_DELETE_WINDOW", close)
    win._close = close
    win._import_lb = import_lb
    win._work = work
    refresh()
    if readonly:
        win.title(_("Baza materiałów (TYLKO PODGLĄD, plik uszkodzony): %s") % MATERIALS_PATH)
    return win


class PreviewCanvas:
    """Podglad z przesuwaniem (przeciaganie) i powiekszaniem (kolko myszy)."""

    def __init__(self, parent):
        import tkinter as tk
        try:
            from PIL import Image, ImageTk
            self.Image, self.ImageTk = Image, ImageTk
        except Exception:
            self.Image = self.ImageTk = None
        self.cvs = tk.Canvas(parent, background="white", highlightthickness=0)
        self.geom = []
        self.off = set()
        self.chosen = set()
        self.view = None          # (skala px/mm, ox, oy)
        self.bbox = None
        self.imgs = []
        self.decoded = {}
        self.after_id = None
        self.drag = None
        c = self.cvs
        c.bind("<Configure>", self._on_conf)
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<Double-Button-1>", lambda e: self.fit())
        c.bind("<MouseWheel>", lambda e: self._zoom(e.x, e.y, 1 if e.delta > 0 else -1))
        c.bind("<Button-4>", lambda e: self._zoom(e.x, e.y, 1))
        c.bind("<Button-5>", lambda e: self._zoom(e.x, e.y, -1))

    def set_data(self, geom, off, keep_view=False):
        self.geom = geom
        self.off = off
        self.decoded = {}
        xs, ys = [], []
        for g in geom:
            pts = g[2] if g[0] == "poly" else g[3]
            xs.extend(p[0] for p in pts)
            ys.extend(p[1] for p in pts)
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        if keep_view and self.view:
            self.redraw()
        else:
            self.fit()

    def clear(self):
        self.geom = []
        self.bbox = None
        self.cvs.delete("all")

    def fit(self):
        if not self.bbox:
            self.cvs.delete("all")
            return
        W = max(50, self.cvs.winfo_width())
        H = max(50, self.cvs.winfo_height())
        minx, miny, maxx, maxy = self.bbox
        m = 20
        s = min((W - 2 * m) / max(maxx - minx, 1e-6), (H - 2 * m) / max(maxy - miny, 1e-6))
        ox = m + ((W - 2 * m) - (maxx - minx) * s) / 2.0 - minx * s
        oy = m + ((H - 2 * m) - (maxy - miny) * s) / 2.0 - miny * s
        self.view = (s, ox, oy)
        self.redraw()

    def _on_conf(self, _e):
        if self.after_id:
            self.cvs.after_cancel(self.after_id)
        self.after_id = self.cvs.after(120, self.fit if self.view is None else self.redraw)

    def _press(self, e):
        self.drag = (e.x, e.y)

    def _motion(self, e):
        if not self.drag or not self.view:
            return
        dx, dy = e.x - self.drag[0], e.y - self.drag[1]
        self.drag = (e.x, e.y)
        s, ox, oy = self.view
        self.view = (s, ox + dx, oy + dy)
        self.cvs.move("all", dx, dy)
        self._schedule()

    def _zoom(self, x, y, step):
        if not self.view:
            return
        f = 1.25 if step > 0 else 0.8
        s, ox, oy = self.view
        if not 1e-3 < s * f < 2000:
            return
        self.view = (s * f, x - (x - ox) * f, y - (y - oy) * f)
        self.cvs.scale("all", x, y, f, f)
        self._schedule()

    def _schedule(self):
        if self.after_id:
            self.cvs.after_cancel(self.after_id)
        self.after_id = self.cvs.after(150, self.redraw)

    def _image(self, png, box, size):
        """Wycinek obrazu (box w pikselach zrodla) przeskalowany do size."""
        import io
        im = self.decoded.get(id(png))
        if im is None:
            im = self.Image.open(io.BytesIO(png)).convert("RGBA")
            self.decoded[id(png)] = im
        return self.ImageTk.PhotoImage(im.crop(box).resize(size))

    def redraw(self):
        c = self.cvs
        c.delete("all")
        self.imgs = []
        if not self.geom or not self.view:
            return
        s, ox, oy = self.view
        W, H = c.winfo_width(), c.winfo_height()

        def T(p):
            return (ox + p[0] * s, oy + p[1] * s)

        for g in self.geom:
            idx = g[1]
            dim = bool(self.chosen) and idx not in self.chosen
            off = idx in self.off
            color = "#d8d8d8" if dim else ("#9a9a9a" if off else LB_PALETTE[idx])
            if g[0] == "poly":
                pts = g[2] + ([g[2][0]] if g[3] else [])
                flat = [v for p in pts for v in T(p)]
                if len(flat) >= 4:
                    c.create_line(*flat, fill=color, width=(2 if self.chosen and not dim else 1),
                                  dash=(4, 3) if off else None)
                continue
            corners = [T(p) for p in g[3]]
            drawn = False
            if self.Image is not None and g[4] and not dim:
                x0, y0 = corners[0]
                x1, y1 = corners[2]
                vx0, vy0, vx1, vy1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
                w, h = int(round(vx1 - vx0)), int(round(vy1 - vy0))
                if w >= 2 and h >= 2 and x1 > x0 and y1 > y0:
                    try:
                        size = png_size(g[2])
                        if not size:
                            if id(g[2]) not in self.decoded:
                                import io
                                self.decoded[id(g[2])] = self.Image.open(
                                    io.BytesIO(g[2])).convert("RGBA")
                            size = self.decoded[id(g[2])].size
                        ow, oh = size
                        box = (int((vx0 - x0) / (x1 - x0) * ow), int((vy0 - y0) / (y1 - y0) * oh),
                               max(1, int(math.ceil((vx1 - x0) / (x1 - x0) * ow))),
                               max(1, int(math.ceil((vy1 - y0) / (y1 - y0) * oh))))
                        # polozenie i rozmiar z calych pikseli wycinka (bez przesuniecia)
                        kx, ky = (x1 - x0) / ow, (y1 - y0) / oh
                        px0, py0 = x0 + box[0] * kx, y0 + box[1] * ky
                        pw_ = max(1, int(round((box[2] - box[0]) * kx)))
                        ph_ = max(1, int(round((box[3] - box[1]) * ky)))
                        if box[2] > box[0] and box[3] > box[1] and pw_ <= 8000 and ph_ <= 8000:
                            ph = self._image(g[2], box, (pw_, ph_))
                            self.imgs.append(ph)
                            c.create_image(int(round(px0)), int(round(py0)), image=ph, anchor="nw")
                            drawn = True
                    except Exception:
                        drawn = False
            flat = [v for p in corners + [corners[0]] for v in p]
            c.create_line(*flat, fill=color, dash=(4, 3) if off else None)
            if not drawn:
                c.create_line(*corners[0], *corners[2], fill=color)
                c.create_line(*corners[1], *corners[3], fill=color)


def build_gui(root, on_lang=None, keep=None):
    """Buduje okno glowne; zwraca slownik z elementami.
    on_lang(app): wywolywane po zmianie jezyka (przebudowa okna); keep: stan do odtworzenia."""
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_config()
    root.title(_("xs2lightburn %s  (xTool Studio / RDWorks -> LightBurn)") % VERSION)
    sw, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
    if keep and keep.get("geometry"):
        root.geometry(keep["geometry"])  # przebudowa po zmianie jezyka: ten sam rozmiar okna
    else:
        root.geometry("%dx%d" % (min(1500, sw - 40), min(900, sh_ - 80)))
    files = []
    q = queue.Queue()
    app = {"cfg": cfg, "files": files, "q": q, "plans": {}, "overrides": {},
           "current": None, "canvas_i": 0, "busy": set(), "gen": 0, "shown": None,
           "dirty": False, "alive": True}

    pw = ttk.Panedwindow(root, orient="horizontal")
    pw.pack(fill="both", expand=True)
    frm = ttk.Frame(pw, padding=8)
    right = ttk.Panedwindow(pw, orient="vertical")
    pw.add(frm, weight=1)
    pw.add(right, weight=3)

    # ---------------- lewa kolumna
    lrow = ttk.Frame(frm)
    lrow.pack(fill="x", pady=(0, 6))
    ttk.Label(lrow, text="Language / Język:").pack(side="left")
    langs = available_languages()
    langvar = tk.StringVar(value=dict(langs)[LANG])
    lcb = ttk.Combobox(lrow, textvariable=langvar, state="readonly",
                       width=max(10, max(len(n) for _c, n in langs) + 2),
                       values=[n for _c, n in langs])
    lcb.pack(side="left", padx=6)

    def on_lang_change(_e=None):
        code = {n: c for c, n in langs}[langvar.get()]
        if code == LANG:
            return
        if app.get("ops"):
            langvar.set(dict(langs)[LANG])
            messagebox.showinfo(_("Język"), _("Poczekaj na koniec konwersji albo wysyłki do "
                                             "LightBurna."))
            return
        edw = mstate.get("editor")
        if edw is not None and edw.winfo_exists():
            edw._close()
            if edw.winfo_exists():
                langvar.set(dict(langs)[LANG])
                return
        set_lang(code)
        cfg["lang"] = code
        save_config(cfg)
        if on_lang is not None:
            app["alive"] = False
            on_lang(app)

    lcb.bind("<<ComboboxSelected>>", on_lang_change)

    ttk.Label(frm, text=_("Pliki (.xs, .xcs, .rld); kliknij plik, żeby go podejrzeć:")).pack(anchor="w")
    lb = tk.Listbox(frm, height=8, selectmode="extended", exportselection=False)
    lb.pack(fill="x")

    def refresh():
        lb.delete(0, "end")
        for f in files:
            lb.insert("end", Path(f).name)

    def add_paths(sel, select=True):
        for f in sel:
            if f not in files:
                files.append(f)
        refresh()
        if select and files and not lb.curselection():
            lb.selection_set(0)
            on_select()

    def add_files():
        sel = filedialog.askopenfilenames(
            title=_("Wybierz pliki projektów"),
            initialdir=cfg.get("in_dir") or str(Path.home()),
            filetypes=[(_("Projekty xTool i RDWorks"), "*.xs *.xcs *.rld"),
                       (_("Wszystkie pliki"), "*.*")])
        if sel:
            cfg["in_dir"] = str(Path(sel[0]).parent)
            save_config(cfg)
            add_paths(sel)

    def remove_sel():
        for i in reversed(lb.curselection()):
            f = files.pop(i)
            for k in [k for k in app["overrides"] if k[0] == f]:
                del app["overrides"][k]
            if f not in files:
                for k in [k for k in app["plans"] if k[0] == f]:
                    del app["plans"][k]
        refresh()
        app["current"] = None
        show_current()

    bar = ttk.Frame(frm)
    bar.pack(fill="x", pady=4)
    ttk.Button(bar, text=_("Dodaj pliki..."), command=add_files).pack(side="left")
    ttk.Button(bar, text=_("Usuń zaznaczone"), command=remove_sel).pack(side="left", padx=6)

    ttk.Label(frm, text=_("Folder zapisu (puste = ten sam folder co plik źródłowy):")).pack(
        anchor="w", pady=(8, 0))
    keep = keep or {}
    outvar = tk.StringVar(value=keep.get("out_dir", cfg.get("out_dir", "")))
    orow = ttk.Frame(frm)
    orow.pack(fill="x")
    ttk.Entry(orow, textvariable=outvar).pack(side="left", fill="x", expand=True)

    def pick_out():
        d = filedialog.askdirectory(
            title=_("Folder zapisu"),
            initialdir=outvar.get() or cfg.get("in_dir") or str(Path.home()))
        if d:
            outvar.set(d)

    ttk.Button(orow, text=_("Wybierz..."), command=pick_out).pack(side="left", padx=6)

    NO_MAT = _("(bez zmian: parametry z projektu)")
    skipped = []
    broken = keep.get("broken", False)
    try:
        if "db" in keep:  # przebudowa okna (zmiana jezyka): bez ponownego wczytania i ostrzezen
            db = keep["db"]
        else:
            db = load_materials(skipped=skipped)
    except Exception as e:
        broken = True
        messagebox.showerror(_("Baza materiałów"), friendly_error(e) + _("\n\nProgram działa bez "
                             "bazy; edytor nie zapisze zmian, dopóki plik jest uszkodzony."))
        db = {"version": DB_VERSION, "note": _(DB_NOTE), "profiles": []}
    if skipped:
        messagebox.showwarning(_("Baza materiałów"), _("Pominięto błędne wpisy w %s:\n\n%s\n\n"
                               "Zapis w edytorze usunie je z pliku (wcześniej powstanie kopia "
                               "%s.bak).") % (MATERIALS_PATH, "\n".join(skipped[:15]),
                                             MATERIALS_PATH.name))
    mstate = {"db": db, "editor": None, "labels": {}}
    ttk.Label(frm, text=_("Materiał (parametry z bazy dla Twojego lasera):")).pack(
        anchor="w", pady=(8, 0))
    mrow = ttk.Frame(frm)
    mrow.pack(fill="x")
    matvar = tk.StringVar()
    mcb = ttk.Combobox(mrow, textvariable=matvar, state="readonly")
    mcb.pack(side="left", fill="x", expand=True)

    def fill_materials(announce=False):
        labels = {}
        for p in sorted(mstate["db"]["profiles"], key=profile_sort_key):
            labels["%s  |  %s" % (p["group"], p["name"])] = p["name"]
        mstate["labels"] = labels
        mcb.configure(values=[NO_MAT] + list(labels))
        want = cfg.get("material", "")
        cur = [lab for lab, nm in labels.items() if nm == want]
        matvar.set(cur[0] if cur else NO_MAT)
        if want and not cur and announce:
            q.put(("log", _("UWAGA: zapamiętanego materiału '%s' nie ma w bazie; wybrano "
                          "'bez zmian'.") % want))

    def current_material():
        nm = mstate["labels"].get(matvar.get())
        return find_profile(mstate["db"], nm) if nm else None

    def on_mat(_e=None):
        p = current_material()
        cfg["material"] = p["name"] if p else ""
        save_config(cfg)
        show_current()

    def saved_db(newdb):
        mstate["db"] = newdb
        app["plans"].clear()
        app["gen"] += 1
        if any(any(v.values()) for v in app["overrides"].values()):
            app["overrides"].clear()
            app["dirty"] = False
            q.put(("log", _("Zmieniono bazę materiałów: ręczne zmiany warstw wyczyszczone "
                          "(numery warstw mogły się zmienić).")))
        fill_materials()
        show_current()

    def open_editor():
        ed = mstate.get("editor")
        if ed is not None and ed.winfo_exists():
            ed.lift()
            ed.focus_force()
            return
        mstate["editor"] = open_material_editor(root, mstate["db"], saved_db, readonly=broken,
                                                cfg=cfg)

    mcb.bind("<<ComboboxSelected>>", on_mat)
    ttk.Button(mrow, text=_("Edytuj bazę..."), command=open_editor).pack(side="left", padx=6)

    mx0, my0 = mirror_settings(cfg)
    mxv = tk.BooleanVar(value=mx0)
    myv = tk.BooleanVar(value=my0)
    owv = tk.BooleanVar(value=keep.get("overwrite", cfg.get("overwrite", False)))
    opt = ttk.Frame(frm)
    opt.pack(fill="x", pady=(8, 0))

    def flips():
        return mirror_to_flip(mxv.get(), myv.get())

    def on_flip():
        cfg["mirror_x"] = bool(mxv.get())
        cfg["mirror_y"] = bool(myv.get())
        cfg.pop("flip_x", None)
        cfg.pop("flip_y", None)
        save_config(cfg)
        show_current()

    ttk.Label(opt, text=_("Urządzenie LightBurn:")).pack(side="left")
    ttk.Checkbutton(opt, text="Mirror X", variable=mxv, command=on_flip).pack(side="left", padx=6)
    ttk.Checkbutton(opt, text="Mirror Y", variable=myv, command=on_flip).pack(side="left")
    ttk.Label(frm, text=_("Zaznacz tak, jak w LightBurnie: Edit > Device Settings > Mirror X / "
                        "Mirror Y. Jeśli projekt otwiera się odbity, zmień te opcje."),
              foreground="#555", wraplength=380, justify="left").pack(anchor="w")
    ttk.Checkbutton(frm, text=_("Nadpisuj istniejące pliki .lbrn2\n(inaczej zapis jako "
                              "'nazwa (2).lbrn2')"), variable=owv).pack(anchor="w", pady=(4, 0))

    btn = ttk.Button(frm, text=_("Konwertuj wszystkie pliki z listy"))
    btn.pack(side="bottom", fill="x", pady=(6, 0))
    lbrow = ttk.Frame(frm)
    lbrow.pack(side="bottom", fill="x", pady=(6, 0))
    lb_open_btn = ttk.Button(lbrow, text=_("Otwórz w LightBurnie"))
    lb_open_btn.pack(side="left", fill="x", expand=True)
    lb_imp_btn = ttk.Button(lbrow, text=_("Wstaw do projektu w LightBurnie"))
    lb_imp_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))
    lf = ttk.Frame(frm)
    lf.pack(fill="both", expand=True, pady=(8, 0))
    log_box = tk.Text(lf, height=10, width=48, wrap="word", state="disabled")
    sb = ttk.Scrollbar(lf, orient="vertical", command=log_box.yview)
    log_box.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    log_box.pack(side="left", fill="both", expand=True)

    # ---------------- prawa strona: podglad
    top = ttk.Frame(right)
    right.add(top, weight=3)
    tbar = ttk.Frame(top, padding=(6, 4))
    tbar.pack(fill="x")
    info = ttk.Label(tbar, text=_("Dodaj pliki i kliknij plik na liście."))
    info.pack(side="left")
    ttk.Button(tbar, text=_("Dopasuj widok"), command=lambda: prev.fit()).pack(side="right")
    cvsel = ttk.Combobox(tbar, state="readonly", width=24)
    ttk.Label(top, text=_("Kółko myszy: powiększ / pomniejsz.  Przeciągnij: przesuń.  "
                        "Dwuklik: dopasuj.  Układ jak w programie źródłowym."),
              foreground="#555", padding=(6, 0)).pack(anchor="w")
    prev = PreviewCanvas(top)
    prev.cvs.pack(fill="both", expand=True)

    # ---------------- prawa strona: warstwy i edycja
    bot = ttk.Frame(right, padding=(6, 4))
    right.add(bot, weight=2)
    cols = (_("Warstwa"), _("Kolor źródła"), _("Operacja"), _("Moc min %"), _("Moc max %"), "mm/s",
            _("Przejścia"), _("Odstęp linii"), _("Kąt"), "Dither", _("Kerf mm"), _("Dmuch"), _("Wyjście"),
            _("Elementy"))
    widths = (65, 75, 105, 72, 72, 55, 65, 140, 65, 65, 60, 50, 110, 65)
    tf = ttk.Frame(bot)
    tf.pack(fill="both", expand=True)
    tree = ttk.Treeview(tf, columns=cols, show="headings", height=6, selectmode="extended")
    for c_, w_ in zip(cols, widths):
        tree.heading(c_, text=c_)
        tree.column(c_, width=w_, minwidth=40, anchor="center", stretch=False)
    tsb = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)
    thb = ttk.Scrollbar(tf, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=tsb.set, xscrollcommand=thb.set)
    tsb.pack(side="right", fill="y")
    thb.pack(side="bottom", fill="x")
    tree.pack(side="left", fill="both", expand=True)
    ttk.Label(bot, foreground="#555", wraplength=1000, justify="left",
              text=_("Warstwa = numer w LightBurnie, * = zmienione ręcznie. Kolory na rysunku = "
                   "kolory warstw LightBurna, szare przerywane = wyjście wyłączone. Zaznacz "
                   "warstwę, zmień parametry poniżej i kliknij Zastosuj. Zmiany dotyczą tylko "
                   "tego pliku i obecnego wyboru materiału; po zmianie pliku źródłowego "
                   "trzeba je zrobić od nowa.")).pack(anchor="w", pady=(4, 2))
    ed = ttk.LabelFrame(bot, text=_("Parametry zaznaczonej warstwy"), padding=6)
    ed.pack(fill="x")
    ev = {k: tk.StringVar() for k in ("min_power", "power", "speed", "passes", "interval",
                                      "dither", "kerf")}
    airv = tk.BooleanVar()
    outv = tk.BooleanVar()
    labels = (("min_power", _("Moc min %")), ("power", _("Moc max %")), ("speed", _("Prędkość mm/s")),
              ("passes", _("Przejścia")), ("interval", _("Odstęp linii mm")), ("kerf", _("Kerf mm")))
    widgets = {}
    for c_, (k, lab) in enumerate(labels):
        ttk.Label(ed, text=lab).grid(row=0, column=c_, padx=4, sticky="w")
        widgets[k] = ttk.Entry(ed, textvariable=ev[k], width=10)
        widgets[k].grid(row=1, column=c_, padx=4)
    ttk.Label(ed, text="Dither").grid(row=0, column=6, padx=4, sticky="w")
    widgets["dither"] = ttk.Combobox(ed, textvariable=ev["dither"], width=11, state="readonly",
                                     values=sorted(set(DITHER.values())))
    widgets["dither"].grid(row=1, column=6, padx=4)
    ttk.Checkbutton(ed, text=_("Dmuch"), variable=airv).grid(row=1, column=7, padx=6)
    ttk.Checkbutton(ed, text=_("Wyjście (wypalaj)"), variable=outv).grid(row=1, column=8, padx=6)
    ebar = ttk.Frame(ed)
    ebar.grid(row=2, column=0, columnspan=9, sticky="w", pady=(6, 0))
    apply_btn = ttk.Button(ebar, text=_("Zastosuj"))
    apply_btn.pack(side="left")
    reset_btn = ttk.Button(ebar, text=_("Przywróć tę warstwę"))
    reset_btn.pack(side="left", padx=6)
    reset_all_btn = ttk.Button(ebar, text=_("Przywróć wszystkie warstwy tej planszy"))
    reset_all_btn.pack(side="left")
    edit_info = ttk.Label(ebar, text="", foreground="#555")
    edit_info.pack(side="left", padx=10)

    # ---------------- logika
    def log(msg):
        q.put(("log", msg))

    def fstat(f):
        try:
            st = os.stat(f)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def context():
        """Klucz planu podgladu (plik, material, odbicia, stan pliku, wersja bazy)."""
        m = current_material()
        f = app["current"]
        fx_, fy_ = flips()
        return (f, m["name"] if m else "", fx_, fy_,
                fstat(f) if f else None, app["gen"])

    def ov_key(ctx):
        """Klucz recznych zmian: plik, material i stan pliku (odbicia nie zmieniaja warstw)."""
        return (ctx[0], ctx[1], ctx[4])

    def ov_for(ctx, canvas_real):
        return app["overrides"].get(ov_key(ctx), {}).get(canvas_real, {})

    def on_select(_e=None):
        cur = lb.curselection()
        if not cur:
            return
        app["current"] = files[cur[0]]
        app["canvas_i"] = 0
        show_current()

    def show_empty(text):
        info.configure(text=text)
        tree.delete(*tree.get_children())
        cvsel.pack_forget()
        prev.clear()
        app["shown"] = None
        app["layers"] = {}
        load_fields()

    def show_current(keep_view=False):
        f = app["current"]
        if not f:
            show_empty(_("Dodaj pliki i kliknij plik na liście."))
            return
        ctx = context()
        res = app["plans"].get(ctx)
        if res is None:
            show_empty(_("Wczytywanie: %s ...") % Path(f).name)
            if ctx in app["busy"]:
                return
            app["busy"].add(ctx)
            mat = current_material()
            threading.Thread(target=analyze_work, args=(ctx, f, mat), daemon=True).start()
            return
        if not res:
            show_empty(_("%s: brak elementów do pokazania (szczegóły w logu)") % Path(f).name)
            return
        if len(res) > 1:
            cvsel.configure(values=["%d. %s" % (i + 1, r[0]) for i, r in enumerate(res)])
            cvsel.current(min(app["canvas_i"], len(res) - 1))
            cvsel.pack(side="right", padx=6)
        else:
            cvsel.pack_forget()
        ci = min(app["canvas_i"], len(res) - 1)
        name, machine, plan, real_i = res[ci]
        ov = ov_for(ctx, real_i)
        layers = [effective_layer(L, ov.get(L["idx"])) for L in plan.layers]
        info.configure(text=_("%s    Maszyna: %s    Materiał: %s    Warstw: %d")
                       % (Path(f).name, machine or "?", ctx[1] or _("nie wybrano (parametry z projektu)"),
                          len(layers)))
        sel = tree.selection()
        tree.delete(*tree.get_children())
        for L in layers:
            tag = "c%02d" % L["idx"]
            tree.tag_configure(tag, background=_mix_white(LB_PALETTE[L["idx"]]))
            tree.insert("", "end", iid=str(L["idx"]), values=layer_row(L), tags=(tag,))
        keep = [s_ for s_ in sel if tree.exists(s_)]
        if keep:
            tree.selection_set(keep)
        app["layers"] = {L["idx"]: L for L in layers}
        app["shown"] = (ctx, real_i)
        prev.chosen = {int(x) for x in keep}
        prev.set_data(preview_geometry(plan), {L["idx"] for L in layers if L["spec"]["off"]},
                      keep_view=keep_view)
        load_fields()

    def analyze_work(ctx, f, mat):
        log(_("Podgląd: %s (materiał: %s)") % (Path(f).name, ctx[1] or _("brak, parametry z projektu")))
        try:
            res = analyze_file(f, log, ctx[2], ctx[3], mat)
        except Exception as e:
            log(_("BŁĄD (%s): %s") % (Path(f).name, friendly_error(e)))
            res = []
        q.put(("plan", (ctx, res)))

    def load_fields():
        sel = tree.selection()
        L = app.get("layers", {}).get(int(sel[0])) if len(sel) == 1 else None
        state_ = "normal" if L else "disabled"
        for w in (apply_btn, reset_btn):
            w.state(["!disabled"] if L else ["disabled"])
        shown = app.get("shown")
        has_ov = bool(shown and ov_for(shown[0], shown[1]))
        reset_all_btn.state(["!disabled"] if has_ov else ["disabled"])
        if not L:
            for v in ev.values():
                v.set("")
            edit_info.configure(text=_("Zaznacz jedną warstwę w tabeli.") if not sel
                                else _("Edycja działa dla jednej warstwy naraz."))
            for k, w in widgets.items():
                w.configure(state="disabled")
            return
        sp = L["spec"]
        kind = sp["kind"]
        ev["power"].set(fmt(sp["power"]))
        ev["min_power"].set(fmt(sp.get("min_power", sp["power"])))
        ev["speed"].set(fmt(sp["speed"]))
        ev["passes"].set(str(sp["passes"]))
        ev["interval"].set(fmt(sp["interval"]) if kind in ("fill", "image") else "")
        ev["dither"].set(sp.get("dither", "") if kind == "image" else "")
        ev["kerf"].set(fmt(sp.get("kerf", 0)))
        airv.set(sp.get("air", 1) != 0)
        outv.set(not sp["off"])
        for k, w in widgets.items():
            if k == "interval":
                w.configure(state="normal" if kind in ("fill", "image") else "disabled")
            elif k == "dither":
                w.configure(state="readonly" if kind == "image" else "disabled")
            else:
                w.configure(state=state_)
        edit_info.configure(text="C%02d: %s" % (L["idx"], _(KIND_SHORT[kind])))

    def on_tree(_e=None):
        prev.chosen = {int(x) for x in tree.selection()}
        prev.redraw()
        load_fields()

    def apply_edit():
        sel = tree.selection()
        if len(sel) != 1:
            return
        idx = int(sel[0])
        L = app["layers"][idx]
        raw = {k: v.get() for k, v in ev.items()}
        raw["air"] = airv.get()
        raw["output"] = outv.get()
        shown = app.get("shown")
        if not shown or shown[0] != context():
            messagebox.showinfo(_("Parametry warstwy"), _("Plik lub ustawienia się zmieniły; podgląd "
                                "wczytuje się ponownie. Wprowadź zmianę jeszcze raz."))
            show_current()
            return
        fields, err = validate_layer_fields(L["spec"]["kind"], raw)
        if err:
            messagebox.showerror(_("Parametry warstwy"), err)
            return
        ctx, real_i = shown
        app["overrides"].setdefault(ov_key(ctx), {}).setdefault(real_i, {})[idx] = fields
        app["dirty"] = True
        app["edit_gen"] = app.get("edit_gen", 0) + 1
        log(_("Zmieniono ręcznie C%02d w %s") % (idx, Path(ctx[0]).name))
        show_current(keep_view=True)

    def reset_edit(all_=False):
        shown = app.get("shown")
        if not shown or shown[0] != context():
            return
        ctx, real_i = shown
        per = app["overrides"].get(ov_key(ctx), {}).get(real_i, {})
        if all_:
            per.clear()
        else:
            for s_ in tree.selection():
                per.pop(int(s_), None)
        app["dirty"] = any(any(v.values()) for v in app["overrides"].values())
        app["edit_gen"] = app.get("edit_gen", 0) + 1
        show_current(keep_view=True)

    def on_canvas_sel(_e=None):
        app["canvas_i"] = cvsel.current()
        show_current()

    app["ov_key"] = ov_key

    lb.bind("<<ListboxSelect>>", on_select)
    tree.bind("<<TreeviewSelect>>", on_tree)
    cvsel.bind("<<ComboboxSelected>>", on_canvas_sel)
    apply_btn.configure(command=apply_edit)
    reset_btn.configure(command=lambda: reset_edit(False))
    reset_all_btn.configure(command=lambda: reset_edit(True))

    def work(jobs, out, fx, fy, ow, mat, gen_):
        total = 0
        errors = 0
        try:
            for f, ov in jobs:
                try:
                    total += len(convert_file(f, out or None, log, fx, fy, ow, mat, ov))
                except Exception as e:
                    errors += 1
                    log(_("BŁĄD (%s): %s") % (Path(f).name, friendly_error(e)))
            log(_("Gotowe. Zapisanych plików .lbrn2: %d") % total)
        finally:
            q.put(("done", (gen_, errors)))

    def start():
        if app.get("ops"):
            return
        if not files:
            messagebox.showinfo("xs2lightburn", _("Najpierw dodaj pliki."))
            return
        cfg["out_dir"] = outvar.get().strip()
        cfg["overwrite"] = bool(owv.get())
        save_config(cfg)
        mat = current_material()
        mname = mat["name"] if mat else ""
        fx, fy = flips()
        import copy
        jobs = []
        for f in files:
            ov = app["overrides"].get((f, mname, fstat(f)))
            stale = [k for k, v in app["overrides"].items()
                     if k[0] == f and k[1] == mname and k[2] != fstat(f) and any(v.values())]
            if stale and not ov:
                log(_("UWAGA: %s zmienił się na dysku po ręcznej edycji warstw; te zmiany "
                    "pominięto") % Path(f).name)
            jobs.append((f, copy.deepcopy(ov) if ov and any(ov.values()) else None))
        busy(1)
        app["edit_gen"] = app.get("edit_gen", 0)
        try:
            threading.Thread(target=work, daemon=True,
                             args=(jobs, cfg["out_dir"], fx, fy, cfg["overwrite"], mat,
                                   app["edit_gen"])).start()
        except Exception:
            busy(-1)
            raise

    btn.configure(command=start)

    def busy(delta):
        """Jeden licznik zajetosci: konwersja i wysylka do LightBurna blokuja te same przyciski
        oraz zmiane jezyka."""
        app["ops"] = max(0, app.get("ops", 0) + delta)
        if not app["alive"]:
            return
        for b in (btn, lb_open_btn, lb_imp_btn):
            b.state(["disabled"] if app["ops"] else ["!disabled"])

    def lb_action(mode):
        """Wysyla pokazana plansze (z biezacym materialem, odbiciami i zmianami warstw)."""
        import copy
        if app.get("ops"):
            return
        shown = app.get("shown")
        if not shown or shown[0] != context():
            messagebox.showinfo("xs2lightburn", _("Kliknij plik na liście i poczekaj na podgląd."))
            return
        ctx, real_i = shown
        f = ctx[0]
        mat = current_material()
        fx, fy = flips()
        ov = ov_for(ctx, real_i)
        ov = copy.deepcopy(ov) if ov else None
        busy(1)

        def job():
            canvases = read_project(Path(f), log)
            cv = canvases[real_i]
            xml, nshapes, _plan = convert_canvas(cv, log, fx, fy, mat, ov)
            if not nshapes:
                raise ValueError(_("plansza nie ma elementów do wysłania"))
            name = output_name(f, real_i, len(canvases), cv)
            log(_("LightBurn: wysyłam %s ...") % name)
            if mode == "open":
                log(_("  Jeśli bieżący projekt w LightBurnie ma niezapisane zmiany, LightBurn "
                      "zapyta o zapis."))
            return name, lb_send(cfg, log, name, xml.encode("utf-8"), mode)

        def done(res, err):
            busy(-1)
            if err is not None:
                log(_("LightBurn: %s") % friendly_error(err))
                return
            name, r = res
            if r is True:
                log(_("LightBurn: otwarto %s") % name if mode == "open" else
                    _("LightBurn: wstawiono %s do bieżącego projektu") % name)
            elif r is None:
                log(_("LightBurn: wysłano %s (LightBurn nie potwierdził importu; sprawdź okno "
                      "LightBurna)") % name)
            if r is not False:
                log(_("  Plik nie jest zapisany na dysku: zapisz projekt w LightBurnie "
                      "(File > Save) albo użyj przycisku konwersji."))

        log(_("LightBurn: przygotowuję %s ...") % Path(f).name)
        try:
            run_background(root, job, done)
        except Exception:
            busy(-1)
            raise

    lb_open_btn.configure(command=lambda: lb_action("open"))
    lb_imp_btn.configure(command=lambda: lb_action("import"))

    def pump():
        if not app["alive"]:
            return
        try:
            while True:
                kind, m = q.get_nowait()
                if kind == "done":
                    busy(-1)
                    gen_, errors = m
                    # zmiany zapisane tylko, gdy nie edytowano w trakcie i nie bylo bledow
                    if gen_ == app.get("edit_gen", 0) and not errors:
                        app["dirty"] = False
                elif kind == "plan":
                    ctx, res = m
                    app["busy"].discard(ctx)
                    app["plans"][ctx] = res
                    if ctx == context():
                        show_current()
                else:
                    log_box.configure(state="normal")
                    log_box.insert("end", m + "\n")
                    log_box.see("end")
                    log_box.configure(state="disabled")
        except queue.Empty:
            pass
        except Exception as e:
            try:
                log_box.configure(state="normal")
                log_box.insert("end", _("BŁĄD okna: %s\n") % friendly_error(e))
                log_box.configure(state="disabled")
            except Exception:
                pass
        finally:
            if app["alive"]:
                root.after(100, pump)

    def on_root_close():
        edw = mstate.get("editor")
        if edw is not None and edw.winfo_exists():
            edw._close()
            if edw.winfo_exists():
                return
        if app["dirty"]:
            if not messagebox.askyesno(_("Zamknąć?"), _("Są ręczne zmiany warstw, których nie "
                                       "zapisano do .lbrn2 (nie było konwersji po zmianach). "
                                       "Zamknąć mimo to?")):
                return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_root_close)
    root.after(100, pump)
    fill_materials(announce=not keep)
    for w in LOCALE_WARNINGS[keep.get("warn_shown", 0):]:
        log("locales: " + w)
    app["warn_shown"] = len(LOCALE_WARNINGS)
    load_fields()
    if keep.get("log"):
        log_box.configure(state="normal")
        log_box.insert("end", keep["log"])
        log_box.see("end")
        log_box.configure(state="disabled")
    else:
        log(_("Dodaj pliki .xs, .xcs lub .rld. Kliknij plik, żeby zobaczyć podgląd i warstwy."))
    app.update({"lb": lb, "tree": tree, "prev": prev, "ev": ev, "airv": airv, "outv": outv,
                "apply": apply_edit, "add_paths": add_paths, "start": start, "matvar": matvar,
                "mstate": mstate, "context": context, "log_box": log_box, "on_mat": on_mat,
                "fill_materials": fill_materials, "langvar": langvar,
                "on_lang_change": on_lang_change, "outvar": outvar, "owv": owv,
                "lb_action": lb_action, "cfg": cfg,
                "broken": broken})
    if keep:
        app["overrides"].update(keep.get("overrides", {}))
        app["edit_gen"] = keep.get("edit_gen", 0)
        if keep.get("files"):
            add_paths(keep["files"], select=False)
            sel = keep.get("sel")
            if sel is not None and 0 <= sel < len(files):
                lb.selection_set(sel)
                on_select()
                app["canvas_i"] = keep.get("canvas_i", 0)
                show_current()
        app["dirty"] = keep.get("dirty", False)
    return app


def run_gui(root=None, holder=None, mainloop=True):
    """Okno programu. holder (lista) dostaje aktualny slownik app (testy)."""
    import tkinter as tk
    if root is None:
        root = tk.Tk()
    if holder is None:
        holder = []

    def rebuild(old):
        sel = old["lb"].curselection()
        text = old["log_box"].get("1.0", "end-1c").rstrip("\n")
        text = text + "\n" if text else ""
        while True:  # komunikaty jeszcze w kolejce starego okna
            try:
                kind, m = old["q"].get_nowait()
            except Exception:
                break
            if kind == "log":
                text += m + "\n"
        keep = {"files": list(old["files"]), "overrides": dict(old["overrides"]),
                "dirty": old["dirty"], "edit_gen": old.get("edit_gen", 0),
                "sel": sel[0] if sel else None, "canvas_i": old["canvas_i"],
                "out_dir": old["outvar"].get(), "overwrite": bool(old["owv"].get()),
                "db": old["mstate"]["db"], "broken": old["broken"],
                "log": text,
                "geometry": root.geometry(), "warn_shown": old.get("warn_shown", 0)}
        for w in root.winfo_children():
            w.destroy()
        holder.append(build_gui(root, on_lang=rebuild, keep=keep))
        # stare zmienne Tk sprzatamy w watku okna; sprzatanie w watku roboczym moze zablokowac Tk
        import gc
        gc.collect()

    holder.append(build_gui(root, on_lang=rebuild))
    if mainloop:
        root.mainloop()
    return holder


def run_console(args):
    cfg = load_config()
    files = list(args.files)
    if not files:
        print(_("Podaj ścieżkę do pliku .xs, .xcs lub .rld (pusta linia = koniec):"))
        while True:
            s = input("> ").strip().strip('"')
            if not s:
                break
            files.append(s)
    out = args.out
    if out is None and files and not args.files:
        s = input(_("Folder zapisu (Enter = obok pliku źródłowego): ")).strip().strip('"')
        out = s or None
    mx, my = mirror_settings(cfg)
    if args.mirror_x is not None:
        mx = args.mirror_x
    if args.mirror_y is not None:
        my = args.mirror_y
    fx, fy = mirror_to_flip(mx, my)
    mat = None
    if args.material:
        try:
            mat = find_profile(load_materials(print), args.material)
        except Exception as e:
            print(_("BŁĄD bazy materiałów: %s") % friendly_error(e))
            sys.exit(1)
        if mat is None:
            print(_("BŁĄD: nie ma w bazie materiału '%s' (lista: --list-materials)") % args.material)
            sys.exit(1)
    written = []
    failed = 0
    for f in files:
        try:
            written += convert_file(f, out, print, fx, fy, args.overwrite, mat)
        except Exception as e:
            failed += 1
            print(_("BŁĄD (%s): %s") % (f, friendly_error(e)))
    mode = getattr(args, "lightburn", None)
    if not mode:
        return
    if failed or not written:
        print(_("LightBurn: nic nie wysłano (błędy konwersji albo brak zapisanych plików)."))
        sys.exit(1)
    if mode == "open" and len(written) > 1:
        print(_("LightBurn: --lightburn open otwiera jeden plik, a zapisano %d; użyj "
                "--lightburn import, żeby wstawić wszystkie do bieżącego projektu.") % len(written))
        sys.exit(1)
    for w in written:
        try:
            r = lb_send(cfg, print, w.name, w.read_bytes(), mode)
        except Exception as e:
            print(_("LightBurn: %s") % friendly_error(e))
            sys.exit(1)
        if r is True:
            print(_("LightBurn: otwarto %s") % w.name if mode == "open" else
                  _("LightBurn: wstawiono %s do bieżącego projektu") % w.name)
        elif r is None:
            print(_("LightBurn: wysłano %s (LightBurn nie potwierdził importu; sprawdź okno "
                    "LightBurna)") % w.name)
        else:
            sys.exit(1)


def main(argv=None):
    import argparse
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    if argv is None:
        argv = sys.argv[1:]
    # jezyk przed zbudowaniem pomocy argparse: --lang, potem konfiguracja, potem system
    lang = None
    for i, a in enumerate(argv):
        if a.startswith("--lang="):
            lang = a.split("=", 1)[1]
        elif a == "--lang" and i + 1 < len(argv):
            lang = argv[i + 1]
    used = set_lang(lang or load_config().get("lang") or system_lang())
    if lang and used.split("_")[0] != lang.replace("-", "_").split("_")[0].lower():
        print("language '%s' not available, using '%s'" % (lang, used), file=sys.stderr)
    for w in LOCALE_WARNINGS:
        print("locales: " + w, file=sys.stderr)
    ap = argparse.ArgumentParser(description=_("xTool Studio (.xs/.xcs) / RDWorks (.rld) -> LightBurn (.lbrn2)"))
    ap.add_argument("files", nargs="*")
    ap.add_argument("-o", "--out", default=None, help=_("folder zapisu"))
    ap.add_argument("--nogui", action="store_true", help=_("tryb konsolowy"))
    ap.add_argument("--mirror-x", dest="mirror_x", action="store_true", default=None,
                    help=_("urządzenie LightBurn ma Mirror X"))
    ap.add_argument("--no-mirror-x", dest="mirror_x", action="store_false")
    ap.add_argument("--mirror-y", dest="mirror_y", action="store_true", default=None,
                    help=_("urządzenie LightBurn ma Mirror Y"))
    ap.add_argument("--no-mirror-y", dest="mirror_y", action="store_false")
    ap.add_argument("--overwrite", action="store_true", help=_("nadpisuj istniejące .lbrn2"))
    ap.add_argument("--material", default=None, help=_("nazwa materiału z bazy"))
    ap.add_argument("--list-materials", action="store_true", help=_("pokaż materiały z bazy"))
    ap.add_argument("--lightburn", choices=("open", "import"), default=None,
                    help=_("po konwersji wyślij do działającego LightBurna 2.2+: open = otwórz "
                           "(jeden plik), import = wstaw do bieżącego projektu"))
    codes = [c for c, _n in available_languages()]
    ap.add_argument("--lang", default=None, help=" / ".join(codes))
    args = ap.parse_args(argv)
    if args.list_materials:
        try:
            db = load_materials(print)
        except Exception as e:
            print(_("BŁĄD bazy materiałów: %s") % friendly_error(e))
            sys.exit(1)
        print(_("Baza: %s") % MATERIALS_PATH)
        for p in sorted(db["profiles"], key=profile_sort_key):
            print("  %-20s %s" % (p["group"], p["name"]))
        return
    if args.files or args.nogui or args.material or args.lightburn:
        run_console(args)
        return
    try:
        run_gui()
    except Exception as e:
        print(_("Nie można otworzyć okna (%s), tryb konsolowy.") % e)
        run_console(args)


if __name__ == "__main__":
    main()
