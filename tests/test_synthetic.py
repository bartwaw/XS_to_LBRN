"""Tests on synthetic projects only (no third-party files needed).

Run:  python tests/test_synthetic.py      (or: python -m pytest tests)
"""
import base64
import atexit
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import xs2lightburn as X  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


_BASE = Path(tempfile.mkdtemp(prefix="xs2lb_test_"))
atexit.register(shutil.rmtree, str(_BASE), True)


def tmpdir():
    return Path(tempfile.mkdtemp(dir=str(_BASE)))


def quiet(_m):
    pass


def disp(i, t, layer, **kw):
    d = {"id": "id%d" % i, "type": t, "layerTag": layer, "visible": True, "zOrder": i,
         "transform": [1, 0, 0, 1, 0, 0]}
    d.update(kw)
    return d


def params(ptype, **v):
    return {"processingType": ptype, "processIgnore": False,
            "data": {ptype: {"materialType": "customize", "parameter": {"customize": v}}}}


# Shared synthetic project: cut (2 shapes), fill, rotated bitmap, line engrave,
# a hidden path, an unsupported TEXT object and a rounded RECT.
TH = math.radians(30)
C30, S30 = math.cos(TH), math.sin(TH)


def displays():
    uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    return [
        disp(1, "PATH", "#ff0000", dPath="M0 0L10 0L10 10L0 10Z"),
        disp(2, "PATH", "#ff0000", dPath="M20 0L30 0L30 10Z"),
        disp(3, "PATH", "#00ff00", dPath="M0 20L10 20L10 30Z"),
        disp(4, "BITMAP", "#00ff00", base64=uri, originWidth=1, originHeight=1,
             transform=[10 * C30, 10 * S30, -10 * S30, 10 * C30, 50, 50], colorInverted=True),
        disp(5, "PATH", "#0000ff", dPath="M0 40L5 40"),
        disp(6, "PATH", "#ff0000", dPath="M0 50L5 50", visible=False),
        disp(7, "TEXT", "#ff0000"),
        disp(8, "RECT", "#ff0000", width=8, height=4, radius=1, transform=[1, 0, 0, 1, 60, 0]),
    ]


OPS = {
    "id1": params("VECTOR_CUTTING", power=100, speed=10, repeat=2),
    "id2": params("VECTOR_CUTTING", power=100, speed=10, repeat=2),
    "id3": params("FILL_VECTOR_ENGRAVING", power=15, speed=300, repeat=1, density=200,
                  scanAngle=45, crossAngle=True),
    "id4": params("BITMAP_ENGRAVING", power=20, speed=150, repeat=1, density=254,
                  bitmapMode="Stucki"),
    "id5": params("VECTOR_ENGRAVING", power=5, speed=50, repeat=1),
    "id8": params("VECTOR_CUTTING", power=100, speed=10, repeat=2),
}
LAYERS = {"#ff0000": {"order": 2}, "#00ff00": {"order": 1}, "#0000ff": {"order": 3}}


def write_xcs(path):
    proj = {"canvas": [{"id": "c1", "title": "p1", "displays": displays(), "layerData": LAYERS}],
            "device": {"data": {"dataType": "Map", "value": [
                ["c1", {"mode": "LASER_PLANE",
                        "displays": {"dataType": "Map", "value": [[k, v] for k, v in OPS.items()]}}]]}}}
    path.write_text(json.dumps(proj), encoding="utf-8")
    return path


def write_xs(path):
    """The same project in the zipped .xs layout (profiles, bindings, patches, vectorRef,
    resourcePath, chunked displays)."""
    ds = displays()
    vec = {}
    for d in ds:
        if d.get("dPath") and d["id"] in ("id1", "id3"):
            h = "hash_" + d["id"]
            vec[h] = d.pop("dPath")
            d["vectorRef"] = {"bucketType": "svg", "vectorHash": h}
        if d["type"] == "BITMAP":
            d.pop("base64")
            d["resourcePath"] = "resources/img.png"
    profiles, bindings, patches = {}, [], {}
    for n, (did, op) in enumerate(OPS.items()):
        pt = op["processingType"]
        values = dict(op["data"][pt]["parameter"]["customize"])
        pid = "prof%d" % n
        profiles[pid] = {"processingType": pt, "values": values}
        b = {"baseProfileId": pid, "displayIds": [did], "patchIds": []}
        if did == "id5":  # a patch overrides the profile speed
            profiles[pid]["values"]["speed"] = 999
            patches["patch5"] = {"overrides": {"speed": 50}}
            b["patchIds"] = ["patch5"]
        bindings.append(b)
    dev = {"processing": {"c1": {"activeMode": "LASER_PLANE", "modes": {"LASER_PLANE": {
        "bindings": bindings, "patches": patches, "ignoredDisplayIds": []}}}}}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(".format", "xs")
        z.writestr("project.json", json.dumps({"modules": {"canvases": ["c1"]},
                                               "activeDeviceId": "d1"}))
        z.writestr("profiles.json", json.dumps({"profiles": profiles}))
        z.writestr("devices/device-d1.json", json.dumps(dev))
        z.writestr("canvases/c1.json", json.dumps({"title": "p1", "layerData": LAYERS,
                                                   "chunkLayout": {"chunkIndexes": [0, 1]}}))
        z.writestr("canvases/c1/displays-0.json", json.dumps({"displays": ds[:4]}))
        z.writestr("canvases/c1/displays-1.json", json.dumps({"displays": ds[4:]}))
        z.writestr("vectors/svg/data-0.json", json.dumps({"entries": vec}))
        z.writestr("resources/img.png", PNG_1PX)
    return path


def convert(src, out, mirror=(False, False), **kw):
    fx, fy = X.mirror_to_flip(*mirror)
    res = X.convert_file(src, out, kw.pop("log", quiet), fx, fy, overwrite=True, **kw)
    assert len(res) == 1
    return res[0]


def layers(root):
    cs = [c for c in root if c.tag in ("CutSetting", "CutSetting_Img")]
    return sorted(cs, key=lambda c: int(c.find("priority").get("Value")))


def val(c, tag):
    e = c.find(tag)
    return None if e is None else e.get("Value")


def verts(shape):
    txt = shape.find("VertList").text
    return [(float(a), float(b)) for a, b in re.findall(r"V(-?[\d.]+) (-?[\d.]+)", txt)]


# --------------------------------------------------------------------------- tests

def test_xcs_layers_and_parameters():
    X.set_lang("pl")
    t = tmpdir()
    logs = []
    root = ET.parse(convert(write_xcs(t / "s.xcs"), t, log=logs.append)).getroot()
    cs = layers(root)
    assert [val(c, "index") for c in cs] == ["4", "3", "2", "1"]
    assert [val(c, "name") for c in cs] == [
        "00FF00 Grawer obrazu 20% 150 254lpcm", "00FF00 Wypełnienie 15% 300 200lpcm",
        "FF0000 Cięcie 100% 10 x2", "0000FF Grawer linią 5% 50"]
    im, fill, cut, line = cs
    assert im.tag == "CutSetting_Img" and val(im, "ditherMode") == "stucki"
    assert val(im, "negative") == "1"
    assert val(im, "dpi") == str(round(25.4 / (10 / 254)))
    assert (fill.get("type"), val(fill, "interval"), val(fill, "angle"), val(fill, "crossHatch")) == \
        ("Scan", "0.05", "45", "1")
    assert (val(cut, "numPasses"), val(cut, "maxPower"), val(cut, "speed")) == ("2", "100", "10")
    assert line.get("type") == "Cut" and val(line, "maxPower") == "5"
    assert len(root.findall("Shape")) == 6  # hidden path and TEXT skipped
    assert any("TEXT" in m for m in logs)


