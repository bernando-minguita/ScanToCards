# Architecture

How ScanToCards is put together: the layers, the data that flows
between them, the threading model and the rules the code follows. It is meant for
someone who has to change the application, not for someone who wants to use it -
that is [USER_GUIDE.md](USER_GUIDE.md).

Everything runs on one computer. There is no server, no database service and no
network call at runtime (the single exception is the optional one-time download of
the face-blur model, see §7).

---

## 1. Layers

The code is split so that the heavy work never touches Qt and the Qt code never
touches OpenCV directly:

```
main.py / cli.py          entry points
      │
      ├── app/            Qt application: window, pages, settings, history
      │        │
      │        ├── widgets/   reusable QWidgets (page view, gallery, dialogs)
      │        └── workers/   QThread wrappers around core/
      │
      └── core/           pure Python + OpenCV + PyMuPDF + Pillow
                          (no Qt import anywhere in this package)
```

| Layer | May import | Must not import |
| --- | --- | --- |
| `core/` | `cv2`, `numpy`, `PIL`, `fitz`, stdlib | `PySide6`, `app`, `widgets`, `workers` |
| `widgets/` | `PySide6`, `app.styles`, `app.icons`, `core.models` | `app.main_window`, `workers` |
| `workers/` | `PySide6.QtCore`, `core.*` | `app`, `widgets` |
| `app/` | everything | — |

`core/` never importing Qt is what makes `cli.py` possible: the headless batch
extractor runs the exact same detector, classifier and exporter as the GUI, on a
machine without a display.

---

## 2. Directory map

```
main.py                     GUI entry point; opens files given on the command line
cli.py                      headless batch extraction (argparse, no Qt)
run.bat                     launcher for main.py

app/
  main_window.py            MainWindow: pages, menus, workflow, Add Region state
                            machine, document sessions, project commands
  settings_page.py          SettingsPage: every settings widget + document types
  history_page.py           HistoryPage: the export history table
  settings.py               AppSettings dataclass, JSON persistence, atomic writes
  history.py                HistoryEntry / HistoryStore (JSON, atomic writes)
  session.py                DocumentSession: one open file + its detected cards
  styles.py                 palette constants + the global Qt style sheet
  icons.py                  vector icons generated from embedded SVG strings

core/
  pdf_loader.py             PdfDocument: PDF (PyMuPDF) and image (Pillow) pages,
                            DPI-aware rendering, is_supported_file()
  image_preprocessor.py     illumination correction, thresholds, hole filling
  card_detector.py          CardDetector: six strategies, scoring, merging
  perspective_corrector.py  quad ordering, warping, rotation, IoU helpers
  orientation_detector.py   word-shape heuristic + optional OCR refinement
  document_classifier.py    keyword/geometry classification, identifier extraction
  barcode_reader.py         optional QR/barcode identifiers (pyzbar)
  ocr_engine.py             optional Tesseract / EasyOCR backend
  face_detector.py          optional face detection for the privacy blur
  image_exporter.py         filenames, DPI-aware saving, face blur
  database.py               ProjectStore: SQLite project + document library
  models.py                 DocumentType, DetectedCard, quad helpers
  page_range.py             tolerant "1,3,5-7" parser (GUI + CLI)

widgets/
  pdf_viewer.py             QGraphicsView page view with draggable quads
  document_gallery.py       thumbnail gallery + CardItemDelegate
  library_sidebar_widget.py the Document Library panel in the sidebar
  crop_editor.py            large crop / orientation editor dialog
  region_size_dialog.py     width/height dialog for "Add Region → Custom…"
  progress_widget.py        status bar progress + cancel button
  qt_image.py               numpy ↔ Qt image conversion

workers/
  detection_worker.py       QThread: render → detect → warp → orient → classify
  export_worker.py          QThread: re-render pages, warp, save

tests/                      pytest suite (see §10); conftest.py makes Qt headless
assets/icons/               note about the vector icons
assets/models/              YuNet face model for the privacy blur (+ licence note)
output/                     default export folder (git-ignored)
```

---

## 3. The two data models

There are two independent representations of "a document", and confusing them is
the most common source of bugs.

### 3.1 `DocumentSession` - the editing state (in memory)

