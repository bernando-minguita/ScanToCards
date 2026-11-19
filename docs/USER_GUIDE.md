# User Guide

A walk through the application from an empty window to a folder full of card
images. Every screen, button and shortcut is described here; the *why* behind the
code lives in [ARCHITECTURE.md](ARCHITECTURE.md).

If the application is not installed yet, follow
[README §2](../README.md#2-installation) first (`pip install -r requirements.txt`,
then `python main.py`).

---

## 1. What the application does

You give it a scan - a PDF page or an image - that contains several documents
lying on a flatbed scanner or photographed on a desk. It finds each document,
straightens it, and writes one image file per document:

```
scan.pdf  ──►  Passport_WE4110283_P01_01.jpg
               Iqama_2567251562_P01_02.jpg
               VehicleRegistration_003_P01_03.jpg
               DriversLicense_004_P01_04.jpg
```

The documents may be landscape or portrait, of different sizes, at different
positions, and slightly rotated or skewed. Nothing is uploaded anywhere.

---

## 2. The window at a glance

```
┌────────────────────────────┬──────────────────────────────────────────────────────────┐
│ ▣ ScanToCards              │  Dashboard                                               │
│   Document Card Extractor  │  Extract passport, Iqama, Istimara and driving licence   │
│                            │  cards from scanned PDF files or images.                 │
│ [ Dashboard    ]           │                                                          │
│ [ Extract Docu…]           │  ┌──────────┐┌──────────┐┌──────────┐┌──────────┐        │
│ [ Extraction H…]           │  │    0     ││    0     ││    0     ││    0     │        │
│ [ Settings     ]           │  │  Files   ││Documents ││ Approved ││Documents │        │
│                            │  │  loaded  ││ detected ││for export││ exported │        │
│ ────────────────           │  └──────────┘└──────────┘└──────────┘└──────────┘        │
│ My scans  2 files          │                                                          │
│ [⟳] [🗑]                    │  Quick start                                             │
│ Search files…              │  1. Open a scanned PDF or image (or drag & drop it …)    │
│ scan.pdf      ✓            │  [ Open File ] [ Go to extraction ] [ Open settings ]    │
│ id_card.png                │                                                          │
│                            │  Recent files                             [ Clear list ] │
│ OCR engine: …              │                                                          │
│ Version 1.0.0 …            │  Pages: 0   Detected: 0   Approved: 0   [ progress ]     │
└────────────────────────────┴──────────────────────────────────────────────────────────┘
```

Four pages, switched with the buttons on the left (or the **View** menu):

| Page | What it is for |
| --- | --- |
| **Dashboard** | counters, quick-start buttons, the recent-files list |
| **Extract Documents** | the working page: page view, gallery, toolbar |
| **Extraction History** | every export you ran, with its output folder |
| **Settings** | rendering, detection, OCR, export, document types |

The **Document Library** panel under the navigation buttons is always visible. It
lists every file of the current project and is the fastest way to jump between
documents: double-click a row to open it in the canvas.

The status bar at the bottom always shows `Pages`, `Detected` and `Approved` for
the current document, plus a progress bar with a **Cancel** button while a run is
going.

---

## 3. First run

1. Start the application (`python main.py`, or double-click `run.bat`).
2. The **Dashboard** opens. It is empty until you open something.
3. Press **Open File** (or drag a PDF/image onto the window).
4. The window switches to **Extract Documents** and renders the first page.
5. Press **Start Detection**.

Detection of one page with four documents takes roughly 1.5–3 s. Cards appear in
the gallery one by one while the run is still going, so you can start reviewing
the first ones before the last one is found.

> **Nothing is saved automatically to a project file.** Card edits are written to
> the in-memory project immediately, but the project only becomes a `.dceproj`
> file on **Save**. The window title shows a `*` while there are unsaved changes.

---

## 4. Opening documents

There are four different ways to get a file into the application, and they do
different things:

| Command | What it does | Where you end up |
| --- | --- | --- |
| **Open File** (`Ctrl+O`) | opens one file *in the canvas*, replacing whatever was open | Extract page |
| **Add Files to Library…** (File menu, or **Add Files** on the toolbar) | adds the files to the **library only** - nothing is opened | stays where you are |
| **Add Folder…** (File menu, or **Add Folder** on the toolbar) | adds every supported file of a folder, recursively, to the library | stays where you are |
| drag & drop onto the window | opens the dropped file(s) in the canvas | Extract page |