def test_english_layer_names():
    X.set_lang("en")
    t = tmpdir()
    root = ET.parse(convert(write_xcs(t / "s.xcs"), t)).getroot()
    assert [val(c, "name") for c in layers(root)] == [
        "00FF00 Image engrave 20% 150 254lpcm", "00FF00 Fill 15% 300 200lpcm",
        "FF0000 Cut 100% 10 x2", "0000FF Line engrave 5% 50"]


def test_xs_matches_xcs():
    X.set_lang("en")
    t = tmpdir()
    a = convert(write_xcs(t / "s.xcs"), t).read_text(encoding="utf-8")
    b = convert(write_xs(t / "s.xs"), t).read_text(encoding="utf-8")
    assert a == b


def test_mirror_settings():
    X.set_lang("en")
    t = tmpdir()
    src = write_xcs(t / "s.xcs")
    for mx in (False, True):
        for my in (False, True):
            root = ET.parse(convert(src, t, (mx, my))).getroot()
            assert root.get("MirrorX") == str(mx) and root.get("MirrorY") == str(my)
            tri = [s for s in root.findall("Shape")
                   if s.get("CutIndex") == "2" and len(verts(s)) == 3]
            assert len(tri) == 1
            v = verts(tri[0])  # xTool points (20,0) (30,0) (30,10)
            dx = v[1][0] - v[0][0]
            dy = v[2][1] - v[1][1]
            # without device mirroring LightBurn has Y up: xTool +Y (down) becomes -Y
            assert (dx > 0) == (not mx), (mx, my, v)
            assert (dy < 0) == (not my), (mx, my, v)


def test_material_examples_import_export():
    X.set_lang("pl")  # example names are English in every language
    t = tmpdir()
    dbp = t / "materials.json"
    db = X.load_materials(None, dbp)
    assert dbp.exists() and len(db["profiles"]) == 3
    assert all(p["name"].startswith("Example") for p in db["profiles"])
    ply = X.find_profile(db, "Example: birch plywood 3 mm")
    X.set_lang("en")
    root = ET.parse(convert(write_xcs(t / "s.xcs"), t, material=ply)).getroot()
    cut = [c for c in layers(root) if " Cut " in val(c, "name")][0]
    assert (val(cut, "minPower"), val(cut, "maxPower"), val(cut, "speed")) == ("10", "65", "50")
    # export, then import into a fresh path
    exp = t / "export.json"
    X.save_materials(db, exp)
    try:
        X.load_materials(None, t / "nothing.json", create=False)
    except Exception:
        pass
    else:
        raise AssertionError("missing database file accepted")
    back = X.load_materials(None, exp, create=False)
    assert [p["name"] for p in back["profiles"]] == [p["name"] for p in db["profiles"]]


def test_engrave_only_material_turns_cut_off():
    X.set_lang("en")
    t = tmpdir()
    db = X.load_materials(None, t / "m.json")
    alu = X.find_profile(db, "Example: anodized aluminum (engrave only)")
    logs = []
    root = ET.parse(convert(write_xcs(t / "s.xcs"), t, material=alu, log=logs.append)).getroot()
    miss = [c for c in layers(root) if "NOT IN DATABASE" in val(c, "name")]
    assert miss and all(val(c, "doOutput") == "0" and val(c, "maxPower") == "0" for c in miss)


def test_layer_overrides():
    X.set_lang("en")
    t = tmpdir()
    src = write_xcs(t / "s.xcs")
    ov = {0: {2: {"power": 40.0, "min_power": 10.0, "speed": 20.0, "passes": 3, "kerf": 0.0,
                  "air": 1, "off": 0}}}
    root = ET.parse(convert(src, t, overrides=ov)).getroot()
    c2 = [c for c in layers(root) if val(c, "index") == "2"][0]
    assert (val(c2, "maxPower"), val(c2, "minPower"), val(c2, "speed"), val(c2, "numPasses")) == \
        ("40", "10", "20", "3")


def test_svg_path_parser():
    assert X.parse_svg_path("M1.5.5L2-3")[0].pts == [(1.5, 0.5), (2, -3)]
    sp = X.parse_svg_path("M0 0 a5 5 0 01.5 3")
    assert abs(sp[0].pts[-1][0] - 0.5) < 1e-9 and abs(sp[0].pts[-1][1] - 3) < 1e-9
    assert len(X.parse_svg_path("M0 0L1 1 m1 1 l1 0")) == 2


def test_damaged_files_give_readable_errors():
    X.set_lang("en")
    t = tmpdir()
    (t / "bad.xs").write_bytes(b"PK\x03\x04garbage")
    (t / "bad.xcs").write_text("{not json")
    for name, word in (("bad.xs", "archive"), ("bad.xcs", "damaged data")):
        try:
            X.convert_file(t / name, t, quiet)
        except Exception as e:
            assert word in X.friendly_error(e)
        else:
            raise AssertionError("no error for " + name)


def _source_keys():
    import ast
    tree = ast.parse((ROOT / "xs2lightburn.py").read_text(encoding="utf-8"))
    keys = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_"
                and n.args and isinstance(n.args[0], ast.Constant)):
            keys.add(n.args[0].value)
    for const in (X.KIND_NAME, X.KIND_SHORT, X.OP_LABEL):
        keys |= set(const.values())
    keys.add(X.DB_NOTE)
    return keys


SPEC = X._SPEC


def test_english_locale_complete():
    keys = _source_keys()
    doc = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
    strings = doc["strings"]
    assert sorted(keys - set(strings)) == [], "missing in en.json"
    assert sorted(set(strings) - keys) == [], "unused keys in en.json"


def test_all_locale_files_valid():
    keys = _source_keys()
    files = sorted((ROOT / "locales").glob("*.json"))
    assert files
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8-sig"))
        assert isinstance(doc.get("language"), str) and doc["language"], f.name
        assert isinstance(doc.get("strings"), dict), f.name
        assert re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", f.stem), f.name
        for k, v in doc["strings"].items():
            assert k in keys, (f.name, "unknown key", k)
            assert isinstance(v, str), (f.name, k)
            assert SPEC.findall(k) == SPEC.findall(v), (f.name, k, v)
            lead = len(k) - len(k.lstrip())
            assert v[:len(v) - len(v.lstrip())] == k[:lead], (f.name, "leading spaces", k)
    codes = [c for c, _n in X.available_languages()]
    assert codes[0] == "pl" and "en" in codes


class _Locales:
    """Temporary locales folder for the loader tests."""

    def __init__(self, files, with_en=True):
        self.dir = tmpdir() / "locales"
        self.dir.mkdir()
        if with_en:
            (self.dir / "en.json").write_bytes((ROOT / "locales" / "en.json").read_bytes())
        for name, data in files.items():
            (self.dir / name).write_bytes(data if isinstance(data, bytes)
                                          else json.dumps(data).encode("utf-8"))

    def __enter__(self):
        self.old = X.LOCALES_DIR
        X.LOCALES_DIR = self.dir
        del X.LOCALE_WARNINGS[:]
        return self

    def __exit__(self, *a):
        X.LOCALES_DIR = self.old
        del X.LOCALE_WARNINGS[:]
        X.set_lang("en")
        return False