`app/session.py` holds one `DocumentSession` per file opened in the canvas:

```python
DocumentSession(
    path=Path, page_count=int,
    cards=[DetectedCard, …],
    detected_pages={0, 2},      # which pages already had a detection run
    dirty=bool, password=str,   # password is memory-only, never persisted
)
```

`MainWindow` keeps a list of sessions plus `_session_index`; the `session`
property returns the current one (or `None`). The drop-down in the top right of
the Extract page switches between them.

`DetectedCard` (`core/models.py`) is the unit of work:

| Field | Meaning |
| --- | --- |
| `card_id` | UUID string, stable across edits |
| `quad_norm` | 4×2 float array, **normalised** (0–1) page coordinates |
| `page_index` | zero-based page number |
| `rotation` | 0 / 90 / 180 / 270, applied before export |
| `fine_rotation` | ±45° sub-degree correction from the crop editor |
| `doc_type` | `DocumentType` instance (identity comparison works) |
| `confidence` | detector score 0–1 |
| `orientation_confident` | `False` → amber "Check orientation" badge |
| `included` | `False` → excluded from the export |
| `identifier`, `ocr_text` | filled by OCR/barcode when available |
| `thumbnail` | small BGR `numpy` array for the gallery |

Geometry is stored **normalised** on purpose: the preview renders at 130 dpi and
the export at 300 dpi, so a pixel coordinate captured in the preview would be
wrong by the time the export runs. `card.quad_pixels(w, h)` converts on demand.

### 3.2 `ProjectStore` - the persisted state (SQLite)

`core/database.py` wraps a small SQLite database. It is the only object the UI
talks to; `sqlite3` never leaks into Qt code.

```
projects          one row: name, timestamps, file_path, active file/page
library_items     one row per file: path, name, size, status, page_count, order
detected_regions  one row per card: file_id, page, doc_type, x/y/w/h,
                  rotation, confidence, orientation_ok, is_included, quad_json,
                  fine_rotation
```

A project that has not been saved yet lives in an **in-memory** database. `Save`
copies it to the chosen `.dceproj` file with SQLite's backup API (so the copy is
always consistent) and continues on that file. `SCHEMA_VERSION` is 3; opening an
older file migrates it in place.

`DetectedRegion` stores the quad as `quad_json` and the absolute path as
`file_path` (denormalised) plus `canonical_path` (`normcase(resolve())`) so a
file can be re-linked after it was removed and added again.

**Which one wins?** The session is the live editing state; the database is written
from it (`_save_current_session_regions()`) before anything that would discard
it - detection runs, project saves, closing a document. Nothing reads the
database back into a session except opening a library file, which reconstructs
the cards from `detected_regions`.

---

## 4. The window

`MainWindow` is a `QStackedWidget` of four pages plus a fixed sidebar:

```
┌──────────────┬──────────────────────────────────────────────────────┐
│ sidebar      │  index 0  Dashboard      statistics, quick start,    │
│              │                          recent files                │
│ brand        │  index 1  Extract        canvas + gallery + toolbar  │
│ nav (4)      │  index 2  History        export history table        │
│ library      │  index 3  Settings       forms, document types       │
│ OCR status   │                                                      │
└──────────────┴──────────────────────────────────────────────────────┘
                   status bar: Pages / Detected / Approved + progress
```

* `_switch_page(index)` is the **only** way to change the page. It sets the
  index, re-colours the nav buttons, refreshes the dashboard or history table
  when they come into view, and calls `_update_menu_state()` so the View menu
  stays in sync (the entry for the page already on screen is greyed out).
* `_show_extract_page(require_document=False)` is the helper the workflow
  commands use to land on the canvas. It does nothing when the canvas is already
  visible (no pointless dashboard rebuild). With `require_document=True` it also
  does nothing when `self.session is None` - that is what Add File / Add Folder
  pass, because those only fill the library and switching to an empty canvas
  would hide the list the user just changed.

### 4.1 State updates

Two functions own "what is enabled right now":

| Function | Owns |
| --- | --- |
| `_update_state()` | status bar counters, toolbar buttons, page spinner, document combo, gallery filters, then delegates to `_sync_card_controls()` and `_update_menu_state()` |
| `_update_menu_state()` | every `QAction` in `self._menu_actions`, including the checkable *Include in Export* and *Add Region* entries |