The distinction matters: **Open File** is for "process this scan now", **Add
Files/Folder** is for "build up the project's library first". The tooltips on the
buttons repeat this.

Several files opened with **Open File** (or dropped together) sit side by side in
the drop-down at the top right of the Extract page. Switching the drop-down
switches the document without closing the others.

**Accepted formats:** PDF, PNG, JPG/JPEG, TIFF (including multi-page TIFF from a
document scanner), BMP, WEBP. Every frame of a multi-page TIFF becomes a page, so
image scans behave exactly like PDFs.

**Password protected PDFs** ask for the password once. It is kept in memory for
that document for the rest of the session, so detection, preview and export never
ask again; if it was wrong, you are asked again.

---

## 5. Detecting documents

Two buttons on the toolbar:

* **Start Detection** (`Ctrl+R`) - runs on every page (or on the page range typed
  into the page bar, see below).
* **Re-run Page** (`Ctrl+Shift+R`, *Tools → Re-run Current Page*) - runs on the
  page currently on screen only.

If the selected pages already have detected cards, a confirmation asks before they
are replaced. Edits you made are written to the project before the run starts, so
a cancelled run never loses what you already had.

### The page bar

Above the page view, left to right:

| Control | Purpose |
| --- | --- |
| `‹` arrow | previous page |
| page spinner + `of N` | jump to a page |
| `›` arrow | next page |
| `Pages (e.g. 1,3,5-7)` field | limits **Start Detection** to those pages |
| zoom out / zoom in / fit icons | change the zoom of the page view |
| **Add Region** | draw a document by hand (see §7) |
| size drop-down + **Size…** | fixed physical size for hand-drawn regions (see §7) |
| **Remove** | remove the selected document |
| hint at the far right | the current page hint (e.g. the detection state) |

The page range field accepts `1`, `1,3`, `5-7`, `1,3,5-7` and tolerates stray
spaces and separators. Unparsable parts are ignored rather than raising an error.
An empty field means "all pages".

### What you see on the page

Detected documents are drawn as coloured polygons. The colour tells you the
document type (passport blue, Iqama teal, vehicle registration amber, driving
licence purple, …). The selected card is highlighted and shows its four round
corner handles.

---

## 6. Reviewing and editing

Every detected document is a *card*. You can work on it either on the page view
or in the gallery - both stay in sync.

### 6.1 The gallery

Right of the page view (the splitter between them can be dragged), a thumbnail
per card with its type, page number and confidence. The header carries the card
count and a scope drop-down (**This page** / **All pages**); the filter bar below
it holds:

| Filter | Effect |
| --- | --- |
| search box | matches type, identifier, `page 2` and the OCR text |
| type drop-down | show one document type only |
| page drop-down | show one page only |
| **Included only** | hide the cards you excluded from the export |
| **Orientation OK only** | hide the cards with the amber "Check orientation" badge |

A card shows its type, `Page NN`, the confidence, the rotation (when not 0°) and
the identifier. Two states are called out:

| Marker | Meaning |
| --- | --- |
| amber `!` circle + *Check orientation* line | the card may be upside down (180°) - one click on the 180° button fixes it |
| dashed border + *Excluded from export* line, faded thumbnail | the card is **not** included in the export |

### 6.2 Selecting a card

Click it - on the page or in the gallery. The row under the gallery becomes
active: rotate left/right/180, **Adjust crop**, the type drop-down and the
**Include** checkbox. The **Edit** menu enables the same commands.

### 6.3 Moving and reshaping

| Action | How |
| --- | --- |
| move the whole region | drag **inside** the selected card (all four corners move together) |
| correct one corner | drag the round handle |
| nudge by 1 px | arrow keys |
| nudge by 10 px | `Ctrl` + arrows |
| nudge by 20 px | `Alt` + arrows |
| resize | `Shift` + arrows (`→`/`←` wider/narrower, `↑`/`↓` taller/shorter, 1 px per side) |
| resize by 10 / 20 px | `Ctrl` + `Shift` / `Alt` + `Shift` + arrows |
| rotate 90° | the two rotate buttons, or the gallery context menu |
| rotate 180° | the flip button - the usual fix for the amber badge |
| leave Add Region mode | `Esc` |