def test_partial_locale_falls_back_to_english():
    files = {
        "de.json": {"language": "Deutsch", "strings": {
            "Cięcie": "Schneiden",
            "Plik: %s": "Datei: %d %s",                    # wrong placeholders: ignored
            "moc musi być w zakresie 0-100%": "Leistung im Bereich 0-100 % halten",  # "% h" is text
            "Grawer obrazu": 5,                            # not text: ignored
            "  pominięto (ukryty komponent): %s": "  100 % des Teils: %s",  # "% d" would crash
        }},
        "bad name.json": {"language": "x", "strings": {}},
        "pt-BR.json": {"language": "x", "strings": {}},
    }
    with _Locales(files):
        assert [c for c, _n in X.available_languages()] == ["pl", "de", "en"]
        assert any("bad name.json" in w for w in X.LOCALE_WARNINGS)
        assert any("pt-BR.json" in w for w in X.LOCALE_WARNINGS)
        assert X.set_lang("de_AT") == "de"
        assert X._("Cięcie") == "Schneiden"
        assert X._("Plik: %s") == "File: %s"           # bad entry -> English
        assert X._("  pominięto (ukryty komponent): %s") % "a" == "  skipped (hidden component): a"
        assert X._("Grawer linią") == "Line engrave"   # missing -> English
        assert X._("moc musi być w zakresie 0-100%") == "Leistung im Bereich 0-100 % halten"
        assert any("de.json: ignored 3 entries" in w for w in X.LOCALE_WARNINGS), X.LOCALE_WARNINGS
        assert X.set_lang("xx") == "en"
        assert X.set_lang("pl") == "pl" and X._("Cięcie") == "Cięcie"


def test_locale_bom_bad_json_duplicates_and_regions():
    bom = b"\xef\xbb\xbf" + json.dumps({"language": "Français",
                                        "strings": {"Cięcie": "Découpe"}}).encode("utf-8")
    files = {
        "fr.json": bom,                                   # saved by Notepad with BOM
        "it.json": '{"language": "Italiano", "strings": {"Cięcie": "Taglio",}}'.encode("utf-8"),  # bad JSON
        "cs.json": {"language": "English", "strings": {}},  # duplicate display name
        "es.json": {"language": "Español", "strings": ["not", "a", "dict"]},
        "pt_BR.json": {"language": "Português (Brasil)", "strings": {"Cięcie": "Corte"}},
    }
    with _Locales(files):
        langs = X.available_languages()
        codes = [c for c, _n in langs]
        assert codes == ["pl", "cs", "en", "fr", "pt_BR"], codes
        assert len(set(n for _c, n in langs)) == len(langs)       # names unique
        assert any(w.startswith("it.json") for w in X.LOCALE_WARNINGS)
        assert any(w.startswith("es.json") for w in X.LOCALE_WARNINGS)
        X.set_lang("fr")
        assert X._("Cięcie") == "Découpe"
        assert X.set_lang("pt") == "pt_BR" and X._("Cięcie") == "Corte"
        assert X.set_lang("pt_PT") == "pt_BR"


def test_missing_locales_folder():
    with _Locales({}, with_en=False):
        assert X.available_languages() == [("pl", "Polski")]
        assert any("locales folder" in w for w in X.LOCALE_WARNINGS)
        assert X.set_lang("en") == "pl"
        assert X._("Cięcie") == "Cięcie"


def test_system_lang():
    env = {k: v for k, v in os.environ.items()
           if k not in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")}
    code = "import sys; sys.path.insert(0, %r); import xs2lightburn as X; print(X.system_lang())" \
        % str(ROOT)
    for extra, want in (({"LANG": "pl_PL.UTF-8"}, "pl_PL"), ({"LANG": "pt_BR.UTF-8"}, "pt_BR"),
                        ({"LC_ALL": "C", "LANG": "pl_PL.UTF-8"}, "pl_PL"),
                        ({"LANGUAGE": "de:en", "LANG": "pl_PL.UTF-8"}, "de_DE")):
        if sys.platform == "win32":
            break  # Windows uses the display language, not these variables
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                           env=dict(env, **extra))
        assert r.stdout.strip() == want, (extra, r.stdout, r.stderr)


def _skip(reason):
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(reason)
    return "skipped: " + reason


def test_gui_language_switch():
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        return _skip("no display")
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        return _skip("no tkinter")
    try:
        X.set_lang("en")
        holder = X.run_gui(root, mainloop=False)
        app = holder[-1]
        t = tmpdir()
        src = write_xcs(t / "s.xcs")
        app["add_paths"]([str(src)])
        app["outvar"].set(str(t))
        root.update()

        def texts(w):
            out = []
            for c in w.winfo_children():
                for opt in ("text", "values"):
                    try:
                        out.append(str(c.cget(opt)))
                    except Exception:
                        pass
                out += texts(c)
            return out

        polish = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
        assert not [x for x in texts(root) if polish.search(x)
                    and "Język" not in x and "Polski" not in x]
        log_len = len(app["log_box"].get("1.0", "end-1c"))
        app["langvar"].set("Polski")
        app["on_lang_change"]()
        root.update()
        app["langvar"] = holder[-1]["langvar"]
        holder[-1]["langvar"].set("English")
        holder[-1]["on_lang_change"]()
        root.update()
        new = holder[-1]
        assert len(holder) == 3 and X.LANG == "en"
        assert new["files"] == [str(src)] and new["outvar"].get() == str(t)
        assert len(new["log_box"].get("1.0", "end-1c")) >= log_len
    finally:
        root.destroy()
        import gc
        gc.collect()   # free Tk objects in the main thread, not in a later worker thread
        X.set_lang("en")


def test_mirror_defaults_and_migration():
    assert X.mirror_settings({}) == (False, False)
    # settings saved by 1.x (flip_x/flip_y) map to the matching device mirroring
    assert X.mirror_settings({"flip_x": True, "flip_y": False}) == (True, True)
    assert X.mirror_settings({"mirror_x": True}) == (True, False)
    t = tmpdir()
    out = X.convert_file(write_xcs(t / "s.xcs"), t, quiet)[0]  # library default: no mirroring
    r = ET.parse(out).getroot()
    assert (r.get("MirrorX"), r.get("MirrorY")) == ("False", "False")


def test_multi_canvas_file_names_do_not_depend_on_language():
    t = tmpdir()
    proj = {"canvas": [{"id": "c%d" % i, "displays": [disp(1, "PATH", "#ff0000",
                                                           dPath="M0 0L10 0L10 10Z")]}
                       for i in (1, 2)],
            "device": {"data": {"dataType": "Map", "value": [
                ["c%d" % i, {"displays": {"dataType": "Map", "value": [
                    ["id1", params("VECTOR_CUTTING", power=50, speed=10, repeat=1)]]}}]
                for i in (1, 2)]}}}
    src = t / "m.xcs"
    src.write_text(json.dumps(proj), encoding="utf-8")
    names = {}
    for lang in ("en", "pl"):
        X.set_lang(lang)
        o = tmpdir()
        names[lang] = sorted(p.name for p in X.convert_file(src, o, quiet))
    assert names["en"] == names["pl"] and len(names["en"]) == 2, names