`_menu_actions` is a plain `dict[str, QAction]` filled while the menu is built,
so the state code can grey out entries without walking the menu tree. The rule
is: **any slot that changes what a command could do must end with one of these
two calls.** The current call sites are card selection (`_on_card_selected`),
type changes (`set_card_type`), quad edits (`_on_quad_edited`), the Add Region
toggle (`_toggle_add_mode`), page changes, detection start/finish and busy-state
changes.

`_sync_card_controls()` handles the per-card row under the gallery (rotate,
crop, type combo, include checkbox) and is guarded so it never re-enters itself
while it is programmatically updating the widgets.

### 4.2 The Add Region state machine

`_btn_add_region` is a checkable toolbar button that is mirrored by a checkable
View-menu entry. Both paths go through `_toggle_add_mode()`, which:

1. flips the button,
2. tells `PdfPageView` to start (or stop) drawing a rubber-band rectangle,
3. calls `_update_menu_state()` so the menu tick matches the button.

`Esc` and finishing a rectangle both leave the mode. The fixed-size presets
(ID card 85.6 × 54 mm, …) are resolved in `widgets/region_size_dialog.py` and
only affect the rectangle the user draws, never the detector.

---

## 5. The detection pipeline

```
PDF page ──render (render_dpi, capped to native)──► BGR image
image file ──decode (native pixels, EXIF applied)─► same pipeline
   │
   ├─ working copy (max 2200 px) - all geometry is normalised, so the export
   │  can re-render the page at full resolution later
   ├─ pre-processing: grey, denoise, flat-field correction, CLAHE, Scharr
   ├─ six candidate strategies (adaptive, Otsu, edge outline, gradient,
   │  colour/saturation, connected components)
   ├─ contour → polygon → rectangle validation (area, aspect, rectangularity,
   │  extent, edge support, convexity, "not the whole page")
   ├─ scoring + per-strategy weight, IoU duplicate removal, nesting resolution
   ├─ quad refinement (snap each edge onto the strongest nearby gradient)
   └─ padding (0.6 % of the diagonal)
```

Each accepted card is warped (`getPerspectiveTransform` + `warpPerspective`),
orientation-analysed and classified. `workers/detection_worker.py` runs this in a
`QThread` and emits `card_found` per card, so the gallery fills up while the run
is still going.

**Cancellation** is cooperative and checked at page boundaries and between the
expensive stages of a card (`_cancel_requested`). A run that is cancelled still
keeps the cards found so far.

**Frozen inputs.** The worker deep-copies the settings and the export worker
copies the cards (`ExportJob`), because the GUI keeps editing them while the
thread runs. A mid-run change can therefore never tear a configuration.

---

## 6. Export

`workers/export_worker.py` takes a list of `ExportJob` (one file + its cards) and
an `ExportOptions`, re-renders each page **once** at full resolution, warps every
included card, applies the face blur if requested and saves it.

Filenames come from `core/image_exporter.py`:

```
{type}_{identifier}_{page}_{sequence}      e.g. Passport_WE4110283_P01_01.jpg
```

`{type}` is `DocumentType.file_token` (no spaces), `{page}` is `P01`, `{sequence}`
is a 3-digit counter. Illegal filename characters are stripped, and an existing
file is never overwritten - a `_2`, `_3`, … suffix is appended instead.

Every finished run appends a `HistoryEntry` to `app/history.py` (JSON, capped at
`MAX_ENTRIES = 200`, atomic write). The history page and the dashboard both read
from that single store.

---

## 7. Persistence and file locations