### 6.4 Adjust crop…

Double-click a card (or press **Adjust crop**) for the large editor:

* the crop is shown at a usable size with the four corner handles,
* a **round grip above the crop** rotates it freely - the *Fine rotation* field
  follows the drag (0.1° precision, ±45°; beyond that the angle is folded into the
  crop and rotation continues),
* **Fine rotation** can also be typed,
* `◀ ▲ ▼ ▶` and `− W / + W / − H / + H` buttons nudge and resize,
* **Margin** ("Add to all sides", e.g. 2 mm) keeps the shadow and the rounded
  corners of the card inside the export,
* the document type and the identifier can be edited here too,
* `Ctrl+Z` / `Ctrl+Y` undo and redo crop, rotation, type, identifier and include.

### 6.5 Document type and include

The type drop-down lists every enabled type. Picking one renames the export file
and is remembered for the project. Untick **Include** to keep a card out of the
export - it stays in the project, so you can bring it back later.

If a type is missing from the drop-down it was unticked in
**Settings → Document types**; see §9.

### 6.6 Removing and exporting single cards

The gallery context menu (right click) contains, in order:

| Entry | Effect |
| --- | --- |
| **Document type** ▸ | submenu with every type, the current one ticked |
| **Rotate left 90°** / **Rotate right 90°** / **Rotate 180°** | rotation |
| **Adjust crop…** | the large crop editor (§6.4) |
| **Show OCR text…** | greyed out while the card has no OCR text |
| **Exclude from export** / **Include in export** | toggles the card |
| **Export this document…** | writes just this card |
| **Remove from list** | removes the card from the project |

Removing a card only removes it from the project - the source file is untouched.

---

## 7. Adding a document by hand

When the detector misses a document (faint edge, a card on a white sheet, a
document photographed at a steep angle), draw it yourself:

1. Press **Add Region** on the page bar (or tick **View → Add Region**).
2. Drag a rectangle over the document.
3. The rectangle becomes a card: it is warped, orientation-analysed and
   classified exactly like a detected one.

`Esc` leaves the mode. The cursor turns into a crosshair while it is active, so
you can see at a glance whether the next drag will draw a region or move a card.

### Fixed sizes

Next to **Add Region** on the page bar there is a size drop-down (and a **Size…**
button that reopens the custom dialog):

| Preset | Size |
| --- | --- |
| Free (use the drawn box) | the drawn rectangle (default) |
| ID card 85.6 × 54 mm | 85.6 × 53.98 mm |
| Business card 90 × 55 mm | 90 × 55 mm |
| Passport page 125 × 88 mm | 125 × 88 mm |
| Custom… | asks for width and height (10–400 mm) |

The same controls exist under **Settings → Add Region size**, where the
*Squeeze a drawn region to a fixed physical size (identity card, passport page, …)*
checkbox turns the behaviour on and the **Width** / **Height** fields hold the
custom size.

With a fixed size the region keeps the **centre** of your drag and takes exactly
that physical size on the scanned page, so an identity card is cropped precisely -
no guessing where its edges are. It only affects the rectangle you draw; the
automatic detector is never constrained by it. These controls stay available even
before a document is opened, and a change made here is stored immediately (it does
not wait for *Save settings*). While a fixed size is active the **Size…** button
shows the current value (e.g. `85.6 × 54.0 mm`) instead of the word.

---

## 8. Exporting

**Export All** (`Ctrl+E`) first asks **which files** to export:

| Answer | Scope |
| --- | --- |
| **Yes** | only the document shown in the canvas |
| **No** | every file in the Document Library that has extracted regions (open or not) |
| **Cancel** | nothing is exported |

Only **included** cards are written; if nothing is included you get a
"Nothing to export" message instead of an empty folder.

**Export Selected Document…** (Tools menu, or the gallery context menu) writes a
single card and asks for its target folder. An excluded card triggers an
"Export it anyway?" confirmation first.

### Output layout

```
output/
└── sample/                      <- one folder per input file (configurable)
    ├── Passport_WE4110283_P01_01.jpg
    ├── Iqama_2567251562_P01_02.jpg
    ├── VehicleRegistration_003_P01_03.jpg
    └── DriversLicense_004_P01_04.jpg
```

### Filenames

The default pattern is `{type}_{identifier}_{page}_{sequence}`:

| Placeholder | Value |
| --- | --- |
| `{type}` | the document type, no spaces (`Passport`, `DriversLicense`, `VehicleRegistration`) |
| `{identifier}` | passport number / 10-digit Iqama number from OCR or a barcode; a 3-digit counter when unknown |
| `{page}` | `P01`, `P02`, … |
| `{sequence}` | `01`, `02`, … within the run |
| `{pdf}` | the source file name without extension |

`{doc_type}` = `{type}`, `{id}` = `{identifier}`, `{page_number}` = the page
without the `P` (`01`) and `{seq}` = `{sequence}`. Illegal filename characters are
stripped, and a template that cannot be filled (unknown placeholder, stray brace)
falls back to the built-in pattern `Type_identifier_P01_01` instead of failing the
export. A live example of the resulting name is shown under the field.

### Existing files

If the target folder already holds images of the same format, you are asked once:

* **Yes** - overwrite them (the new images take the same names),
* **No** - keep them and write the new images next to them as `name_2.jpg`,
  `name_3.jpg`, …,
* **Cancel** - do not export.

### What the result dialog tells you

A message box reports how many images were written and to which folder. When the
run had problems the title changes accordingly:

| Title | Meaning |
| --- | --- |
| *Export finished* | everything worked |
| *Export finished with a note* | the images were written, but a requested option (e.g. face blur) was skipped - the note lists what |
| *Export finished with errors* | some files failed; the first few error messages are shown |
| *Export cancelled* | you pressed Cancel; the images written so far are kept |

If *Open the output folder when the export finishes* is on, Explorer opens the
folder as well.

### Extraction history

Every run is recorded on the **Extraction History** page (Date, File, Documents,
Output folder, Status). Double-click a row (or use **Open output folder**) to jump
back to the images. **Refresh** rebuilds the table from the history kept in memory;
**Clear history** removes the entries (after a confirmation) without touching the
exported images. A cancelled run that wrote nothing is not recorded. The history
lives next to the settings in your user profile and keeps the last 200 runs.

---

## 9. Settings

**Settings** page (`Ctrl+4`, or **Tools → Preferences…** / `Ctrl+,`). The form is
filled from the current settings; nothing is applied until you press **Save
settings** at the bottom, which writes them to
`%LOCALAPPDATA%\ScanToCards\settings.json` and confirms with a dialog.
**Restore defaults** puts every field back to its default (the recent-files list is
kept) - press **Save settings** afterwards to keep that state.

> Leaving the page without pressing **Save settings** keeps your edits in the form
> only - the next detection or export still uses the previous values.

Most widgets wait for **Save settings**. These act immediately (they write
`settings.json` the moment you change them), because they change what the rest of
the window does right now:

* the **Add Region size** controls (fixed size, preset, width/height),
* the **Document types** tiles - tick/untick, **Keywords…**, **Add type…**,
  **Remove**,
* the **Export presets** (**Save** / **Delete**),
* the **OCR engine → Check** button (a test, nothing is stored).

### PDF rendering

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| Export / detection DPI | 300 | 150–600 | resolution used for detection and export |
| Preview DPI | 130 | 80–220 | resolution of the page preview (speed) |
| Never upscale scans beyond their native resolution | on | — | caps the DPI at the scan's own resolution |

### Card detection

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| Detection sensitivity | 5 | 1–10 | higher finds fainter edges but may add false positives |
| Minimum document area | 0.8 % | 0.1–20 % | smaller blobs are ignored |
| Maximum document area | 92 % | 30–100 % | larger blobs (the whole page) are ignored |
| Minimum confidence | 0.42 | 0–0.95 | cards below this score are dropped |
| Working resolution | 2200 px | 1200–4000 | downscale used for detection (speed vs. detail) |
| Snap detected edges onto the real image gradients | on | — | refines each quad edge onto the actual card edge |

### Add Region size

| Field | Default | Meaning |
| --- | --- | --- |
| Squeeze a drawn region to a fixed physical size | off | enables the fixed size for hand-drawn regions |
| Preset | Free (use the drawn box) | ID card 85.6 × 54, business card 90 × 55, passport page 125 × 88, Custom… |
| Width / Height | 85.6 / 53.98 mm | 10–400 mm, used for *Custom…* |

These three controls are **live** and are stored the moment you change them: they
update the toolbar drop-down immediately and do not wait for *Save settings*.