def test_no_polish_text_outside_translations():
    import ast
    tree = ast.parse((ROOT / "xs2lightburn.py").read_text(encoding="utf-8"))
    inside = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_":
            inside |= {id(a) for a in ast.walk(n)}
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and \
                isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            inside.add(id(n.body[0].value))
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in (
                "KIND_NAME", "KIND_SHORT", "OP_LABEL", "DB_NOTE"):
            inside |= {id(a) for a in ast.walk(n)}
    allowed = {"Language / Język:", "nie"}
    pl = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]|\b(brak\w*|plik\w*|warstw\w*|parametr\w*|zakres\w*|"
                    r"kopi\w*|filtr\w*|mask[aię]\w*|plansz\w*|edycj\w*|wczytywan\w*|uwaga|"
                    r"zapis\w*|baz[aęy]\w*|pomini\w*|przyk\w*)\b", re.I)
    found = [(n.lineno, n.value) for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in inside
             and n.value not in allowed and pl.search(n.value)]
    assert not found, found


def test_console_survives_non_utf8_output():
    t = tmpdir()
    env = dict(os.environ, HOME=str(t), USERPROFILE=str(t), PYTHONIOENCODING="cp437")
    r = subprocess.run([sys.executable, str(ROOT / "xs2lightburn.py"), "--help", "--lang", "pl"],
                       capture_output=True, env=env)
    assert r.returncode == 0, r.stderr


def test_language_precedence():
    t = tmpdir()
    (t / ".xs2lightburn.json").write_text(json.dumps({"lang": "pl"}), encoding="utf-8")
    env = dict(os.environ, HOME=str(t), USERPROFILE=str(t), LANG="en_US.UTF-8",
               PYTHONIOENCODING="utf-8")
    env.pop("LANGUAGE", None)
    env.pop("LC_ALL", None)
    script = str(ROOT / "xs2lightburn.py")
    r = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True,
                       encoding="utf-8", env=env)
    assert "folder zapisu" in r.stdout, r.stdout  # config beats system
    r = subprocess.run([sys.executable, script, "--help", "--lang", "en"], capture_output=True,
                       text=True, encoding="utf-8", env=env)
    assert "output folder" in r.stdout  # --lang beats config