| What | Where | Written by |
| --- | --- | --- |
| Settings | `%LOCALAPPDATA%\ScanToCards\settings.json` | `AppSettings.save()` |
| History | same folder, `history.json` | `HistoryStore.save()` |
| Projects | wherever the user saves a `.dceproj` file | `ProjectStore.save_as()` |
| Exports | `output/<file name>/` by default | `ImageExporter` |
| Face model | `assets/models/` (or `%LOCALAPPDATA%\…\models\`) | ships with the app |

`app/settings.py::_write_json_atomic()` writes to a temporary file in the same
folder and then `os.replace()`s it, so a crash mid-write can never truncate the
previous file. A corrupt JSON file is renamed to `*.invalid.json` instead of
being silently overwritten.

The only network access in the whole application is the **Download model…**
button on the settings page, which fetches the 227 KB YuNet face detector once
when it is missing. Everything else is offline.

`AppSettings` is a plain dataclass; unknown keys in an older/newer JSON file are
ignored and missing ones fall back to the field default, which is what makes
settings forward/backward compatible without a migration step.

---

## 8. Threading rules

1. **Never touch a widget from a worker.** Workers only emit signals; every slot
   that touches Qt widgets lives on `MainWindow` and runs in the GUI thread.
2. **Snapshot what the thread reads.** `DetectionWorker` deep-copies settings,
   `ExportJob` copies cards, `DocumentSession.password` is read once into the
   worker constructor.
3. **Cancellation is a flag, not a kill.** `request_cancel()` sets
   `_cancel_requested`; the worker checks it and emits `cancelled`. There is no
   `terminate()` anywhere in the codebase.
4. **Clear the reference after `finished`, not after `finished_ok`.** The
   `finished_ok`/`cancelled` signals arrive while the thread can still be
   running; `closeEvent` waits on `self._detection_worker`, which is cleared in
   `_on_detection_thread_finished` (connected to `QThread.finished`).
5. **One run at a time.** `self._busy` is set before the thread starts and
   cleared in the finish/cancel/fail slots; `_update_state()` greys out every
   command that would start a second run.

---

## 9. Error handling

The rule is *report, never crash*:

| Situation | Behaviour |
| --- | --- |
| Unreadable / corrupt PDF or image | `PdfError` → `QMessageBox` with the reason, session untouched |
| Password protected PDF | prompt once, keep the password in `DocumentSession` (memory only), re-prompt when it was wrong |
| Page fails to render | the page is reported, the rest of the batch continues |
| No document found on a page | status hint "No documents found on this page", no dialog |
| Face blur requested but unavailable | every image is still exported; the summary lists what was skipped |
| Malformed page range | `cli.py` prints a usage error and exits 2; the GUI ignores the unparsable parts |
| Settings/history file corrupt | renamed to `*.invalid.json`, defaults are used |
| Project cannot be opened at startup | warning, then a new empty project |

Detection warnings are collected in `self._detection_errors` and shown once when
the run finishes, not per page - a batch of 50 pages must not produce 50 dialogs.

---

## 10. Testing

```
pip install -r requirements-dev.txt
python -m pytest tests -q
```

214 tests, about 2 minutes. `QT_QPA_PLATFORM=offscreen` is set in
`tests/conftest.py`, so the GUI tests need no display.

| File | Covers |
| --- | --- |
| `conftest.py` | generates a synthetic multi-card PDF + image, so most tests run without `sample.pdf` |
| `test_units.py` | geometry, warping, filenames, classification, orientation, settings/history persistence, page ranges, face-detector selection |
| `test_detection.py` | the real detector on `sample.pdf` and on a synthetic card |
| `test_workers.py` | worker threads run synchronously: sequencing, cancellation, per-page errors, frozen snapshots |
| `test_database.py` | project/library/region round-trips, re-linking, save-as |
| `test_canvas_and_cards.py` | page view and card editing |
| `test_library_and_project.py` | library sidebar + project commands |
| `test_gui.py` | end-to-end offscreen `MainWindow`: open → detect → edit → export → history |
| `test_ux_requests.py` | the interaction details: menus, state wiring, page switching, dialogs |

---

## 11. Conventions

* **Type hints everywhere**, `from __future__ import annotations` at the top of
  every module.
* **Docstrings explain *why*.** The code says what it does; the docstring says
  which bug it prevents.
* **Comments are for the non-obvious.** A comment that restates the next line is
  removed; a comment that explains why `finished` is used instead of
  `finished_ok` stays.
* **No Qt in `core/`.** If a helper needs a `QMessageBox`, it belongs in `app/`.
* **Normalised geometry.** Never store pixel coordinates that outlive the render
  that produced them.
* **New settings** go into `AppSettings` with a default, into the settings page,
  and into the README settings table - in that order.
