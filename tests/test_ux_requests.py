"""Tests for the UX requests: Save command, loading progress, library search,
extracted-file markers, export prompts, Open vs Add, project folder and title.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.main_window import APP_NAME, APP_VERSION
from core.database import DEFAULT_PROJECT_NAME, DetectedRegion, ProjectStore
from core.image_exporter import ExportOptions


def _touch_pdf(folder: Path, name: str) -> Path:
    """A file the library accepts (page count falls back to 1)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"%PDF-1.4\n% test\n")
    return path


def _menu_actions(window) -> dict:
    actions = {}
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is None:
            continue
        for item in menu.actions():
            actions[item.text()] = item
    return actions


# --------------------------------------------------------------------- #1 Save
def test_file_menu_has_save_with_ctrl_s(main_window):
    actions = _menu_actions(main_window)

    assert "Save" in actions, sorted(actions)
    assert actions["Save"].shortcut().toString() == "Ctrl+S"
    # "Save Project As…" keeps its own shortcut
    assert actions["Save Project As…"].shortcut().toString() == "Ctrl+Shift+S"


def test_save_quick_falls_back_to_save_as_for_unsaved_project(
    main_window, tmp_path, monkeypatch
):
    calls = []
    target = tmp_path / "new_project.dceproj"

    def fake_save_dialog(*args, **kwargs):
        calls.append(args[2] if len(args) > 2 else "")
        return str(target), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_save_dialog))

    assert main_window.save_project_quick() is True
    assert calls, "the Save As dialog was not opened"
    assert target.is_file()
    assert Path(main_window.settings.last_project_path) == target


def test_save_quick_writes_to_the_current_project_file(main_window, tmp_path):
    target = tmp_path / "project.dceproj"
    assert main_window.save_project(str(target)) is True
    assert Path(main_window.settings.last_project_path) == target

    # A second save must reuse the file instead of asking again.
    assert main_window.save_project_quick() is True
    assert Path(main_window.settings.last_project_path) == target


# -------------------------------------------------------------------- #8 Title
def test_window_title_hides_the_default_project_name(main_window):
    title = main_window.windowTitle()

    assert title == f"{APP_NAME} {APP_VERSION}"
    assert DEFAULT_PROJECT_NAME not in title


def test_window_title_shows_a_renamed_project(main_window, tmp_path):
    target = tmp_path / "project.dceproj"
    assert main_window.save_project(str(target)) is True
    assert main_window._library_store.rename("My scans") is True
    main_window._update_window_title()

    assert "My scans" in main_window.windowTitle()
    assert DEFAULT_PROJECT_NAME not in main_window.windowTitle()


def test_window_title_shows_a_dirty_marker(main_window, tmp_path):
    target = tmp_path / "project.dceproj"
    assert main_window.save_project(str(target)) is True
    assert not main_window.windowTitle().endswith("*")

    main_window._mark_project_dirty()
    assert main_window.windowTitle().endswith("*")

    # Saving again clears the marker.
    assert main_window.save_project(str(target)) is True
    assert not main_window.windowTitle().endswith("*")


# ------------------------------------------------------- #7 project start folder
def test_open_project_starts_in_the_last_project_folder(
    main_window, tmp_path, monkeypatch
):
    project_folder = tmp_path / "projects"
    project_folder.mkdir()
    main_window.settings.last_project_path = str(project_folder / "saved.dceproj")
    # the folder "Add Folder"/"Open File" would use
    main_window.settings.last_input_dir = str(tmp_path)

    seen = {}

    def fake_open_dialog(*args, **kwargs):
        seen["dir"] = args[2] if len(args) > 2 else ""
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_open_dialog))
    main_window.open_project_dialog()

    assert Path(seen["dir"]) == project_folder


# ------------------------------------------------- #3/#4 search + marker
def _library_with_region(tmp_path) -> ProjectStore:
    store = ProjectStore.create()
    added = store.add_paths(
        [
            str(_touch_pdf(tmp_path / "scans", "alpha_scan.pdf")),
            str(_touch_pdf(tmp_path / "scans", "beta_scan.pdf")),
            str(_touch_pdf(tmp_path / "scans", "gamma_scan.pdf")),
        ]
    )
    assert len(added) == 3
    store.save_regions(
        added[0].id,
        1,
        [
            DetectedRegion(
                id=0,
                file_id=added[0].id,
                page_num=1,
                region_index=1,
                doc_type="passport",
                x=0.1,
                y=0.1,
                width=0.3,
                height=0.2,
                rotation_deg=0,
                confidence=0.9,
                orientation_ok=True,
                is_included=True,
            )
        ],
    )
    return store