def test_cli():
    t = tmpdir()
    src = write_xcs(t / "s.xcs")
    env = dict(os.environ, HOME=str(t), USERPROFILE=str(t), PYTHONIOENCODING="utf-8")
    script = str(ROOT / "xs2lightburn.py")
    r = subprocess.run([sys.executable, script, "--help", "--lang", "en"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0 and "output folder" in r.stdout
    r = subprocess.run([sys.executable, script, str(src), "-o", str(t), "--lang", "pl",
                        "--mirror-x", "--mirror-y", "--overwrite"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0 and "zapisano" in r.stdout, r.stdout + r.stderr
    root = ET.parse(t / "s.lbrn2").getroot()
    assert root.get("MirrorX") == "True" and root.get("MirrorY") == "True"
    r = subprocess.run([sys.executable, script, "--list-materials", "--lang=en"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert "Example: birch plywood 3 mm" in r.stdout


CLB = b"""<?xml version="1.0" encoding="UTF-8"?>
<LightBurnLibrary DisplayName="Test lib">
  <Material name="Plywood">
    <Entry Thickness="3.0000" Desc="Cut">
      <CutSetting type="Cut"><index Value="0"/><minPower Value="15"/><maxPower Value="60"/>
        <speed Value="25"/><numPasses Value="2"/></CutSetting>
    </Entry>
    <Entry Thickness="3.0000" Desc="Cut slow">
      <CutSetting type="Cut"><minPower Value="15"/><maxPower Value="70"/><speed Value="15"/></CutSetting>
    </Entry>
    <Entry Thickness="6.0000" Desc="Cut">
      <CutSetting type="Cut"><maxPower Value="70"/><speed Value="10"/><kerf Value="0.1"/></CutSetting>
    </Entry>
    <Entry Thickness="-1.0000" NoThickTitle="Engrave" Desc="Fill">
      <CutSetting type="Scan"><minPower Value="10"/><maxPower Value="20"/><speed Value="400"/>
        <interval Value="0.08"/><runBlower Value="0"/></CutSetting>
    </Entry>
    <Entry Thickness="-1.0000" NoThickTitle="Photo" Desc="Image">
      <CutSetting_Img type="Image"><maxPower Value="30"/><speed Value="300"/><dpi Value="254"/>
        <ditherMode Value="stucki"/></CutSetting_Img>
    </Entry>
  </Material>
  <Material name="Anodized Al">
    <Entry Thickness="-1.0000" NoThickTitle="Mark" Desc="Score">
      <CutSetting type="Cut"><maxPower Value="25"/><speed Value="200"/></CutSetting>
    </Entry>
    <Entry Thickness="-1.0000" NoThickTitle="Odd" Desc="Tool">
      <CutSetting type="Tool"><maxPower Value="25"/><speed Value="200"/></CutSetting>
    </Entry>
    <Entry Thickness="-1.0000" NoThickTitle="Bad" Desc="Too fast">
      <CutSetting type="Scan"><maxPower Value="25"/><speed Value="99999"/></CutSetting>
    </Entry>
  </Material>
</LightBurnLibrary>
"""


def _by_name(profiles):
    return {p["name"]: p for p in profiles}


def test_clb_import():
    X.set_lang("en")
    t = tmpdir()
    (t / "lib.clb").write_bytes(CLB)
    profs, skipped = X.clb_to_profiles(t / "lib.clb")
    p = _by_name(profs)
    assert sorted(p) == ["Anodized Al", "Plywood 3 mm", "Plywood 3 mm (Cut slow)", "Plywood 6 mm"]
    ply3 = p["Plywood 3 mm"]
    assert ply3["group"] == "LightBurn: Test lib" and ply3["thickness"] == 3
    assert ply3["ops"]["cut"] == {"speed": 25.0, "min": 15.0, "max": 60.0, "passes": 2, "air": True}
    # entries without thickness (engrave, image) are shared by every thickness
    assert ply3["ops"]["fill"]["interval"] == 0.08 and ply3["ops"]["fill"]["air"] is False
    assert ply3["ops"]["image"]["dither"] == "stucki" and ply3["ops"]["image"]["interval"] == 0.1
    assert p["Plywood 6 mm"]["ops"]["cut"]["kerf"] == 0.1
    assert p["Plywood 6 mm"]["ops"]["cut"]["min"] == 70.0      # no minPower -> max
    assert p["Plywood 3 mm (Cut slow)"]["ops"]["cut"]["speed"] == 15.0
    assert list(p["Anodized Al"]["ops"]) == ["line"]           # no thickness + "Score" -> line
    assert len(skipped) == 2 and any("Tool" in x for x in skipped)
    # every imported profile is valid for the converter
    assert all(X.clean_profile(x) == x for x in profs)
    root = ET.parse(convert(write_xcs(t / "s.xcs"), t, material=ply3)).getroot()
    cut = [c for c in layers(root) if " Cut " in val(c, "name")][0]
    assert (val(cut, "maxPower"), val(cut, "speed"), val(cut, "numPasses")) == ("60", "25", "2")


def test_clb_rejects_bad_files():
    t = tmpdir()
    for name, data in (("a.clb", b"<notalibrary/>"), ("b.clb", b"garbage<<"),
                       ("c.clb", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
                                 b'<LightBurnLibrary/>')):
        (t / name).write_bytes(data)
        try:
            X.clb_to_profiles(t / name)
        except ValueError:
            pass
        else:
            raise AssertionError(name)


API_LIB = {"product": "lightburn", "name": "Shop", "entries": [
    {"guid": 1, "material": "Birch", "description": "Cut", "thickness": 3, "no_thick_title": "",
     "cut": {"kind": "normal", "mode": "cut", "index": -1, "params": {
         "speed": 1200, "min_power": 20, "max_power": 60, "num_passes": 2, "kerf": 0.004,
         "gantry": {"run_blower": False}}}},
    {"guid": 2, "material": "Birch", "description": "Fill", "thickness": -1,
     "no_thick_title": "Engrave", "cut": {"kind": "normal", "mode": "scan", "params": {
         "speed": 24000, "min_power": 10, "max_power": 20, "num_passes": 1, "interval": 0.004}}},
    {"guid": 3, "material": "Birch", "description": "Photo", "thickness": -1,
     "no_thick_title": "Image", "cut": {"kind": "image", "params": {
         "speed": 18000, "max_power": 30, "dpi": 254, "dither_mode": "Stucki"}}},
]}


def test_api_library_units():
    # LightBurn reports values in its display units: here mm/min and inches
    entries = X.api_library_entries(API_LIB, {"units": "mm/min", "distance_units": "in"})
    profs, skipped = X.library_to_profiles(entries, "Shop", "test")
    assert not skipped and len(profs) == 1
    ops = profs[0]["ops"]
    assert profs[0]["name"] == "Birch 3 mm"
    assert ops["cut"]["speed"] == 20.0 and ops["cut"]["air"] is False
    assert abs(ops["cut"]["kerf"] - 0.1016) < 1e-9
    assert ops["fill"]["speed"] == 400.0 and abs(ops["fill"]["interval"] - 0.1016) < 1e-9
    assert ops["image"]["dither"] == "stucki" and ops["image"]["interval"] == 0.1
    try:
        X.api_library_entries(API_LIB, {"units": "furlong/fortnight"})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown units accepted")


def _fake(**kw):
    sys.path.insert(0, str(ROOT / "tests"))
    from mock_lightburn import FakeLightBurn
    return FakeLightBurn(**kw)


class _TempPaths:
    """Config and LightBurn secret files in a temporary folder."""

    def __enter__(self):
        t = tmpdir()
        self.old = (X.CONFIG_PATH, X.LB_SECRET_PATH)
        X.CONFIG_PATH, X.LB_SECRET_PATH = t / "cfg.json", t / "secret.json"
        return t

    def __exit__(self, *a):
        X.CONFIG_PATH, X.LB_SECRET_PATH = self.old
        return False


def test_lightburn_api_client():
    X.set_lang("en")
    with _TempPaths():
        fake = _fake(library=API_LIB, units={"units": "mm/s", "distance_units": "mm"})
        try:
            cfg = {"lightburn_url": fake.url}
            logs = []
            profs, skipped, name = X.lb_material_profiles(cfg, logs.append)
            assert name == "Shop" and profs and fake.pairings == 1
            assert X.lb_secret(cfg) == "secret1" and "lightburn_secret" not in cfg
            stored = json.loads(X.LB_SECRET_PATH.read_text())["secrets"]
            assert stored == {fake.url: "secret1"}
            if os.name == "posix":
                assert X.LB_SECRET_PATH.stat().st_mode & 0o077 == 0
            r = X.lb_send(cfg, logs.append, "Próba ł.lbrn2", b"<LightBurnProject/>", "open")
            path, headers, body = fake.received[-1]
            assert r is True and path == "/api/file/open" and body == b"<LightBurnProject/>"
            assert headers["X-Filename"] == "Proba _.lbrn2" and fake.pairings == 1
            fake.revoke_all()                            # revoked in LightBurn -> pair again
            assert X.lb_send(cfg, logs.append, "b.lbrn2", b"x", "import") is True
            assert fake.pairings == 2 and fake.received[-1][0] == "/api/file/upload"
            assert X.lb_secret(cfg) == "secret2"
            fake.import_result = {"success": False, "error": "bad file"}
            assert X.lb_send(cfg, logs.append, "c.lbrn2", b"x", "import") is False
            fake.import_result = True                    # not an object: no crash
            assert X.lb_send(cfg, logs.append, "c.lbrn2", b"x", "import") is False
            fake.send_events = False
            assert X.lb_send(cfg, logs.append, "d.lbrn2", b"x", "import", wait=1) is None
            fake.html_on_upload = True                   # some other web server on the port
            try:
                X.lb_send(cfg, logs.append, "h.lbrn2", b"x", "import", wait=1)
            except X.LightBurnError:
                pass
            else:
                raise AssertionError("HTML answer accepted")
            fake.html_on_upload = False
            fake.approve = False
            fake.revoke_all()
            try:
                X.lb_send(cfg, logs.append, "e.lbrn2", b"x", "import")
            except X.LightBurnError as e:
                assert e.code == 403
            else:
                raise AssertionError("declined pairing accepted")
        finally:
            fake.close()
        try:
            X.lb_material_profiles({"lightburn_url": fake.url}, quiet)
        except X.LightBurnError as e:
            assert "LightBurn 2.2" in str(e)
        else:
            raise AssertionError("no error without LightBurn")


def test_settings_and_secret_files():
    with _TempPaths() as t:
        # settings: atomic, no temp files left, symlink kept, old dev-build secret dropped
        X.save_config({"lang": "en", "lightburn_secret": "OLD"})
        assert X.load_config() == {"lang": "en"}
        assert "OLD" not in X.CONFIG_PATH.read_text()      # removed from disk right away
        if os.name == "posix":
            X.CONFIG_PATH.chmod(0o600)
            X.save_config({"lang": "en"})
            assert X.CONFIG_PATH.stat().st_mode & 0o777 == 0o600   # existing mode kept
        assert not list(t.glob("*.tmp"))
        if os.name == "posix":
            real = t / "real.json"
            real.write_text("{}")
            X.CONFIG_PATH.unlink()
            X.CONFIG_PATH.symlink_to(real)
            X.save_config({"lang": "pl"})
            assert X.CONFIG_PATH.is_symlink() and json.loads(real.read_text()) == {"lang": "pl"}
        # damaged secret file: treated as empty, then overwritten
        cfg = {"lightburn_url": "http://127.0.0.1:1"}
        X.LB_SECRET_PATH.write_text("{garbage")
        assert X.lb_secret(cfg) is None
        X.lb_set_secret(cfg, "abc")
        assert X.lb_secret(cfg) == "abc"
        if os.name == "posix":
            assert X.LB_SECRET_PATH.stat().st_mode & 0o077 == 0
        X.lb_set_secret(cfg, None)
        assert X.lb_secret(cfg) is None and not list(t.glob("*.tmp"))


def test_api_library_requires_units():
    for units in ({}, {"units": "mm/s"}, {"distance_units": "mm"}, None):
        try:
            X.api_library_entries(API_LIB, units)
        except ValueError:
            pass
        else:
            raise AssertionError(units)


def test_line_heuristic_whole_words():
    for label in ("Cut outline", "Remark cut", "Cut - Spline", "linia cięcia", "Through cut"):
        assert X._lib_kind("Cut", 3.0, label) == "cut", label
    for label in ("Score", "Engrave lines", "Mark", "Grawer", "etching"):
        assert X._lib_kind("Cut", 3.0, label) == "line", label
    assert X._lib_kind("Cut", None, "Cut") == "line"


def test_clb_rejects_hidden_entities():
    t = tmpdir()
    pad = b"<!--" + b"x" * 5000 + b"-->"
    bomb = (b'<?xml version="1.0"?>' + pad + b'<!DOCTYPE l [<!ENTITY a "aaaa">]>'
            b'<LightBurnLibrary><Material name="&a;"/></LightBurnLibrary>')
    (t / "pad.clb").write_bytes(bomb)
    (t / "u16.clb").write_bytes(bomb.decode("ascii").encode("utf-16"))
    for name in ("pad.clb", "u16.clb"):
        try:
            X.clb_to_profiles(t / name)
        except ValueError:
            pass
        else:
            raise AssertionError(name)
    # BOM + XML declaration with encoding is fine
    (t / "bom.clb").write_bytes(b"\xef\xbb\xbf" + CLB)
    assert X.clb_to_profiles(t / "bom.clb")[0]


def test_cli_send_to_lightburn():
    t = tmpdir()
    fake = _fake()
    try:
        (t / ".xs2lightburn.json").write_text(json.dumps({"lightburn_url": fake.url}),
                                              encoding="utf-8")
        src = write_xcs(t / "s.xcs")
        env = dict(os.environ, HOME=str(t), USERPROFILE=str(t), PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, "-B", str(ROOT / "xs2lightburn.py"), "-o", str(t), "--lang", "en",
               "--overwrite"]
        r = subprocess.run(cmd + [str(src), "--lightburn", "open"], capture_output=True,
                           text=True, encoding="utf-8", env=env, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "LightBurn: opened s.lbrn2" in r.stdout, r.stdout
        path, headers, body = fake.received[-1]
        assert path == "/api/file/open" and body == (t / "s.lbrn2").read_bytes()
        assert json.loads((t / ".xs2lightburn_lightburn.json").read_text())["secrets"][fake.url]
        assert "secret" not in (t / ".xs2lightburn.json").read_text()
        # a failed conversion must give a non-zero exit code
        r = subprocess.run(cmd + [str(t / "missing.xcs"), "--lightburn", "import"],
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        assert r.returncode == 1 and "nothing sent" in r.stdout, r.stdout
    finally:
        fake.close()


def test_gui_lightburn_buttons():
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        return _skip("no display")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
    except Exception:
        return _skip("no tkinter")
    fake = _fake(library=API_LIB)
    paths = _TempPaths()
    t = paths.__enter__()
    X.CONFIG_PATH.write_text(json.dumps({"lightburn_url": fake.url}), encoding="utf-8")
    shown = []
    saved = (messagebox.showinfo, messagebox.showerror, messagebox.askyesnocancel)
    messagebox.showinfo = lambda *a, **k: shown.append(("info",) + a)
    messagebox.showerror = lambda *a, **k: shown.append(("error",) + a)
    messagebox.askyesnocancel = lambda *a, **k: False     # duplicates: add with "(import)"
    try:
        X.set_lang("en")
        app = X.run_gui(root, mainloop=False)[-1]
        src = write_xcs(t / "s.xcs")
        app["add_paths"]([str(src)])

        def wait(cond, secs=15):
            end = time.time() + secs
            while time.time() < end and not cond():
                root.update()
                time.sleep(0.05)
            return cond()

        assert wait(lambda: app.get("shown") is not None), "preview not ready"
        app["lb_action"]("open")
        assert wait(lambda: fake.received), app["log_box"].get("1.0", "end")
        path, headers, body = fake.received[-1]
        assert path == "/api/file/open" and headers["X-Filename"] == "s.lbrn2"
        assert b"<LightBurnProject" in body
        assert wait(lambda: "LightBurn: opened s.lbrn2" in app["log_box"].get("1.0", "end"))
        assert not list(t.glob("*.lbrn2"))                 # nothing written to disk
        # material editor: import from the library selected in LightBurn
        ed = X.open_material_editor(root, app["mstate"]["db"], lambda d: None, cfg=app["cfg"])
        ed._import_lb()
        assert wait(lambda: any(x[0] == "info" for x in shown)), shown
        names = [p["name"] for p in ed._work["profiles"]]
        assert "Birch 3 mm" in names, names
        ed.destroy()
    finally:
        messagebox.showinfo, messagebox.showerror, messagebox.askyesnocancel = saved
        root.destroy()
        import gc
        gc.collect()   # free Tk objects in the main thread, not in a later worker thread
        fake.close()
        paths.__exit__()
        X.set_lang("en")


def _R():
    sys.path.insert(0, str(ROOT / "tests"))
    import rld_synth
    return rld_synth


def _rld_file(t, **kw):
    path = t / kw.pop("name", "proj.rld")
    path.write_bytes(_R().build(**kw))
    return path


def _png_rows(png):
    """(color type, rows without the filter byte) of a PNG written by the converter."""
    import zlib
    w, h, _bd, ctype = struct.unpack(">IIBB", png[16:26])
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    n = w * (1 if ctype == 0 else 3) + 1
    return ctype, [raw[i * n + 1:(i + 1) * n] for i in range(h)]


def test_rld_layers_and_geometry():
    X.set_lang("en")
    R = _R()
    t = tmpdir()
    gray = [[0, 255, 0], [255, 0, 255]]   # 3 x 2 pixels, top row first
    objs = [
        ("path", "000000", True, R.rect(1160, 770, 30, 20)),
        ("path", "ff0000", False, [R.line(100, 700, 110, 700),
                                   R.curve((110, 700), (120, 710), (115, 700), (120, 705))]),
        ("bitmap", "00ff00", (500.0, 530.0, 600.0, 620.0), R.bmp8(3, 2, gray)),
        ("path", "123456", True, R.rect(10, 10, 5, 5)),     # color outside the palette
        # closed path whose last point is not the first one: a closing line is added
        ("path", "ff0000", True, [R.line(0, 0, 10, 0), R.line(10, 0, 10, 10)]),
    ]
    path = _rld_file(t, vector={-1: {"speed": 123.0, "min": 11.0, "max": 22.0, "air": 0},
                                1: {"scan": True, "interval": 0.15, "passes": 3, "out": 0}},
                     bitmap={2: {"speed": 77.0, "min": 33.0, "max": 44.0, "flags": (1, 0, 0)}},
                     objects=objs)
    assert X.is_rld(path)
    logs = []
    cv = X.read_project(path, logs.append)[0]
    assert cv.machine.startswith("RDWorks") and "1200 x 800" in cv.machine
    assert [it.kind for it in cv.items] == ["path", "path", "bitmap", "path", "path"]
    black, red, img, odd, tri = cv.items
    assert black.rd_spec["kind"] == "cut" and black.rd_spec["speed"] == 123.0
    assert (black.rd_spec["min_power"], black.rd_spec["power"], black.rd_spec["air"]) == \
        (11.0, 22.0, 0)
    rs = red.rd_spec
    assert (rs["kind"], rs["interval"], rs["passes"], rs["off"]) == ("fill", 0.15, 3, 1)
    assert img.rd_spec["kind"] == "image" and img.rd_spec["speed"] == 77.0
    # the negative flag is not verified yet: never carried over, a warning instead
    assert (img.rd_spec["power"], img.rd_spec["negative"], img.rd_spec["dither"]) == \
        (44.0, 0, "threshold")
    assert sum("Negative Engrave" in m for m in logs) == 1, logs
    assert odd.rd_spec["off"] == 1 and odd.rd_spec["power"] == 0.0
    assert any("123456" in m for m in logs)
    # Y axis: RDWorks counts up from the bed bottom, the converter's screen space counts down
    sp = black.subpaths[0]
    assert sp.closed and len(sp.pts) == 4 and len(sp.segs) == 4
    assert (1190.0, 10.0) in sp.pts and (1160.0, 30.0) in sp.pts
    sp = red.subpaths[0]
    assert not sp.closed and sp.pts == [(100, 100), (110, 100), (120, 90)]
    assert sp.segs[1] == ("C", (115, 100), (120, 95))
    sp = tri.subpaths[0]
    assert sp.closed and sp.pts == [(0, 800), (10, 800), (10, 790)] and len(sp.segs) == 3
    # bitmap: BMP stored bottom-up -> PNG top row first; placed at its frame
    assert img.ow == 3 and img.oh == 2 and img.matrix == [10.0, 0, 0, 10.0, 500.0, 180.0]
    assert _png_rows(img.png) == (0, [bytes(gray[0]), bytes(gray[1])])
    # any of the three unverified bitmap switches gives the warning
    for flags in ((0, 1, 0), (0, 0, 1)):
        logs = []
        X.read_project(_rld_file(t, name="f.rld", bitmap={2: {"flags": flags}}, objects=objs[2:3]),
                       logs.append)
        assert any("Negative Engrave" in m for m in logs), (flags, logs)
    logs = []
    X.read_project(_rld_file(t, name="f.rld", objects=objs[2:3]), logs.append)
    assert not any("WARNING" in m for m in logs), logs


def test_rld_layer_warnings():
    X.set_lang("en")
    R = _R()
    t = tmpdir()
    objs = [("path", "ff0000", True, R.rect(10, 10, 5, 5)),
            ("path", "00ff00", True, R.rect(30, 10, 5, 5))]
    # "0,15" typed with a comma ends up as 0.00254 in RDWorks; unknown mode byte
    logs = []
    cv = X.read_project(_rld_file(t, vector={1: {"scan": True, "interval": 0.00254},
                                             2: {"mode": 7}}, objects=objs), logs.append)[0]
    assert any("very small line interval" in m and "#ff0000" in m for m in logs), logs
    assert cv.items[0].rd_spec["kind"] == "fill"
    g = cv.items[1].rd_spec
    assert (g["kind"], g["off"]) == ("cut", 1)
    assert any("unknown RDWorks processing mode (7)" in m and "output off" in m
               for m in logs), logs
    # every warning is logged once, with the WARNING prefix
    warns = [m for m in logs if "WARNING" in m]
    assert len(warns) == len(set(warns)) == 2, warns
    # invalid speed (negative, zero, huge): 100 mm/s and a warning
    for bad in (-50.0, 0.0, 1e5, float("nan")):
        logs = []
        cv = X.read_project(_rld_file(t, name="v.rld", vector={1: {"speed": bad}},
                                      objects=objs[:1]), logs.append)[0]
        assert cv.items[0].rd_spec["speed"] == 100.0, bad
        assert any("invalid RDWorks layer speed" in m for m in logs), (bad, logs)
    # bitmap on the black layer: vector settings of that layer, Image layer, warning
    logs = []
    cv = X.read_project(_rld_file(t, name="k.rld", vector={-1: {"speed": 55.0, "max": 40.0}},
                                  objects=[("bitmap", "000000", (0.0, 2.0, 0.0, 1.0),
                                            R.bmp8(2, 1, [[0, 255]]))]), logs.append)[0]
    s = cv.items[0].rd_spec
    assert (s["kind"], s["speed"], s["power"], s["dither"]) == ("image", 55.0, 40.0, "threshold")
    assert any("black layer" in m for m in logs), logs
    # closed path whose end is within the tolerance of its start: no extra closing segment
    pts = R.rect(10, 10, 5, 5)
    t3, v = pts[-1]
    pts[-1] = (t3, v[:2] + (v[2] + 1e-4, v[3]) + v[4:])
    cv = X.read_project(_rld_file(t, name="c.rld", objects=[("path", "ff0000", True, pts)]),
                        quiet)[0]
    sp = cv.items[0].subpaths[0]
    assert sp.closed and len(sp.pts) == len(sp.segs) == 4
    # empty project: warning, no summary line
    logs = []
    cv = X.read_project(_rld_file(t, name="e.rld"), logs.append)[0]
    assert not cv.items and any("no objects found" in m for m in logs)
    assert not any("layers:" in m for m in logs), logs


def test_rld_bmp_formats():
    R = _R()
    w2 = [[0, 1, 1, 0, 1, 0, 0, 1, 1], [1, 0, 0, 1, 0, 1, 1, 0, 0]]
    # 1 bit, black and white: gray PNG, 2 levels
    png, w, h, lv = X._bmp_to_png(R.bmp(1, 9, w2))
    assert (w, h, lv) == (9, 2, 2)
    assert _png_rows(png) == (0, [bytes(255 * v for v in r) for r in w2])
    # 4 bit with a gray palette, stored top-down
    rows = [[0, 5, 15], [15, 10, 0]]
    png, w, h, lv = X._bmp_to_png(R.bmp(4, 3, rows, top_down=True))
    assert (w, h, lv) == (3, 2, 3)
    assert _png_rows(png) == (0, [bytes(v * 17 for v in r) for r in rows])
    # 8 bit with a color palette: RGB PNG
    pal = [(255, 0, 0), (0, 0, 255)] + [(0, 0, 0)] * 254
    png, w, h, lv = X._bmp_to_png(R.bmp(8, 2, [[0, 1]], palette=pal))
    assert lv == 256 and _png_rows(png) == (2, [bytes((255, 0, 0, 0, 0, 255))])
    # 8 bit with a palette shorter than 256 entries (biClrUsed = 2)
    png, w, h, lv = X._bmp_to_png(R.bmp(8, 3, [[0, 1, 0]], palette=[(0, 0, 0), (255, 255, 255)]))
    assert lv == 2 and _png_rows(png) == (0, [bytes((0, 255, 0))])
    # 24 bit: color stays RGB, black and white becomes a 2-level gray image (Threshold)
    png, *_ = X._bmp_to_png(R.bmp(24, 2, [[(1, 2, 3), (250, 251, 252)]]))
    assert _png_rows(png) == (2, [bytes((1, 2, 3, 250, 251, 252))])
    png, w, h, lv = X._bmp_to_png(R.bmp(24, 2, [[(0, 0, 0), (255, 255, 255)]] * 2))
    assert lv == 2 and _png_rows(png) == (0, [bytes((0, 255))] * 2)
    # 32 bit with alpha: transparent pixels become white (not burned)
    png, *_ = X._bmp_to_png(R.bmp(32, 3, [[(0, 0, 0, 0), (0, 0, 0, 255), (100, 100, 100, 128)]]))
    assert _png_rows(png) == (0, [bytes((255, 0, 178))])
    # the alpha decision is made for the whole image: a fully transparent margin row stays white
    png, *_ = X._bmp_to_png(R.bmp(32, 2, [[(0, 0, 0, 0)] * 2, [(0, 0, 0, 255), (9, 8, 7, 255)]]))
    assert _png_rows(png) == (2, [bytes((255,) * 6), bytes((0, 0, 0, 9, 8, 7))])
    # 32 bit with an empty alpha channel (common for "XRGB"): alpha ignored
    png, *_ = X._bmp_to_png(R.bmp(32, 1, [[(9, 8, 7, 0)]]))
    assert _png_rows(png) == (2, [bytes((9, 8, 7))])
    # BI_BITFIELDS: the usual B, G, R(, A) layout is read; alpha mask 0 means no alpha
    std = (0xff0000, 0xff00, 0xff)
    png, *_ = X._bmp_to_png(R.bmp(32, 1, [[(9, 8, 7, 0)]], comp=3, masks=std))
    assert _png_rows(png) == (2, [bytes((9, 8, 7))])
    png, *_ = X._bmp_to_png(R.bmp(32, 2, [[(9, 8, 7, 0), (9, 8, 7, 255)]], comp=3,
                                  masks=std + (0xff000000,)))
    assert _png_rows(png) == (2, [bytes((255, 255, 255, 9, 8, 7))])
    png, *_ = X._bmp_to_png(R.bmp(32, 2, [[(9, 8, 7, 0), (9, 8, 7, 255)]], comp=3,
                                  masks=std + (0,)))
    assert _png_rows(png) == (2, [bytes((9, 8, 7, 9, 8, 7))])
    for hdr in (108, 124):   # V4 / V5 headers keep the masks at the same place
        png, *_ = X._bmp_to_png(R.bmp(32, 2, [[(9, 8, 7, 0), (9, 8, 7, 255)]], comp=3,
                                      masks=std + (0xff000000,), header=hdr))
        assert _png_rows(png) == (2, [bytes((255, 255, 255, 9, 8, 7))]), hdr
    # only two gray shades that are not black and white: still grayscale, not Threshold
    png, w, h, lv = X._bmp_to_png(R.bmp(24, 2, [[(100, 100, 100), (200, 200, 200)]]))
    assert lv == 3 and _png_rows(png) == (0, [bytes((100, 200))])
    small_header = bytearray(R.bmp(8, 2, [[0, 1]]))
    small_header[14:18] = struct.pack("<I", 12)
    for bad in (R.bmp(8, 2, [[0, 1]], comp=1), b"BM" + b"\x00" * 60, R.bmp(8, 2, [[0, 1]])[:70],
                bytes(small_header),
                R.bmp(32, 1, [[(9, 8, 7, 0)]], comp=3, masks=(0xff, 0xff00, 0xff0000)),
                R.bmp(24, 1, [[(9, 8, 7)]], comp=3, masks=std),
                R.bmp(32, 1, [[(9, 8, 7, 0)]], comp=3, masks=std + (0xff,)),
                R.bmp(32, 1, [[(9, 8, 7, 0)]], comp=3, masks=std + (0xff000000,))[:68]):
        try:
            X._bmp_to_png(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad[:20])


def test_rld_convert_and_errors():
    X.set_lang("en")
    R = _R()
    t = tmpdir()
    path = _rld_file(t, objects=[("path", "000000", True, R.rect(1160, 770, 30, 20)),
                                 ("path", "ff0000", True, R.rect(1100, 770, 30, 20))],
                     vector={1: {"speed": 25.0, "min": 20.0, "max": 60.0}})
    root = ET.parse(convert(path, t, (True, True))).getroot()
    names = [val(c, "name") for c in layers(root)]
    assert names == ["000000 Cut 30% 100", "FF0000 Cut 20-60% 25"], names
    assert "RDWorks" in root.find("Notes").get("Notes", "")
    shapes = root.findall("Shape")
    assert len(shapes) == 2 and all(s.get("Type") == "Path" for s in shapes)
    # damaged / truncated files give readable errors, not crashes
    for name, data in (("cut.rld", path.read_bytes()[:5000]),
                       ("junk.rld", b"\x0fRDFILEVER8.0.01a" + b"\x00" * 100),
                       ("other.rld", b"RDWorks 9 file? " + b"\x00" * 100)):
        (t / name).write_bytes(data)
        try:
            X.convert_file(t / name, t, quiet)
        except ValueError as e:
            assert "RDWorks" in X.friendly_error(e), X.friendly_error(e)
        else:
            raise AssertionError(name)
    # file cut off in the middle of an object: the rest is read, the loss is reported
    data = path.read_bytes()
    (t / "short.rld").write_bytes(data[:len(data) - 100])
    logs = []
    cv = X.read_project(t / "short.rld", logs.append)[0]
    assert len(cv.items) == 1 and any("could not be read (1;" in m for m in logs), logs
    # NaN in a segment and an unknown segment type: object skipped and reported
    for seg in ((0, (float("nan"), 1, 2, 3, 0, 0, 0, 0)), (5, (0, 0, 1, 1, 0, 0, 0, 0))):
        logs = []
        cv = X.read_project(_rld_file(t, name="bad.rld", objects=[
            ("path", "000000", False, [R.line(0, 0, 1, 1), seg]),
            ("path", "000000", True, R.rect(0, 0, 5, 5))]), logs.append)[0]
        assert len(cv.items) == 1 and any("could not be read" in m for m in logs), (seg, logs)
    # file cut off inside a bitmap: counted as unreadable
    logs = []
    data = _rld_file(t, name="bm.rld", objects=[
        ("path", "000000", True, R.rect(0, 0, 5, 5)),
        ("bitmap", "00ff00", (0.0, 2.0, 0.0, 1.0), R.bmp8(2, 1, [[0, 255]]))]).read_bytes()
    (t / "bm.rld").write_bytes(data[:len(data) - 45])
    cv = X.read_project(t / "bm.rld", logs.append)[0]
    assert [it.kind for it in cv.items] == ["path"]
    assert any("could not be read (1;" in m for m in logs), logs
    # compressed bitmap: skipped with a warning, the rest converts
    bmp = R.bmp(8, 2, [[0, 1]], comp=1)
    logs = []
    cv = X.read_project(_rld_file(t, name="rle.rld", objects=[
        ("bitmap", "00ff00", (0.0, 2.0, 0.0, 1.0), bmp),
        ("path", "000000", True, R.rect(0, 0, 5, 5))]), logs.append)[0]
    assert [it.kind for it in cv.items] == ["path"]
    assert any("unsupported format" in m for m in logs), logs
    # random bytes after the tables must not produce phantom objects
    import random
    rnd = random.Random(1)
    noise = bytes(rnd.getrandbits(8) for _ in range(200000))
    (t / "noise.rld").write_bytes(R.build() + noise)
    cv = X.read_project(t / "noise.rld", quiet)[0]
    assert not cv.items


def test_rld_real_samples():
    """Optional: XS2LB_RLD_SAMPLES=folder with real RDWorks files; each must convert."""
    folder = os.environ.get("XS2LB_RLD_SAMPLES")
    if not folder:
        return "skipped: set XS2LB_RLD_SAMPLES to a folder with .rld files"
    files = sorted(Path(folder).glob("*.rld"))
    assert files, folder
    t = tmpdir()
    for f in files:
        for out in X.convert_file(f, t, quiet, True, False, overwrite=True):
            ET.parse(out)


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    skipped = 0
    for n, f in tests:
        res = f()
        if isinstance(res, str) and res.startswith("skipped"):
            skipped += 1
            print("SKIP", n, "(%s)" % res)
        else:
            print("OK", n)
    print("ALL %d TESTS PASSED%s" % (len(tests) - skipped,
                                      " (%d skipped)" % skipped if skipped else ""))


if __name__ == "__main__":
    main()