### Orientation and classification

| Field | Default | Meaning |
| --- | --- | --- |
| Rotate documents automatically when the orientation is clear | on | heuristic rotation, milliseconds per card |
| Classify documents (Passport / Iqama / Istimara / Driving licence) | on | keyword / geometry classification |
| Enable local OCR (used for classification and identifiers) | off | turns OCR on |
| Use OCR to resolve 180° orientation (slow) | off | resolves the flip the heuristic cannot decide |
| OCR engine | Automatic | Automatic / Tesseract / EasyOCR, with a **Check** button |
| OCR languages | `en` | comma separated, e.g. `en,ar` (mapped to `eng+ara` for Tesseract) |

OCR is optional - see [OCR_SETUP.md](OCR_SETUP.md) for installing an engine, and
for the table that tells you whether OCR actually ran.

### Export

| Field | Default | Meaning |
| --- | --- | --- |
| Image format | JPG | JPG / PNG / TIFF |
| JPEG quality | 95 % | 60–100 |
| Output folder | `output\` next to the application | where images are written (**Browse…**) |
| Create a separate sub-folder for each file | on | `output/<file name>/` |
| Open the output folder when the export finishes | on | Explorer opens when done |
| Filename format | `{type}_{identifier}_{page}_{sequence}` | live preview underneath, see §8 |
| Blur faces in exported images | off | privacy redaction of the portrait photo |
| Blur kernel | 51 px | 3–201, odd, larger = stronger |
| Min face size | 40 px | 20–200, measured at the 300 dpi baseline |

The line under the face-blur option always names the detector that will be used
(YuNet or Haar cascade). If no model is present a **Download model…** button
appears - it fetches the ~230 KB model once, after which the application is
offline again.

**Export presets** name the export block: pick **Preset → Save**, type a name, and
the format, quality, folder, template and face-blur values are stored. Choosing the
name from the drop-down fills those fields back in; **Delete** removes the preset.
Presets are written to disk immediately (they do not need *Save settings*), and
they never change the render DPI, because that value also drives detection.

### Document types

Every type is shown as a tile with a checkbox, its category and its keywords:

| Type | Category | Removable |
| --- | --- | --- |
| Passport, Iqama, Vehicle Registration, Driver's License, Health Insurance Card, Bank Card, Salary Card, National ID, Unknown | built-in | no (untick to hide) |
| anything you add | custom | yes (**Remove** on the tile) |

* **Unticking** hides a type from the pickers *and* from automatic classification;
  the tile greys out. Existing cards keep their type. `Unknown` is always
  available.
* **Add type…** asks for a name and (optionally) the words that appear on that
  document. The new type immediately appears in the gallery drop-down, is used for
  the filename (`MembershipCard_001_P01_01.jpg`) and - with keywords and OCR
  enabled - is assigned automatically.
* **Remove** only appears on your own types.

---

## 10. Projects and the Document Library

A **project** is a `.dceproj` file that holds the library and every detected card
with its edits. Working without saving is fine - the project simply lives in
memory - but it is lost when the application closes.

### The library panel

Under the navigation buttons:

| Element | Action |
| --- | --- |
| project name + `N files` | — |
| refresh icon | re-check whether the files still exist on disk |
| trash icon | remove the selected file(s) from the project (files stay on disk) |
| search box | filter by file name |
| file list | double-click to open in the canvas; right click for the context menu |

Each row carries an icon instead of a status word: a **document** icon for a file
that has not been processed yet, a **check** (and a `✓` after the name) once
documents were extracted from it, and a **warning** icon when the file is missing
on disk. The tooltip of a row shows the full path, the size, the date it was added
and how many regions are stored for it.

Right-clicking a row gives **Open File**, **Remove from Library** and **Refresh
Status**; for a missing file an extra **Relink File…** appears right below
**Open File**. Relinking keeps the detected cards and matches them to the new path.

### Project commands (File menu)

| Command | Shortcut | Effect |
| --- | --- | --- |
| New Project | `Ctrl+N` | start empty; unsaved changes are confirmed first |
| Open Project… | `Ctrl+Shift+O` | open a `.dceproj` file |
| Save | `Ctrl+S` | save to the file the project came from |
| Save Project As… | `Ctrl+Shift+S` | save under a new name |
| Close Project | `Ctrl+W` | close the project (confirming unsaved changes) |

New, Open and Close all return to the **Extract Documents** page, so you always
land where the work happens. Opening a project that turns out to be empty, or
that points at files which are no longer on disk, is reported with a warning
instead of failing silently.

---

## 11. Keyboard shortcuts

### Project and files

| Shortcut | Action |
| --- | --- |
| `Ctrl+N` | New Project |
| `Ctrl+Shift+O` | Open Project… |
| `Ctrl+O` | Open File… (replaces the canvas) |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save Project As… |
| `Ctrl+E` | Export All… |
| `Ctrl+W` | Close Project |
| `Ctrl+Q` | Exit |

### Pages and view

| Shortcut | Action |
| --- | --- |
| `Ctrl+1` / `Ctrl+2` / `Ctrl+3` / `Ctrl+4` | Dashboard / Extract Documents / Extraction History / Settings |
| `Ctrl+PgUp` / `Ctrl+PgDown` | previous / next page |
| `Ctrl++` / `Ctrl+-` | zoom in / out |
| `Ctrl+0` | fit page to window |
| `Ctrl` + mouse wheel | zoom the page |
| `Ctrl+Shift+A` | toggle **Add Region** |
| `Esc` | leave Add Region mode |

### Documents

| Shortcut | Action |
| --- | --- |
| `Ctrl+R` | Start Detection |
| `Ctrl+Shift+R` | Re-run Current Page |
| `Ctrl+[` / `Ctrl+]` | rotate the selected document 90° left / right |
| `Ctrl+I` | include / exclude the selected document |
| `Ctrl+Shift+C` | Edit Crop… |
| `Ctrl+Del` | Remove Document |
| `←` `→` `↑` `↓` | move the selected crop by 1 px |
| `Shift` + arrows | resize the crop (1 px per side) |
| `Ctrl` / `Alt` + arrows | same, but 10 px / 20 px per press |
| `Ctrl+Z` / `Ctrl+Y` | undo / redo inside *Adjust crop…* |

### Other

| Shortcut | Action |
| --- | --- |
| `Ctrl+,` | Preferences (Settings page) |
| `F1` | Documentation (README) |

`Ctrl+Del` rather than `Del` is deliberate: a plain `Del` would steal the key from
the gallery search box and the page range field. Accelerators come from Qt's
standard keys, so macOS shows ⌘ instead of Ctrl.

---

## 12. Tips for best results

* Scan at 200–400 dpi. Higher resolution is capped automatically and does not help.
* Leave a small gap between the cards on the platen - it makes splitting trivial.
* If a card is missed, raise the detection sensitivity or use **Add Region**; pick
  the *ID card 85.6 × 54 mm* preset so the region is exactly card sized.
* If a card is slightly tilted, select it and drag the round **rotation grip**
  above the crop in *Adjust crop…* (or type an angle in *Fine rotation*).
* If two cards were merged into one, the detector usually splits them; if not,
  remove that region and add both cards manually.
* Always check the amber *"Check orientation"* hint before exporting - a 180° flip
  cannot be detected without OCR.
* Sharing scans of personal IDs? Switch on *Blur faces in exported images* in the
  Settings: the portrait photo of every exported card is blurred, and the status
  line under the option tells you which detector will do it.
* **Save the project** before closing if you spent time correcting crops - the
  corrections live in the `.dceproj` file, not in the exported images.

---

## 13. Where to look when something is wrong

| Symptom | Where to look |
| --- | --- |
| No documents found | raise **Detection sensitivity**; check the scan resolution; use **Add Region** |
| A card is cut off | select it and use **Margin** in *Adjust crop…* |
| A card is upside down | the amber badge is the hint - press the 180° button |
| OCR seems to do nothing | [OCR_SETUP.md §6.1](OCR_SETUP.md#61-installed-is-not-the-same-as-used--how-to-confirm) |
| A file shows **Missing file** | the file was moved/renamed - use **Relink File…** in the library context menu |
| History is empty | the history page keeps the last 200 runs; **Refresh** rebuilds the table from the history held in memory |
| The window title has a `*` | there are unsaved project changes - press `Ctrl+S` |

**Help** menu: *Documentation* (`F1`) opens the README, *OCR Setup Guide* explains
the OCR installation, *Output Standards* describes the exported images,
*Diagnostics* lists paths, versions and engine availability, and *About* shows the
version.