def test_region_counts_reports_files_with_extracted_regions(tmp_path):
    store = _library_with_region(tmp_path)
    try:
        counts = store.region_counts()
        items = store.items()
        extracted = [item for item in items if item.file_name == "alpha_scan.pdf"]
        assert len(extracted) == 1
        assert counts == {extracted[0].id: 1}
    finally:
        store.close()


def test_library_search_filters_by_name(main_window, tmp_path):
    store = _library_with_region(tmp_path)
    try:
        sidebar = main_window._library_sidebar
        sidebar.set_store(store)
        assert sidebar._list.count() == 3

        sidebar._search.setText("BETA")
        assert sidebar._list.count() == 1
        assert "beta_scan.pdf" in sidebar._list.item(0).text()
        assert "1 of 3" in sidebar._count_label.text()

        sidebar._search.setText("does-not-exist")
        assert sidebar._list.count() == 0
        assert "No files match" in sidebar._empty_label.text()

        sidebar._search.setText("")
        assert sidebar._list.count() == 3
    finally:
        store.close()


def test_library_marks_files_with_extracted_regions(main_window, tmp_path):
    store = _library_with_region(tmp_path)
    try:
        sidebar = main_window._library_sidebar
        sidebar.set_store(store)

        texts = [sidebar._list.item(i).text() for i in range(sidebar._list.count())]
        marked = [text for text in texts if "✓" in text]
        assert len(marked) == 1, texts
        assert "alpha_scan.pdf" in marked[0]

        # the marker is also explained in the tooltip
        marked_item = next(
            sidebar._list.item(i)
            for i in range(sidebar._list.count())
            if "✓" in sidebar._list.item(i).text()
        )
        assert "1 extracted region" in marked_item.toolTip()
    finally:
        store.close()


# ------------------------------------------------- #5 overwrite prompt
def test_export_does_not_prompt_when_the_folder_is_empty(main_window, tmp_path):
    options = ExportOptions(
        output_dir=str(tmp_path / "out"), image_format="jpg", create_subfolder=False
    )
    assert main_window._confirm_export_overwrite(options, []) is True
    assert options.overwrite_existing is False


def test_export_prompts_and_can_overwrite(main_window, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.jpg").write_bytes(b"x")
    options = ExportOptions(
        output_dir=str(out), image_format="jpg", create_subfolder=False
    )

    seen = {}

    def fake_question(*args, **kwargs):
        seen["text"] = args[2]
        return QMessageBox.StandardButton.Yes  # Overwrite

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))

    assert main_window._confirm_export_overwrite(options, []) is True
    assert options.overwrite_existing is True
    assert "already contains" in seen["text"]


def test_export_prompts_and_can_create_a_new_file(main_window, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.jpg").write_bytes(b"x")
    options = ExportOptions(
        output_dir=str(out), image_format="jpg", create_subfolder=False
    )
    options.overwrite_existing = True

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )

    assert main_window._confirm_export_overwrite(options, []) is True
    assert options.overwrite_existing is False


def test_export_prompt_cancel_aborts(main_window, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.jpg").write_bytes(b"x")
    options = ExportOptions(
        output_dir=str(out), image_format="jpg", create_subfolder=False
    )

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
    )

    assert main_window._confirm_export_overwrite(options, []) is False


# ------------------------------------------------- #9 export scope dialog
def test_export_scope_question_offers_both_targets(main_window, monkeypatch):
    seen = {}

    def fake_question(*args, **kwargs):
        seen["text"] = args[2]
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    assert main_window._ask_export_scope() == "library"

    text = seen["text"].lower()
    assert "canvas" in text
    assert "document library" in text


def test_export_scope_current_file_and_cancel(main_window, monkeypatch):
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    assert main_window._ask_export_scope() == "current"

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
    )
    assert main_window._ask_export_scope() == ""


# ------------------------------------------------- #6 Open File vs Add File
def test_open_file_replaces_the_canvas(main_window, synthetic_pdf, synthetic_pdf_multi):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    assert len(main_window._sessions) == 1

    # "Open File" on an already open document collapses back to that one
    assert main_window.load_pdf(str(synthetic_pdf_multi), add=False)
    assert len(main_window._sessions) == 1
    assert main_window.session.path == synthetic_pdf_multi


