# Changelog

All notable changes to this project are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [2.1] - 2026-11-12

### Added
- *Open in LightBurn* and *Add to LightBurn project* buttons: the previewed design (with the
  chosen material, mirror settings and layer edits) is sent to a running LightBurn 2.2+ through
  its REST API, without saving a file. Console: `--lightburn open` / `--lightburn import`.
  One-time approval in LightBurn; the secret is stored in `~/.xs2lightburn_lightburn.json`
  (owner-only permissions on Linux/macOS).

### Changed
- Settings are saved atomically, so an interrupted save cannot leave a damaged settings file.
- Material import from LightBurn libraries: *Import...* now also reads `.clb` files, and
  *From LightBurn...* imports the library selected in a running LightBurn 2.2+
  (values converted from LightBurn's display units).

## [2.0] - 2026-11-11

First public release.

### Added
- English and Polish interface. Switch it in the window (*Language / Język*) or with `--lang`.
  The first start follows the system language.
- Translations live in separate files, `locales/<code>.json`. A new language is a new file;
  missing strings fall back to English. Problems in a language file are reported in the log
  and on the console. See *Adding a language* in the README.
- *Mirror X* / *Mirror Y* settings that match LightBurn's *Edit > Device Settings*,
  also as `--mirror-x` / `--no-mirror-x` / `--mirror-y` / `--no-mirror-y`.
- Material database: *Import...* (merges a JSON file, asks about duplicate names),
  *Export...* and *Add examples*.
- Synthetic test suite (`tests/test_synthetic.py`); it needs no sample projects.
- *Sponsor* button on GitHub and a Support section in the README (Buy Me a Coffee).

### Changed
- The material database starts with three example materials, not a full table.
  Example names are English in every language.
- Default device mirroring is now off (LightBurn's default). Settings saved by 1.x
  (`flip_x` / `flip_y`) are converted to the matching Mirror X / Mirror Y values.
- Canvases without their own name get the English default name `Canvas <n>` in output file
  names, in every interface language.
- The window keeps its file list, layer edits, output folder, log and size when the
  language is changed.

### Fixed
- Console output no longer crashes when it is redirected to a file or a non-UTF-8 terminal.

## 1.0 - 1.5 (private versions, not published)

Development versions used by the author: reading of `.xs` and `.xcs`, geometry and bitmaps,
cut / line / fill / image layers, window with preview and layer table, material database
with an editor, per-layer editing, LightBurn tags checked against files saved by LightBurn 2.x.
