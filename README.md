# xs2lightburn

Convert **xTool Studio** projects (`.xs`, `.xcs`) to **LightBurn** projects (`.lbrn2`),
keeping layers and laser operations, not just the geometry.

[Polski opis niżej](#polski)

---

## What it does

- Reads `.xs` (the zipped format of newer xTool Studio versions) and `.xcs` (JSON).
- Transfers geometry: paths, circles, ellipses, rectangles, components (as groups), bitmaps.
- Creates one LightBurn layer per xTool color and operation, with power, speed, passes,
  line interval, scan angle, cross-hatch, dithering, negative image and kerf.
- Objects excluded from processing in xTool go to layers with Output off, so nothing is lost.
- Window with a pan/zoom preview, a layer table and per-layer editing before conversion.
- Optional **material database**: pick a material and its parameters replace the xTool ones.
- English and Polish interface (switch in the window, or `--lang en|pl`). More languages can be
  added as files in `locales/` (see [Adding a language](#adding-a-language)).
- **Open in LightBurn** with one click, and import material settings from a LightBurn library
  (`.clb` file or the library selected in a running LightBurn 2.2+).
- Console mode for batch conversion.

Operation mapping:

| xTool Studio | LightBurn |
|---|---|
| Vector cutting | Line layer (cut) |
| Vector engraving | Line layer |
| Fill engraving | Fill layer (Scan; Offset fill when xTool uses offset fill) |
| Bitmap engraving | Image layer (dither mode, DPI from line density) |

Line density in xTool (lines/cm) becomes the LightBurn interval: `interval mm = 10 / density`.

## Requirements

- Python 3.8 or newer with tkinter (included in the python.org installers for Windows and macOS).
- Optional: [Pillow](https://pypi.org/project/Pillow/) (`pip install pillow`), used to mirror
  bitmap pixels, to read image formats other than PNG/JPG and to show images in the preview.
  Without it PNG/JPG images are still converted (other formats are skipped with a warning).
- Optional: LightBurn 2.2 or newer for *Open in LightBurn* and *From LightBurn...*
  (everything else works with any LightBurn version that opens `.lbrn2` files).

Download the repository (*Code > Download ZIP*) and keep the `locales` folder next to
`xs2lightburn.py`. Without that folder the program still works, but only in Polish.

## Usage

Window:

```
python xs2lightburn.py
```

1. Add `.xs` / `.xcs` files and click a file to see the preview and its layers.
2. Optionally choose a material from the database.
3. Set **Mirror X / Mirror Y** to match your LightBurn device
   (LightBurn: *Edit > Device Settings > Mirror X / Mirror Y*).
4. Optionally select a layer, change its parameters and click *Apply*.
5. Click *Convert all files in the list*. The `.lbrn2` file is saved next to the xTool file
   or in the chosen output folder. Or send the previewed design straight to LightBurn with
   *Open in LightBurn* / *Add to LightBurn project* (see below).

Console:

```
python xs2lightburn.py project.xs other.xcs -o out_folder
python xs2lightburn.py project.xs --mirror-x --mirror-y --material "Example: birch plywood 3 mm"
python xs2lightburn.py project.xs --lightburn open      # convert, then open it in LightBurn
python xs2lightburn.py a.xs b.xs --lightburn import     # convert, add all to the open project
python xs2lightburn.py --list-materials
python xs2lightburn.py --help
```

## Working with a running LightBurn (2.2 or newer)

LightBurn 2.2 has a local REST API, switched on by default. The converter uses it for two
things; everything else works without it.

- **Open in LightBurn** sends the design shown in the preview (with the chosen material,
  mirror settings and layer edits) to LightBurn as a new project. If the project open in
  LightBurn has unsaved changes, LightBurn asks whether to save them first.
  **Add to LightBurn project** adds it to the current project instead. Nothing is written to disk: save the project in LightBurn
  (*File > Save*), or use *Convert* to get the `.lbrn2` file.
- **Edit database... > From LightBurn...** imports the material library selected in LightBurn
  (*Library* window, *Select Library*). Values are converted from LightBurn's display units
  to mm and mm/s.

The first time, LightBurn shows a window asking you to approve "xs2lightburn"; you have
30 seconds. The program asks for read-only project and machine state and file upload. The
read-only project permission also covers LightBurn's camera image; this program does not read
it, and it cannot start a job. The approval is remembered as a secret in
`~/.xs2lightburn_lightburn.json`; treat that file like a password. To withdraw it, use
*Manage API Security* in LightBurn's *Settings > Extensions*. The program pairs again
when the secret no longer works.

The API listens on this computer only (`http://127.0.0.1:19520`). It is new and may change
between LightBurn versions.

## Mirror X / Mirror Y

LightBurn devices can have mirrored axes (common on Ruida controllers). The converter writes
coordinates so that the design opens the same way as in xTool Studio on *your* device.
If the converted project opens mirrored, change the two checkboxes (or the `--mirror-x` /
`--mirror-y` options) to match *Edit > Device Settings* in LightBurn and convert again.

## Material database

Stored in `~/.xs2lightburn_materialy.json` (user folder). It starts with **three examples
only**, taken from public tables for a 100 W CO2 laser. They are not tested on your machine.
Example names are in English in both interface languages.
Use *Edit database...* to add your own tested settings, or *Import...* a file:

- a JSON file exported by this program (for example by another user);
- a LightBurn library file (`.clb`), no LightBurn needed. Each material and thickness becomes
  one entry. Settings without a thickness (for example an engrave or image preset) are added to
  every thickness of that material. Vector presets without a thickness, or named like
  "score" / "engrave", become *line engrave*; a second preset of the same kind gets its own
  entry with the description in its name. Sub-layers are not imported.

*From LightBurn...* does the same with the library selected in a running LightBurn.
Imported values are not saved until you click *Save*. *Export...* saves your database to share it.

Each material has a group, a name, a thickness and settings per operation (cut, line, fill,
image). Operations a material does not define (for example cutting on an engrave-only
material) get Output off and power 0, and the layer name is marked.

Window settings are stored in `~/.xs2lightburn.json`.

## Limitations

- Text objects must be converted to paths in xTool Studio first (they are skipped with a warning).
- xTool image adjustments (brightness, contrast, sharpening) are not transferred.
- Only flat laser processing is supported (rotary and other modes are reported).
- LightBurn has 30 color layers (C00-C29); larger projects are reported.
- The `.xs` format is not documented by xTool. It was worked out from real files and may
  change with new xTool Studio versions. Please open an issue with a sample file if a project
  does not convert correctly.

## Support

If the converter saves you time, you can [buy me a coffee](https://buymeacoffee.com/bartwaw).

## Disclaimer

Laser parameters are only starting points. Always run a material test before a real job.
Never leave a laser unattended. This project is not affiliated with xTool or LightBurn Software.
Use at your own risk (see [LICENSE](LICENSE)).

## Adding a language

1. Copy `locales/en.json` to `locales/<code>.json`, for example `locales/de.json`.
   The file name is the language code: two or three lowercase letters, optionally `_` and a
   region in capitals (`pt_BR.json`, not `pt-BR.json`).
2. Set `"language"` to the language's own name (`"Deutsch"`). It must differ from the other
   languages in the list.
3. Translate the **values** in `"strings"`. Do not change the keys (they are the Polish
   source texts). Keep placeholders such as `%s`, `%d`, `%02d` and `%%`, `\n` and leading
   spaces as in the key. Values you leave empty or remove are shown in English.
4. Save the file as UTF-8 (with or without BOM).
5. Run `python tests/test_synthetic.py` and open a pull request.

The program never stops because of a language file: problems (invalid JSON, a wrong file name,
entries with wrong placeholders) are listed in the log window and on the console, and the
affected texts are shown in English.

The new language appears in the *Language / Język* list and in `--lang`.

## Tests

```
python tests/test_synthetic.py
```

The tests build synthetic `.xs` and `.xcs` projects, so no sample files are needed.

Changes are listed in [CHANGELOG.md](CHANGELOG.md).

---

## Polski

Konwerter projektów **xTool Studio** (`.xs`, `.xcs`) do projektów **LightBurn** (`.lbrn2`).
Przenosi warstwy i operacje lasera, a nie tylko geometrię.

### Co robi

- Czyta `.xs` (spakowany format nowszych wersji xTool Studio) i `.xcs` (JSON).
- Przenosi geometrię: ścieżki, okręgi, elipsy, prostokąty, komponenty (jako grupy), bitmapy.
- Tworzy warstwy LightBurna według kolorów i operacji xTool: moc, prędkość, przejścia,
  odstęp linii, kąt, kratka, dithering, negatyw, kerf.
- Elementy wyłączone z obróbki w xTool trafiają na warstwy z wyłączonym wyjściem (Output).
- Okno z podglądem (przesuwanie, powiększanie), tabelą warstw i edycją parametrów warstwy.
- Opcjonalna **baza materiałów**: wybrany materiał zastępuje parametry z xTool.
- Interfejs po angielsku i po polsku (przełącznik w oknie albo `--lang en|pl`). Kolejne języki
  to pliki w folderze `locales/` (opis w części angielskiej: *Adding a language*).
- Tryb konsolowy do konwersji wielu plików.

### Wymagania

- Python 3.8 lub nowszy z tkinter (jest w instalatorach z python.org dla Windows i macOS).
- Opcjonalnie [Pillow](https://pypi.org/project/Pillow/) (`pip install pillow`): odbicie pikseli
  bitmap, odczyt obrazów innych niż PNG/JPG i obrazy w podglądzie. Bez niej obrazy PNG/JPG też
  są przenoszone (inne formaty są pomijane z ostrzeżeniem).

Pobierz całe repozytorium (*Code > Download ZIP*) i trzymaj folder `locales` obok
`xs2lightburn.py`. Bez niego program działa tylko po polsku.

### Użycie

Okno: `python xs2lightburn.py`

1. Dodaj pliki `.xs` / `.xcs` i kliknij plik, żeby zobaczyć podgląd i warstwy.
2. Opcjonalnie wybierz materiał z bazy.
3. Ustaw **Mirror X / Mirror Y** tak jak w LightBurnie
   (*Edit > Device Settings > Mirror X / Mirror Y*).
4. Opcjonalnie zaznacz warstwę, zmień parametry i kliknij *Zastosuj*.
5. Kliknij *Konwertuj wszystkie pliki z listy*. Plik `.lbrn2` powstaje obok pliku xTool
   albo w wybranym folderze.

Konsola: jak wyżej w części angielskiej; `--lang pl` daje komunikaty po polsku.

### Mirror X / Mirror Y

Urządzenie w LightBurnie może mieć odbite osie (częste przy sterownikach Ruida). Konwerter
zapisuje współrzędne tak, żeby projekt otworzył się tak jak w xTool Studio na Twoim urządzeniu.
Jeśli projekt otwiera się odbity, ustaw oba pola jak w *Edit > Device Settings* i przekonwertuj
ponownie.

### Baza materiałów

Plik `~/.xs2lightburn_materialy.json` w folderze użytkownika. Na start zawiera **tylko trzy
przykłady** z publicznych tabel dla lasera CO2 100 W, niesprawdzone na Twojej maszynie.
Przykłady (nazwy i opisy) są po angielsku w obu wersjach językowych.
*Edytuj bazę...* pozwala dopisać własne, sprawdzone parametry. *Importuj...* wczytuje plik JSON
z tego programu (np. od kogoś innego) albo bibliotekę LightBurna (`.clb`), bez uruchamiania
LightBurna. Każdy materiał i grubość z biblioteki to jeden wpis; ustawienia bez grubości (grawer,
obraz) dochodzą do każdej grubości tego materiału. *Z LightBurna...* robi to samo z biblioteką
wybraną w działającym LightBurnie 2.2+. Zaimportowane wartości trafiają do bazy po kliknięciu
*Zapisz*. *Eksportuj...* zapisuje bazę do udostępnienia.

### Praca z działającym LightBurnem (2.2 i nowsze)

LightBurn 2.2 ma lokalne REST API, domyślnie włączone. *Otwórz w LightBurnie* wysyła projekt
z podglądu (z materiałem, odbiciami i zmianami warstw) do LightBurna jako nowy projekt; gdy
w LightBurnie są niezapisane zmiany, LightBurn sam zapyta o ich zapis. *Wstaw do projektu w LightBurnie*
dodaje go do bieżącego projektu. Nic nie jest zapisywane na dysk: zapisz projekt w LightBurnie
(*File > Save*) albo użyj konwersji. W konsoli: `--lightburn open` albo `--lightburn import`.

Za pierwszym razem LightBurn pokazuje okno z prośbą o zgodę dla "xs2lightburn" (30 sekund).
Program prosi o odczyt projektu i stanu maszyny oraz wysyłanie plików. Uprawnienie do odczytu
projektu obejmuje też obraz z kamery LightBurna, ale program go nie czyta; nie może też uruchomić
zadania. Zgoda jest zapamiętana jako sekret w `~/.xs2lightburn_lightburn.json` (traktuj ten plik
jak hasło). Cofniesz ją w LightBurnie: *Settings > Extensions > Manage API Security*.

### Ograniczenia

- Tekst trzeba w xTool Studio zamienić na ścieżkę (inaczej jest pomijany z ostrzeżeniem).
- Korekty obrazu z xTool (jasność, kontrast, wyostrzenie) nie są przenoszone.
- Obsługiwana jest tylko obróbka płaska.
- LightBurn ma 30 warstw kolorów (C00-C29).
- Format `.xs` nie jest opisany przez xTool; został rozpracowany na prawdziwych plikach i może
  się zmienić w nowych wersjach xTool Studio. Jeśli projekt konwertuje się źle, załóż zgłoszenie
  (issue) z przykładowym plikiem.

### Wsparcie

Jeśli konwerter oszczędza Ci czas, możesz [postawić mi kawę](https://buymeacoffee.com/bartwaw).

### Zastrzeżenie

Parametry lasera to tylko punkt wyjścia. Zawsze zrób test materiału przed właściwą pracą.
Nie zostawiaj pracującego lasera bez nadzoru. Projekt nie jest powiązany z xTool ani z LightBurn
Software. Używasz na własną odpowiedzialność (zob. [LICENSE](LICENSE)).