def test_add_file_only_fills_the_library(
    main_window, synthetic_pdf, synthetic_pdf_multi, monkeypatch
):
    """Add File must not touch the canvas - it only registers library rows."""
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    active_before = main_window.session.path

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(synthetic_pdf_multi)], "")),
    )
    main_window.add_pdf_dialog()

    # canvas untouched
    assert len(main_window._sessions) == 1
    assert main_window.session.path == active_before
    # library grew
    names = {item.file_name for item in main_window._library_store.items()}
    assert "multi.pdf" in names


def test_add_file_accepts_multiple_files(main_window, tmp_path, monkeypatch):
    first = _touch_pdf(tmp_path, "one.pdf")
    second = _touch_pdf(tmp_path, "two.pdf")
    third = _touch_pdf(tmp_path, "three.pdf")

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(first), str(second), str(third)], "")),
    )
    main_window.add_pdf_dialog()

    names = {item.file_name for item in main_window._library_store.items()}
    assert {"one.pdf", "two.pdf", "three.pdf"} <= names


def test_add_file_never_touches_the_canvas(main_window, tmp_path, monkeypatch):
    """Even with an empty canvas, Add File must not open anything."""
    first = _touch_pdf(tmp_path, "first.pdf")
    second = _touch_pdf(tmp_path, "second.pdf")

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(first), str(second)], "")),
    )
    main_window.add_pdf_dialog()

    assert main_window._sessions == []
    names = {item.file_name for item in main_window._library_store.items()}
    assert {"first.pdf", "second.pdf"} <= names


def test_open_file_warns_about_unsaved_changes(main_window, synthetic_pdf, monkeypatch):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window.session.dirty = True
    main_window._mark_project_dirty()

    asked = {}

    def fake_question(*args, **kwargs):
        asked["text"] = args[2]
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    assert main_window._confirm_discard_open_documents() is False
    assert "unsaved changes" in asked["text"].lower()


def test_open_file_does_not_warn_when_nothing_is_open(main_window):
    main_window._sessions = []
    main_window._session_index = -1
    assert main_window._confirm_discard_open_documents() is True


def test_open_and_add_buttons_explain_the_difference(main_window):
    open_tip = main_window._btn_open.toolTip().lower()
    add_tip = main_window._btn_add.toolTip().lower()

    assert "replace" in open_tip
    assert "library" in add_tip
    assert "canvas" in add_tip
    assert open_tip != add_tip


def test_add_folder_only_fills_the_library(main_window, tmp_path, monkeypatch):
    """Add Folder behaves like Add File: library only, canvas untouched."""
    folder = tmp_path / "batch"
    _touch_pdf(folder, "a.pdf")
    _touch_pdf(folder, "b.pdf")

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder))
    )
    main_window.add_folder_dialog()

    names = {item.file_name for item in main_window._library_store.items()}
    assert {"a.pdf", "b.pdf"} <= names
    # Add Folder is library-only as well: nothing is opened in the canvas.
    assert main_window._sessions == []


# ------------------------------------------------- #2 progress while loading
def test_load_paths_reports_progress(main_window, synthetic_pdf, synthetic_pdf_multi):
    events = []
    original_start = main_window._progress.start
    original_finish = main_window._progress.finish
    original_progress = main_window._progress.set_progress

    main_window._progress.start = lambda text="Working…": (
        events.append(("start", text)),
        original_start(text),
    )[-1]
    main_window._progress.set_progress = lambda current, total, text="": (
        events.append(("progress", current, total, text)),
        original_progress(current, total, text),
    )[-1]
    main_window._progress.finish = lambda text="Ready": (
        events.append(("finish", text)),
        original_finish(text),
    )[-1]
    try:
        main_window._sessions = []
        main_window._session_index = -1
        loaded = main_window._load_paths(
            [str(synthetic_pdf), str(synthetic_pdf_multi)], add=True, caption="Adding…"
        )
    finally:
        main_window._progress.start = original_start
        main_window._progress.set_progress = original_progress
        main_window._progress.finish = original_finish

    assert loaded == 2
    kinds = [event[0] for event in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "finish"
    assert kinds.count("progress") == 2
    # the caption names the file that is being loaded
    assert any("synthetic.pdf" in event[3] for event in events if event[0] == "progress")
    assert main_window._loading_batch is False


# ------------------------------------------------- File menu organisation
def _file_menu(window):
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is not None and menu_action.text().replace("&", "") == "File":
            return menu
    raise AssertionError("the window has no File menu")


def _shortcut_strings(action) -> set:
    """Every key sequence bound to *action* (``setShortcuts`` allows several)."""
    return {sequence.toString() for sequence in action.shortcuts()}


def test_file_menu_is_grouped_into_logical_sections(main_window):
    """Project · documents · saving · exporting · closing, separated by rules."""
    menu = _file_menu(main_window)
    entries = [
        "" if item.isSeparator() else item.text().replace("&", "")
        for item in menu.actions()
    ]

    assert entries == [
        "New Project",
        "Open Project…",
        "",
        "Open File…",
        "Add Files to Library…",
        "Add Folder…",
        "",
        "Save",
        "Save Project As…",
        "",
        "Export All…",
        "",
        "Close Project",
        "Exit",
    ]


def test_file_menu_uses_the_platform_standard_shortcuts(main_window):
    actions = _menu_actions(main_window)

    assert "Ctrl+N" in _shortcut_strings(actions["New Project"])
    assert "Ctrl+Shift+O" in _shortcut_strings(actions["Open Project…"])
    assert "Ctrl+O" in _shortcut_strings(actions["Open File…"])
    assert "Ctrl+S" in _shortcut_strings(actions["Save"])
    assert "Ctrl+Shift+S" in _shortcut_strings(actions["Save Project As…"])
    assert "Ctrl+E" in _shortcut_strings(actions["Export All…"])
    assert "Ctrl+W" in _shortcut_strings(actions["Close Project"])
    assert "Ctrl+Q" in _shortcut_strings(actions["Exit"])


def test_every_shortcut_command_has_a_real_key_binding(main_window):
    """A standard key that resolves to nothing would silently do nothing."""
    actions = _menu_actions(main_window)
    for label in (
        "New Project",
        "Open Project…",
        "Open File…",
        "Save",
        "Save Project As…",
        "Export All…",
        "Close Project",
        "Exit",
    ):
        assert _shortcut_strings(actions[label]), f"{label} has no shortcut"


def test_no_two_commands_share_a_shortcut(main_window):
    """Duplicate bindings make Qt treat the shortcut as ambiguous and drop it."""
    seen = {}
    for action in main_window.actions():
        for sequence in _shortcut_strings(action):
            seen.setdefault(sequence, []).append(action.text() or "window action")
    for menu_action in main_window.menuBar().actions():
        menu = menu_action.menu()
        if menu is None:
            continue
        for item in menu.actions():
            for sequence in _shortcut_strings(item):
                seen.setdefault(sequence, []).append(item.text())

    duplicates = {key: names for key, names in seen.items() if len(names) > 1}
    assert not duplicates, duplicates


# ------------------------------------------------------- New / Close project
def test_new_project_keeps_unsaved_work_when_cancelled(main_window, monkeypatch):
    main_window._mark_project_dirty()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
    )

    main_window.new_project()

    assert main_window._project_dirty is True


def test_new_project_starts_empty_when_confirmed(main_window, monkeypatch):
    main_window._mark_project_dirty()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )

    main_window.new_project()

    assert main_window._project_dirty is False
    assert main_window._sessions == []
    assert not main_window.windowTitle().endswith("*")


def test_closing_a_clean_project_never_asks_for_confirmation(main_window, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a clean project must not ask for confirmation")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(boom))
    main_window.request_close_project()

    assert main_window._project_dirty is False


def test_close_project_clears_the_dirty_marker(main_window):
    main_window._mark_project_dirty()
    assert main_window.windowTitle().endswith("*")

    main_window.close_project()

    assert main_window._project_dirty is False
    assert not main_window.windowTitle().endswith("*")


# --------------------------------------------------------- Menu bar commands
def _menu_by_title(window, title: str):
    """The QMenu whose title is *title* (``&`` accelerators stripped)."""
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is not None and menu_action.text().replace("&", "") == title:
            return menu
    raise AssertionError(f"the window has no {title} menu")


def test_menu_bar_has_the_five_expected_menus(main_window):
    titles = [
        action.text().replace("&", "")
        for action in main_window.menuBar().actions()
        if action.menu() is not None
    ]

    assert titles == ["File", "Edit", "View", "Tools", "Help"]


def test_every_menu_has_at_least_one_command(main_window):
    for title in ("File", "Edit", "View", "Tools", "Help"):
        entries = [
            item
            for item in _menu_by_title(main_window, title).actions()
            if not item.isSeparator()
        ]
        assert entries, f"the {title} menu is empty"


# ------------------------------------------------------ Edit menu behaviour
def _add_card(window, *, rotation: int = 0):
    """Attach one synthetic card to the open session and select it."""
    import numpy as np

    from core.models import DetectedCard, DocumentType

    card = DetectedCard(
        page_index=0,
        quad_norm=np.array(
            [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], dtype=np.float32
        ),
        confidence=0.9,
        source="manual",
        doc_type=DocumentType.PASSPORT,
        rotation=rotation,
        included=True,
    )
    window.session.add_card(card)
    window._selected_card_id = card.card_id
    window._refresh_gallery()
    window._update_state()
    return card


def test_edit_menu_is_disabled_without_a_selected_card(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._selected_card_id = ""
    main_window._update_state()

    actions = _menu_actions(main_window)
    for label in (
        "Rotate Left",
        "Rotate Right",
        "Rotate 180°",
        "Include in Export",
        "Edit Crop…",
        "Show OCR Text…",
        "Remove Document",
    ):
        assert not actions[label].isEnabled(), f"{label} should be disabled"


def test_edit_menu_rotate_right_turns_the_selected_card(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)

    rotate = _menu_actions(main_window)["Rotate Right"]
    assert rotate.isEnabled()
    rotate.trigger()

    assert card.rotation == 90


def test_edit_menu_rotate_left_turns_the_selected_card(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)

    _menu_actions(main_window)["Rotate Left"].trigger()

    assert card.rotation == 270


def test_edit_menu_include_toggles_the_selected_card(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)
    assert card.included is True

    include = _menu_actions(main_window)["Include in Export"]
    include.trigger()
    assert card.included is False

    include.trigger()
    assert card.included is True


def test_edit_menu_remove_deletes_the_selected_card(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)
    session = main_window.session

    _menu_actions(main_window)["Remove Document"].trigger()

    assert session.find_card(card.card_id) is None
    assert main_window._selected_card_id == ""


def test_edit_menu_include_checkbox_mirrors_the_selected_card(
    main_window, synthetic_pdf
):
    import numpy as np

    from core.models import DetectedCard, DocumentType

    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = DetectedCard(
        page_index=0,
        quad_norm=np.array(
            [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], dtype=np.float32
        ),
        confidence=0.9,
        source="manual",
        doc_type=DocumentType.PASSPORT,
        included=False,
    )
    main_window.session.add_card(card)
    main_window._selected_card_id = card.card_id
    main_window._update_state()

    assert _menu_actions(main_window)["Include in Export"].isChecked() is False


# ------------------------------------------------------ View menu behaviour
def test_view_menu_page_commands_are_disabled_without_a_document(main_window):
    main_window._update_state()
    actions = _menu_actions(main_window)

    for label in (
        "Previous Page",
        "Next Page",
        "Zoom In",
        "Zoom Out",
        "Fit Page to Window",
    ):
        assert not actions[label].isEnabled(), f"{label} should be disabled"


def test_view_menu_page_navigation_follows_the_page_index(
    main_window, synthetic_pdf_multi
):
    assert main_window.load_pdf(str(synthetic_pdf_multi), add=False)
    actions = _menu_actions(main_window)

    # first page: back is impossible, forward is possible
    main_window.select_page(0)
    main_window._update_state()
    assert not actions["Previous Page"].isEnabled()
    assert actions["Next Page"].isEnabled()

    # last page: the other way round
    main_window.select_page(main_window.session.page_count - 1)
    main_window._update_state()
    assert actions["Previous Page"].isEnabled()
    assert not actions["Next Page"].isEnabled()


def test_view_menu_next_page_moves_the_canvas(main_window, synthetic_pdf_multi):
    assert main_window.load_pdf(str(synthetic_pdf_multi), add=False)
    main_window.select_page(0)
    main_window._update_state()

    _menu_actions(main_window)["Next Page"].trigger()

    assert main_window._page_index == 1


def test_view_menu_greys_out_the_page_already_on_screen(main_window):
    actions = _menu_actions(main_window)

    actions["Extraction History"].trigger()
    assert main_window._pages.currentIndex() == 2
    assert not actions["Extraction History"].isEnabled()
    assert actions["Dashboard"].isEnabled()

    actions["Dashboard"].trigger()
    assert main_window._pages.currentIndex() == 0
    assert not actions["Dashboard"].isEnabled()
    assert actions["Extraction History"].isEnabled()


def test_view_menu_add_region_mirrors_the_toolbar_button(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._update_state()
    actions = _menu_actions(main_window)

    assert actions["Add Region"].isEnabled()
    actions["Add Region"].trigger()

    assert main_window._btn_add_region.isChecked() == actions["Add Region"].isChecked()


# ----------------------------------------------------- Tools menu behaviour
def test_tools_menu_detection_needs_an_open_document(main_window):
    main_window._update_state()
    actions = _menu_actions(main_window)

    assert not actions["Start Detection"].isEnabled()
    assert not actions["Re-run Current Page"].isEnabled()
    # these two never depend on the document
    assert actions["Open Output Folder"].isEnabled()
    assert actions["Check OCR Engines"].isEnabled()
    assert actions["Preferences…"].isEnabled()


def test_tools_menu_start_detection_uses_ctrl_r(main_window):
    actions = _menu_actions(main_window)

    assert actions["Start Detection"].shortcut().toString() == "Ctrl+R"
    assert actions["Re-run Current Page"].shortcut().toString() == "Ctrl+Shift+R"


def test_tools_menu_export_selected_needs_a_selected_card(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._selected_card_id = ""
    main_window._update_state()
    actions = _menu_actions(main_window)

    assert not actions["Export Selected Document…"].isEnabled()

    _add_card(main_window)
    assert actions["Export Selected Document…"].isEnabled()


def test_tools_menu_has_no_duplicate_export_all(main_window):
    """File ▸ Export All… is the single entry point for the bulk export."""
    actions = _menu_actions(main_window)

    assert "Export All…" in actions
    assert "Export Approved Documents…" not in actions




# ------------------------------------------------------ Help menu behaviour
def test_help_menu_entries_are_always_available(main_window):
    main_window._update_state()
    actions = _menu_actions(main_window)

    for label in (
        "Documentation",
        "OCR Setup Guide",
        "Output Standards",
        "Diagnostics",
        f"About {APP_NAME}",
    ):
        assert actions[label].isEnabled(), f"{label} should always be available"


def test_help_menu_documentation_uses_f1(main_window):
    assert _menu_actions(main_window)["Documentation"].shortcut().toString() == "F1"


def test_help_output_standards_reports_the_current_settings(main_window, monkeypatch):
    """The standards dialog must show the values the export really uses."""
    captured = {}

    def fake_dialog(title, heading, body, monospace=False):
        captured["title"] = title
        captured["body"] = body

    monkeypatch.setattr(main_window, "_show_text_dialog", fake_dialog)
    main_window.settings.render_dpi = 200
    main_window.settings.jpeg_quality = 88

    main_window._show_output_standards()

    assert captured["title"] == "Output Standards"
    assert "200 DPI" in captured["body"]
    assert "88" in captured["body"]


def test_help_diagnostics_reports_the_application_version(main_window, monkeypatch):
    captured = {}

    def fake_dialog(title, heading, body, monospace=False):
        captured["body"] = body

    monkeypatch.setattr(main_window, "_show_text_dialog", fake_dialog)

    main_window._show_diagnostics()

    assert APP_NAME in captured["body"]
    assert APP_VERSION in captured["body"]


def test_help_ocr_setup_explains_both_engines(main_window, monkeypatch):
    captured = {}

    def fake_dialog(title, heading, body, monospace=False):
        captured["body"] = body

    monkeypatch.setattr(main_window, "_show_text_dialog", fake_dialog)

    main_window._show_ocr_setup()

    assert "Tesseract" in captured["body"]
    assert "EasyOCR" in captured["body"]


def test_help_documentation_falls_back_when_the_readme_cannot_be_opened(
    main_window, monkeypatch
):
    """No README (or no handler): explain where it should be instead of failing."""
    from app import main_window as module

    monkeypatch.setattr(
        module.QDesktopServices, "openUrl", staticmethod(lambda url: False)
    )
    captured = {}
    monkeypatch.setattr(
        main_window,
        "_show_text_dialog",
        lambda title, heading, body, monospace=False: captured.update(body=body),
    )

    main_window._show_documentation()

    assert "README.md" in captured["body"]


# ------------------------------------------------ #3 state wiring (menus/tools)
def test_selecting_a_card_enables_the_edit_menu(main_window, synthetic_pdf):
    """A click in the gallery/canvas must enable the Edit entries right away."""
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)

    # Start from the "nothing selected" state the menu was built in.
    main_window._selected_card_id = ""
    main_window._update_state()
    assert not _menu_actions(main_window)["Rotate Right"].isEnabled()

    main_window._on_card_selected(card.card_id)

    assert main_window._selected_card_id == card.card_id
    assert _menu_actions(main_window)["Rotate Right"].isEnabled()
    assert _menu_actions(main_window)["Remove Document"].isEnabled()


def test_set_card_type_refreshes_the_include_menu_entry(main_window, synthetic_pdf):
    """Changing the type must not leave a stale "Include in Export" tick."""
    from core.models import DocumentType

    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)
    # Simulate an edit made behind the menu's back (e.g. via the crop editor).
    card.included = False

    main_window.set_card_type(card.card_id, DocumentType.IQAMA)

    assert card.doc_type is DocumentType.IQAMA
    assert _menu_actions(main_window)["Include in Export"].isChecked() is False


def test_editing_a_quad_enables_the_edit_menu(main_window, synthetic_pdf):
    """Dragging a corner selects the card, so the Edit menu has to follow."""
    import numpy as np

    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    card = _add_card(main_window)
    main_window._selected_card_id = ""
    main_window._update_state()
    assert not _menu_actions(main_window)["Edit Crop…"].isEnabled()

    quad = np.array(
        [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6], [0.2, 0.6]], dtype=np.float32
    )
    main_window._on_quad_edited(card.card_id, quad)

    assert main_window._selected_card_id == card.card_id
    assert _menu_actions(main_window)["Edit Crop…"].isEnabled()


def test_toolbar_add_region_mirrors_the_view_menu(main_window, synthetic_pdf):
    """The checkable View entry follows the toolbar button in both directions."""
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._update_state()
    action = _menu_actions(main_window)["Add Region"]

    main_window._btn_add_region.setChecked(True)
    assert action.isChecked() is True

    main_window._btn_add_region.setChecked(False)
    assert action.isChecked() is False


# ------------------------------------------------- #4 page auto-switch on open
def test_close_project_returns_to_the_extraction_canvas(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._switch_page(2)
    assert main_window._pages.currentIndex() == 2

    main_window.close_project()

    assert main_window._pages.currentIndex() == 1


def test_new_project_returns_to_the_extraction_canvas(main_window, synthetic_pdf):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._switch_page(3)
    assert main_window._pages.currentIndex() == 3

    main_window.new_project()

    assert main_window._pages.currentIndex() == 1


def test_open_project_shows_the_canvas_even_when_it_is_empty(
    main_window, tmp_path, monkeypatch
):
    """An empty project never reaches _activate_session(), so it needs its own switch."""
    from core.database import ProjectStore

    project = tmp_path / "empty.dceproj"
    store = ProjectStore.create(project, name="Empty")
    store.close()

    main_window._switch_page(0)
    assert main_window._pages.currentIndex() == 0

    assert main_window.open_project(str(project)) is True

    assert main_window._pages.currentIndex() == 1


def test_add_file_does_not_switch_pages_without_a_document(
    main_window, tmp_path, monkeypatch
):
    """Add File only fills the library - the dashboard must stay visible."""
    first = _touch_pdf(tmp_path, "one.pdf")

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(first)], "")),
    )
    main_window._switch_page(0)
    main_window.add_pdf_dialog()

    assert main_window._pages.currentIndex() == 0
    names = {item.file_name for item in main_window._library_store.items()}
    assert "one.pdf" in names


def test_add_file_returns_to_the_canvas_when_a_document_is_open(
    main_window, synthetic_pdf, synthetic_pdf_multi, monkeypatch
):
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    main_window._switch_page(0)

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(synthetic_pdf_multi)], "")),
    )
    main_window.add_pdf_dialog()

    assert main_window._pages.currentIndex() == 1
    # the canvas itself is untouched (Add File never opens anything)
    assert main_window.session.path == synthetic_pdf

