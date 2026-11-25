"""Main application window."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import zlib
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app import styles
from app.history import HistoryEntry, HistoryStore
from app.history_page import HistoryPage
from app.icons import app_icon, apply_icon_size, button_icon, icon
from app.session import DocumentSession
from app.settings import AppSettings, settings_path
from app.settings_page import REGION_PRESET_TOKENS, REGION_PRESETS, SettingsPage
from core.database import (
    DEFAULT_PROJECT_NAME,
    PROJECT_SUFFIX,
    STATUS_EXTRACTED,
    DetectedRegion,
    ProjectError,
    ProjectStore,
)
from core.image_exporter import ExportOptions, ExportSummary
from core.models import DetectedCard, DocumentType
from core.ocr_engine import OcrEngine
from core.pdf_loader import (
    PdfDocument,
    PdfError,
    is_supported_file,
    supported_file_filter,
)
from core.perspective_corrector import make_thumbnail, rotate_image, warp_quad
from widgets.crop_editor import CropEditorDialog
from widgets.document_gallery import DocumentGallery, GalleryFilter
from widgets.library_sidebar_widget import LibrarySidebarWidget
from widgets.pdf_viewer import OverlaySpec, PdfPageView
from widgets.progress_widget import ProgressPanel
from widgets.qt_image import bgr_to_qimage
from widgets.region_size_dialog import RegionSizeDialog
from workers.detection_worker import DetectionWorker
from workers.export_worker import ExportJob, ExportWorker

APP_NAME = "ScanToCards"
APP_SUBTITLE = "Document Card Extractor"
APP_VERSION = "1.0.1"


def standard_shortcuts(
    key: QKeySequence.StandardKey, fallback: str = ""
) -> list[QKeySequence]:
    """Resolve a platform standard key to concrete key sequences.

    ``QKeySequence.StandardKey`` values are theme dependent: ``Quit`` has no
    binding at all on Windows and ``Close``/``Print`` differ per platform.  An
    empty key sequence would silently do nothing, so *fallback* (a portable
    string such as ``"Ctrl+Q"``) is used when the platform offers none.
    """
    shortcuts = QKeySequence.keyBindings(key)
    if not shortcuts and fallback:
        shortcuts = [QKeySequence(fallback)]
    return shortcuts


TYPE_COLORS = {
    # Keyed by token (not by type object) so the mapping survives reloads of
    # the registry and stays stable while custom types come and go.
    "passport": "#2563eb",
    "iqama": "#0d9488",
    "istimara": "#f59e0b",
    "driving_license": "#7c3aed",
    "health_insurance": "#0284c7",
    "bank_card": "#be123c",
    "salary_card": "#15803d",
    "national_id": "#9333ea",
    "unknown": "#64748b",
}


class MainWindow(QMainWindow):
    """Everything the user interacts with."""

    #: Widgets created by SettingsPage and mirrored onto this window via
    #: ``PASSTHROUGH_HANDLES``. Declared here so type checkers see them as
    #: known attributes even though they are assigned with ``setattr``.
    _set_region_preset: QComboBox | None = None
    _set_region_fixed: QCheckBox | None = None
    _set_region_width: QDoubleSpinBox | None = None
    _set_region_height: QDoubleSpinBox | None = None

    def __init__(self, settings: AppSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or AppSettings.load()
        self.settings.apply_document_types()
        self.history = HistoryStore()
        # Set when the last project could not be restored (reported after the UI
        # exists, so the user never just sees an empty library).
        self._startup_project_error = ""
        # The document library is stored in SQLite.  Always start with a fresh
        # empty in-memory project; the user can open or create a project explicitly.
        self._library_store: ProjectStore | None = ProjectStore.create()

        self._sessions: list[DocumentSession] = []
        self._session_index = -1
        self._page_index = 0
        self._page_image: np.ndarray | None = None
        self._page_dpi: float = 0.0
        self._region_pending_dialog: int = -1
        self._preview_document: PdfDocument | None = None
        # A freshly probed document not yet attached to the preview (avoids
        # opening/decoding the same file twice in load_pdf).
        self._pending_preview_document: PdfDocument | None = None
        self._selected_card_id = ""
        self._detection_worker: DetectionWorker | None = None
        self._export_worker: ExportWorker | None = None
        self._detection_errors: list[str] = []
        self._export_errors: list[str] = []
        self._active_export_jobs: list[ExportJob] = []
        self._busy = False
        # Batch loading (Add Folder / drop / project open) drives the progress
        # panel itself; ``_loading_batch`` keeps it from re-entering and
        # ``_batch_cancel`` lets the Cancel button stop the loop between files.
        self._loading_batch = False
        self._batch_cancel = False
        # True when the project database holds changes that are not in the
        # saved .dceproj file yet (drives the "*" in the window title).
        self._project_dirty = False
        # Thumbnail rebuilds are a full perspective warp; arrow-key nudges and
        # corner drags trigger them continuously, so they are coalesced.
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(30)
        self._thumb_timer.timeout.connect(self._flush_thumbnails)
        self._thumb_pending: DetectedCard | None = None
        # The gallery is rebuilt when a page finishes; a 20 page run would
        # rebuild it 20 times, so those refreshes are coalesced as well.
        self._gallery_timer = QTimer(self)
        self._gallery_timer.setSingleShot(True)
        self._gallery_timer.setInterval(120)
        self._gallery_timer.timeout.connect(self._refresh_gallery)


        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.resize(1440, 900)
        self.setMinimumSize(1100, 720)

        self._build_ui()
        self._create_shortcuts()
        # a slightly larger icon keeps the 1-px strokes of the SVG icons visible
        apply_icon_size(self)
        self._update_state()
        self._update_window_title()

    def _update_window_title(self) -> None:
        """Update the window title, showing the project name when one is open.

        The default in-memory project is named ``"Document Library"``; appending it to
        the title produces ``"ScanToCards 1.0.0 - Document Library"``, which
        is noise.  Only show a name when the project was actually saved and renamed.
        """
        store = self._library_store
        name = store.name if store is not None else ""
        if (
            store is not None
            and store.project_file is not None
            and name
            and name != DEFAULT_PROJECT_NAME
        ):
            title = f"{APP_NAME} {APP_VERSION} - {name}"
        else:
            title = f"{APP_NAME} {APP_VERSION}"
        # A trailing "*" is the usual "there are changes not in the .dceproj
        # file yet" marker.
        if self._project_dirty:
            title += " *"
        self.setWindowTitle(title)

    def _mark_project_dirty(self) -> None:
        """Remember that the project differs from the last saved ``.dceproj``.

        Card edits are written to the project database immediately, but the
        database only reaches the disk as a ``.dceproj`` file on Save - so the
        window title needs to say that the file is out of date.
        """
        if not self._project_dirty:
            self._project_dirty = True
            self._update_window_title()

    def _clear_project_dirty(self) -> None:
        if self._project_dirty:
            self._project_dirty = False
            self._update_window_title()

    # ------------------------------------------------------------------ session
    @property
    def session(self) -> DocumentSession | None:
        if 0 <= self._session_index < len(self._sessions):
            return self._sessions[self._session_index]
        return None

    # --------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_dashboard_page())
        self._pages.addWidget(self._build_extract_page())
        self._pages.addWidget(self._build_history_page())
        self._pages.addWidget(self._build_settings_page())
        layout.addWidget(self._pages, 1)
        self.setCentralWidget(central)

        self._build_status_bar()
        self._build_menu()
        self._load_settings_into_form()
        # The dashboard is the start page, so its counters and the recent file
        # list must be filled right away (not only when the nav button is used).
        self._refresh_dashboard()
        self._warn_about_startup_project()

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(236)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(icon("extract", styles.PRIMARY).pixmap(28, 28))
        brand_row.addWidget(brand_icon)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("BrandTitle")
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("BrandSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(18)

        self._nav_buttons: list[QPushButton] = []
        for index, (label, icon_name) in enumerate(
            (
                ("Dashboard", "dashboard"),
                ("Extract Documents", "extract"),
                ("Extraction History", "history"),
                ("Settings", "settings"),
            )
        ):
            button = QPushButton(icon(icon_name, styles.TEXT_MUTED), f"  {label}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(lambda _checked=False, i=index: self._switch_page(i))
            layout.addWidget(button)
            self._nav_buttons.append(button)
        self._nav_buttons[0].setChecked(True)

        layout.addSpacing(16)

        # Document Library panel (integrated into sidebar)
        self._library_sidebar = LibrarySidebarWidget(self._library_store, self)
        self._library_sidebar.fileActivated.connect(self._open_library_file)
        self._library_sidebar.relinkRequested.connect(self._relink_file)
        self._library_sidebar.message.connect(self._set_status_message)
        layout.addWidget(self._library_sidebar, 1)

        layout.addSpacing(8)

        self._ocr_status_label = QLabel(self._ocr_status_text())
        self._ocr_status_label.setObjectName("Faint")
        self._ocr_status_label.setWordWrap(True)
        layout.addWidget(self._ocr_status_label)
        version = QLabel(f"Version {APP_VERSION} · offline")
        version.setObjectName("Faint")
        layout.addWidget(version)
        return sidebar

    @staticmethod
    def _ocr_status_text() -> str:
        engine = OcrEngine.describe_availability()
        return f"OCR engine: {engine or 'not installed'}"

    def _update_ocr_status_label(self) -> None:
        """Refresh the sidebar line (it was computed once at startup)."""
        label = getattr(self, "_ocr_status_label", None)
        if label is not None:
            label.setText(self._ocr_status_text())


    def _build_status_bar(self) -> None:
        status = self.statusBar()
        status.setSizeGripEnabled(True)
        self._status_pages = QLabel("Pages: 0")
        self._status_detected = QLabel("Detected: 0")
        self._status_approved = QLabel("Approved: 0")
        for label in (self._status_pages, self._status_detected, self._status_approved):
            label.setContentsMargins(8, 0, 8, 0)
            status.addWidget(label)

        self._progress = ProgressPanel()
        self._progress.cancelRequested.connect(self._cancel_current_task)
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 8, 0)
        row.addWidget(self._progress)
        status.addPermanentWidget(container)

    # ------------------------------------------------------- document library
    def _open_startup_project(self) -> ProjectStore | None:
        """Open the last saved project, or start a new unsaved one.

        The default project lives in memory, so the library only survives a
        restart once it has been saved to a ``.dceproj`` file.  A project that
        cannot be restored is *reported* (``_startup_project_error``) instead of
        silently degrading to an empty library.
        """
        last = str(getattr(self.settings, "last_project_path", "") or "")
        if last and not Path(last).is_file():
            self._startup_project_error = (
                f"The last project could not be found:\n{last}\n\n"
                "A new, empty project was started instead."
            )
            self.settings.last_project_path = ""
        elif last:
            try:
                return ProjectStore.open(last)
            except ProjectError as exc:
                self._startup_project_error = (
                    f"The last project could not be opened:\n{last}\n\n{exc}\n\n"
                    "A new, empty project was started instead."
                )
                self.settings.last_project_path = ""
        try:
            return ProjectStore.create()
        except ProjectError:
            return None

    def _warn_about_startup_project(self) -> None:
        """Explain a failed restore / a restored project with unusable files."""
        message = getattr(self, "_startup_project_error", "")
        if message:
            QMessageBox.warning(self, "Project not restored", message)
            return
        store = self._library_store
        if store is None or store.project_file is None:
            return  # a brand new unsaved project has nothing to report
        self._refresh_library_statuses()
        self._report_project_contents(store.project_file.name, warn_empty=True)

    def _refresh_library_statuses(self) -> None:
        """Re-check the files on disk so restored projects show true statuses."""
        store = self._library_store
        if store is None:
            return
        try:
            if store.refresh_statuses():
                self._library_sidebar.refresh()
        except ProjectError:
            pass

    def _report_project_contents(self, label: str, warn_empty: bool = True) -> None:
        """Warn when a project has no entries or points at missing files."""
        store = self._library_store
        if store is None:
            return
        try:
            items = store.items()
        except ProjectError as exc:
            QMessageBox.warning(self, "Project could not be read", str(exc))
            return
        if not items:
            if warn_empty:
                QMessageBox.warning(
                    self,
                    "Empty project",
                    f"“{label}” contains no documents.\n\n"
                    "Add files with the Document Library panel, then save the "
                    "project again.",
                )
            return
        missing = [item.file_path for item in items if not item.exists]
        if not missing:
            return
        preview = "\n".join(f"• {path}" for path in missing[:6])
        extra = f"\n…and {len(missing) - 6} more." if len(missing) > 6 else ""
        QMessageBox.warning(
            self,
            "Files not found",
            f"{len(missing)} of {len(items)} file(s) in “{label}” are missing on disk:\n\n"
            f"{preview}{extra}\n\n"
            "Restore or reconnect them, then press Refresh in the Document Library panel.",
        )

    def _build_menu(self) -> None:
        """Build the menu bar.

        The File menu is grouped the way a document application is usually
        laid out - project, documents, saving, exporting, closing - and every
        command that has a platform convention uses ``QKeySequence.StandardKey``
        so the accelerators follow the operating system (Ctrl on Windows/Linux,
        Cmd on macOS) instead of being hard coded.
        """
        menu_bar = self.menuBar()
        # Every action that depends on the application state is registered here
        # so ``_update_menu_state`` can grey it out without touching the menus.
        self._menu_actions: dict[str, QAction] = {}

        file_menu = menu_bar.addMenu("&File")

        # -- project group ------------------------------------------------
        new_project = file_menu.addAction("New Project")
        new_project.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.New, "Ctrl+N")
        )
        new_project.setToolTip(
            "Start an empty project.\n"
            "The current project is closed; unsaved changes are confirmed first."
        )
        new_project.setStatusTip("Start a new, empty project")
        new_project.triggered.connect(self.new_project)

        open_project = file_menu.addAction("Open Project…")
        open_project.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_project.setStatusTip("Open a saved .dceproj project")
        open_project.triggered.connect(self.open_project_dialog)

        file_menu.addSeparator()

        # -- documents group ----------------------------------------------
        open_file = file_menu.addAction("Open File…")
        open_file.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.Open, "Ctrl+O")
        )
        open_file.setToolTip(
            "Open a single file in the canvas for quick extraction.\n"
            "It replaces the documents currently open (they stay in the library)."
        )
        open_file.setStatusTip("Open a single file, replacing the documents currently open")
        open_file.triggered.connect(self.open_pdf_dialog)

        add_file = file_menu.addAction("Add Files to Library…")
        add_file.setToolTip(
            "Add one or more files to the Document Library.\n"
            "Nothing is opened in the canvas - double-click a library row to open it."
        )
        add_file.setStatusTip(
            "Add one or more files to the Document Library without opening them"
        )
        add_file.triggered.connect(self.add_pdf_dialog)

        add_folder = file_menu.addAction("Add Folder…")
        add_folder.setStatusTip("Add every supported file of a folder (recursively)")
        add_folder.triggered.connect(self.add_folder_dialog)

        file_menu.addSeparator()

        # -- saving group --------------------------------------------------
        save_project = file_menu.addAction("Save")
        save_project.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.Save, "Ctrl+S")
        )
        save_project.setStatusTip("Save the project to the file it was opened from")
        save_project.triggered.connect(self.save_project_quick)

        save_project_as = file_menu.addAction("Save Project As…")
        save_project_as.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.SaveAs, "Ctrl+Shift+S")
        )
        save_project_as.setStatusTip("Save the project under a new name")
        save_project_as.triggered.connect(self.save_project_as)

        file_menu.addSeparator()

        # -- exporting group -----------------------------------------------
        export_all = file_menu.addAction("Export All…")
        export_all.setShortcut(QKeySequence("Ctrl+E"))
        export_all.setStatusTip(
            "Export the approved documents of the current file or of the whole library"
        )
        export_all.triggered.connect(self.export_all)

        file_menu.addSeparator()

        # -- closing group --------------------------------------------------
        close_project = file_menu.addAction("Close Project")
        close_project.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.Close, "Ctrl+W")
        )
        close_project.setStatusTip(
            "Close the project and start with an empty canvas (unsaved changes are confirmed)"
        )
        close_project.triggered.connect(self.request_close_project)

        exit_action = file_menu.addAction("Exit")
        # ``StandardKey.Quit`` is Cmd+Q on macOS but has no binding on Windows.
        exit_action.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.Quit, "Ctrl+Q")
        )
        exit_action.setStatusTip(f"Quit {APP_NAME}")
        exit_action.triggered.connect(self.close)

        # -- state driven entries -------------------------------------------
        self._menu_actions.update(
            {
                "new_project": new_project,
                "open_project": open_project,
                "open_file": open_file,
                "add_files": add_file,
                "add_folder": add_folder,
                "save_project": save_project,
                "save_project_as": save_project_as,
                "export_all": export_all,
                "close_project": close_project,
                "exit": exit_action,
            }
        )

        self._build_edit_menu(menu_bar)
        self._build_view_menu(menu_bar)
        self._build_tools_menu(menu_bar)
        self._build_help_menu(menu_bar)

    # ---------------------------------------------------------- view menu
    def _build_view_menu(self, menu_bar) -> None:
        """Navigation and zoom - they need an open document to do anything."""
        view_menu = menu_bar.addMenu("&View")

        dashboard = view_menu.addAction("Dashboard")
        dashboard.setShortcut(QKeySequence("Ctrl+1"))
        dashboard.setStatusTip("Show the dashboard")
        dashboard.triggered.connect(lambda: self._switch_page(0))

        extract = view_menu.addAction("Extract Documents")
        extract.setShortcut(QKeySequence("Ctrl+2"))
        extract.setStatusTip("Show the extraction canvas")
        extract.triggered.connect(lambda: self._switch_page(1))

        history = view_menu.addAction("Extraction History")
        history.setShortcut(QKeySequence("Ctrl+3"))
        history.setStatusTip("Show the extraction history")
        history.triggered.connect(lambda: self._switch_page(2))

        settings = view_menu.addAction("Settings")
        settings.setShortcut(QKeySequence("Ctrl+4"))
        settings.setStatusTip("Show the settings page")
        settings.triggered.connect(lambda: self._switch_page(3))

        view_menu.addSeparator()

        prev_page = view_menu.addAction("Previous Page")
        prev_page.setShortcut(QKeySequence("Ctrl+PgUp"))
        prev_page.setStatusTip("Show the previous page of the open document")
        prev_page.triggered.connect(lambda: self.select_page(self._page_index - 1))

        next_page = view_menu.addAction("Next Page")
        next_page.setShortcut(QKeySequence("Ctrl+PgDown"))
        next_page.setStatusTip("Show the next page of the open document")
        next_page.triggered.connect(lambda: self.select_page(self._page_index + 1))

        view_menu.addSeparator()

        zoom_in = view_menu.addAction("Zoom In")
        zoom_in.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.ZoomIn, "Ctrl++")
        )
        zoom_in.setStatusTip("Enlarge the page preview")
        zoom_in.triggered.connect(lambda: self._view.zoom_in())

        zoom_out = view_menu.addAction("Zoom Out")
        zoom_out.setShortcuts(
            standard_shortcuts(QKeySequence.StandardKey.ZoomOut, "Ctrl+-")
        )
        zoom_out.setStatusTip("Shrink the page preview")
        zoom_out.triggered.connect(lambda: self._view.zoom_out())

        fit = view_menu.addAction("Fit Page to Window")
        fit.setShortcut(QKeySequence("Ctrl+0"))
        fit.setStatusTip("Fit the whole page inside the canvas")
        fit.triggered.connect(lambda: self._view.fit_to_window())

        view_menu.addSeparator()

        add_region = view_menu.addAction("Add Region")
        add_region.setCheckable(True)
        add_region.setShortcut(QKeySequence("Ctrl+Shift+A"))
        add_region.setStatusTip(
            "Draw a rectangle to add a document the detector missed (Esc to cancel)"
        )
        add_region.triggered.connect(self._toggle_add_mode_from_menu)

        self._menu_actions.update(
            {
                "view_dashboard": dashboard,
                "view_extract": extract,
                "view_history": history,
                "view_settings": settings,
                "prev_page": prev_page,
                "next_page": next_page,
                "zoom_in": zoom_in,
                "zoom_out": zoom_out,
                "fit_page": fit,
                "add_region": add_region,
            }
        )

    def _build_edit_menu(self, menu_bar) -> None:
        """Card editing commands - they only apply to a selected document."""
        edit_menu = menu_bar.addMenu("&Edit")

        rotate_left = edit_menu.addAction("Rotate Left")
        rotate_left.setShortcut(QKeySequence("Ctrl+["))
        rotate_left.setStatusTip("Rotate the selected document 90° counter-clockwise")
        rotate_left.triggered.connect(lambda: self._rotate_selected(-90))

        rotate_right = edit_menu.addAction("Rotate Right")
        rotate_right.setShortcut(QKeySequence("Ctrl+]"))
        rotate_right.setStatusTip("Rotate the selected document 90° clockwise")
        rotate_right.triggered.connect(lambda: self._rotate_selected(90))

        rotate_180 = edit_menu.addAction("Rotate 180°")
        rotate_180.setStatusTip("Turn the selected document upside down")
        rotate_180.triggered.connect(lambda: self._rotate_selected(180))

        edit_menu.addSeparator()

        toggle_include = edit_menu.addAction("Include in Export")
        toggle_include.setCheckable(True)
        toggle_include.setShortcut(QKeySequence("Ctrl+I"))
        toggle_include.setStatusTip(
            "Include or exclude the selected document from the export"
        )
        toggle_include.triggered.connect(self._toggle_selected_included)

        edit_crop_action = edit_menu.addAction("Edit Crop…")
        edit_crop_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        edit_crop_action.setStatusTip("Open the crop editor for the selected document")
        edit_crop_action.triggered.connect(self._edit_selected_crop)

        ocr_action = edit_menu.addAction("Show OCR Text…")
        ocr_action.setStatusTip("Show the text recognised for the selected document")
        ocr_action.triggered.connect(self._show_selected_ocr_text)

        edit_menu.addSeparator()

        remove_action = edit_menu.addAction("Remove Document")
        # ``Delete`` alone would steal the key from the search / page-range line
        # edits, so the menu uses the Ctrl+Delete combination instead.
        remove_action.setShortcut(QKeySequence("Ctrl+Del"))
        remove_action.setStatusTip("Remove the selected document from the page")
        remove_action.triggered.connect(self._delete_selected_card)

        self._menu_actions.update(
            {
                "rotate_left": rotate_left,
                "rotate_right": rotate_right,
                "rotate_180": rotate_180,
                "toggle_include": toggle_include,
                "edit_crop": edit_crop_action,
                "show_ocr": ocr_action,
                "remove_card": remove_action,
            }
        )

    # --------------------------------------------------------- tools menu
    def _build_tools_menu(self, menu_bar) -> None:
        """Detection, export and settings shortcuts."""
        tools_menu = menu_bar.addMenu("&Tools")

        detect = tools_menu.addAction("Start Detection")
        detect.setShortcut(QKeySequence("Ctrl+R"))
        detect.setStatusTip("Detect documents on the pages of the open file")
        detect.triggered.connect(lambda: self.start_detection())

        redetect = tools_menu.addAction("Re-run Current Page")
        redetect.setShortcut(QKeySequence("Ctrl+Shift+R"))
        redetect.setStatusTip("Run the detector again on the page on screen")
        redetect.triggered.connect(lambda: self.start_detection([self._page_index]))

        tools_menu.addSeparator()

        export_selected = tools_menu.addAction("Export Selected Document…")
        export_selected.setStatusTip("Export only the document selected in the gallery")
        export_selected.triggered.connect(self._export_selected_card)

        open_output = tools_menu.addAction("Open Output Folder")
        open_output.setStatusTip("Open the export folder in the file manager")
        open_output.triggered.connect(
            lambda: self._open_folder(self.settings.output_dir)
        )

        tools_menu.addSeparator()

        settings_action = tools_menu.addAction("Preferences…")
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.setStatusTip("Open the settings page")
        settings_action.triggered.connect(lambda: self._switch_page(3))

        check_ocr = tools_menu.addAction("Check OCR Engines")
        check_ocr.setStatusTip("Report which OCR engines are installed")
        check_ocr.triggered.connect(self._check_ocr_engines)

        self._menu_actions.update(
            {
                "detect": detect,
                "redetect": redetect,
                "export_selected": export_selected,
                "open_output": open_output,
                "preferences": settings_action,
                "check_ocr": check_ocr,
            }
        )

    # ---------------------------------------------------------- help menu
    def _build_help_menu(self, menu_bar) -> None:
        """Help entries - always available, they never depend on the state."""
        help_menu = menu_bar.addMenu("&Help")

        documentation = help_menu.addAction("Documentation")
        documentation.setShortcut(QKeySequence("F1"))
        documentation.setStatusTip("Open the README with the usage instructions")
        documentation.triggered.connect(self._show_documentation)

        ocr_setup = help_menu.addAction("OCR Setup Guide")
        ocr_setup.setStatusTip("How to install an OCR engine for text recognition")
        ocr_setup.triggered.connect(self._show_ocr_setup)

        help_menu.addSeparator()

        standards = help_menu.addAction("Output Standards")
        standards.setStatusTip("Resolution, format and naming of the exported images")
        standards.triggered.connect(self._show_output_standards)

        diagnostics = help_menu.addAction("Diagnostics")
        diagnostics.setStatusTip("Paths, versions and engine availability")
        diagnostics.triggered.connect(self._show_diagnostics)

        help_menu.addSeparator()

        about = help_menu.addAction(f"About {APP_NAME}")
        about.setStatusTip("Version and licence information")
        about.triggered.connect(self._show_about)

        self._menu_actions.update(
            {
                "documentation": documentation,
                "ocr_setup": ocr_setup,
                "standards": standards,
                "diagnostics": diagnostics,
                "about": about,
            }
        )



    # ------------------------------------------------- menu state & helpers
    def _update_menu_state(self) -> None:
        """Enable only the menu entries that apply to the current state.

        Menu items are greyed out (``QAction.setEnabled(False)``) instead of
        being hidden: a menu that keeps its shape is easier to learn, and the
        disabled entry is the hint that the command exists but needs another
        context (an open document, a selected card, a finished detection run).
        """
        actions = getattr(self, "_menu_actions", {})
        if not actions:
            return
        session = self.session
        has_session = session is not None
        has_pages = has_session and session.page_count > 0
        card = self._selected_card() if has_session else None
        has_card = card is not None
        busy = self._busy
        counts = session.counts() if has_session else {"detected": 0, "approved": 0}
        page = self._pages.currentIndex() if hasattr(self, "_pages") else 0

        def set_enabled(key: str, enabled: bool) -> None:
            action = actions.get(key)
            if action is not None:
                action.setEnabled(bool(enabled))

        # -- File -----------------------------------------------------------
        # Opening/adding is always possible, saving and closing need a project.
        set_enabled("new_project", not busy)
        set_enabled("open_project", not busy)
        set_enabled("open_file", not busy)
        set_enabled("add_files", not busy)
        set_enabled("add_folder", not busy)
        set_enabled("save_project", not busy)
        set_enabled("save_project_as", not busy)
        set_enabled("close_project", not busy)
        set_enabled("export_all", has_session and counts["approved"] > 0 and not busy)

        # -- Edit -----------------------------------------------------------
        for key in (
            "rotate_left",
            "rotate_right",
            "rotate_180",
            "toggle_include",
            "edit_crop",
            "show_ocr",
            "remove_card",
        ):
            set_enabled(key, has_card and not busy)
        toggle = actions.get("toggle_include")
        if toggle is not None:
            toggle.blockSignals(True)
            toggle.setChecked(bool(card.included) if card is not None else False)
            toggle.blockSignals(False)

        # -- View -----------------------------------------------------------
        # Page navigation and zoom only make sense with a rendered document.
        set_enabled("prev_page", has_pages and self._page_index > 0 and not busy)
        set_enabled(
            "next_page",
            session is not None and has_pages and self._page_index < session.page_count - 1 and not busy,
        )
        set_enabled("zoom_in", has_pages)
        set_enabled("zoom_out", has_pages)
        set_enabled("fit_page", has_pages)
        set_enabled("add_region", has_session and not busy)
        add_region = actions.get("add_region")
        if add_region is not None:
            button = getattr(self, "_btn_add_region", None)
            checked = bool(button is not None and button.isChecked())
            add_region.blockSignals(True)
            add_region.setChecked(checked)
            add_region.blockSignals(False)

        # -- Tools ----------------------------------------------------------
        set_enabled("detect", has_session and not busy)
        set_enabled("redetect", has_pages and not busy)
        set_enabled("export_selected", has_card and not busy)
        set_enabled("open_output", not busy)
        set_enabled("check_ocr", not busy)

        # -- Help -----------------------------------------------------------
        # Always available, but the page-specific entries stay reachable too.
        for key in ("documentation", "ocr_setup", "standards", "diagnostics", "about"):
            set_enabled(key, True)

        # A page that is already on screen cannot be navigated to again, so its
        # View entry is greyed out - the menu doubles as a page indicator.
        set_enabled("view_dashboard", page != 0)
        set_enabled("view_extract", page != 1)
        set_enabled("view_history", page != 2)
        set_enabled("view_settings", page != 3)

    # ------------------------------------------------------ menu slot helpers
    def _toggle_selected_included(self) -> None:
        """Edit ▸ Include in Export for the selected card."""
        if self._selected_card_id:
            self.toggle_card_included(self._selected_card_id)

    def _edit_selected_crop(self) -> None:
        """Edit ▸ Edit Crop… for the selected card."""
        if self._selected_card_id:
            self.edit_crop(self._selected_card_id)

    def _show_selected_ocr_text(self) -> None:
        """Edit ▸ Show OCR Text… for the selected card."""
        if self._selected_card_id:
            self.show_ocr_text(self._selected_card_id)

    def _toggle_add_mode_from_menu(self, checked: bool) -> None:
        """View ▸ Add Region mirrors the toolbar toggle button."""
        button = getattr(self, "_btn_add_region", None)
        if button is None:
            return
        if button.isChecked() != bool(checked):
            button.setChecked(bool(checked))

    def _export_selected_card(self) -> None:
        """Tools ▸ Export Selected Document… for the selected card."""
        if self._selected_card_id:
            self.export_card(self._selected_card_id)

    # ------------------------------------------------------- help dialogs
    def _show_text_dialog(
        self, title: str, heading: str, body: str, monospace: bool = False
    ) -> None:
        """Read-only dialog used by the Help menu entries.

        ``QMessageBox`` truncates long reports, so the diagnostics and the
        standards are shown in a scrollable text view with a Copy button.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(620, 460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        title_label = QLabel(heading)
        title_label.setObjectName("PageTitle")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        viewer = QPlainTextEdit(body)
        viewer.setReadOnly(True)
        if monospace:
            font = viewer.font()
            font.setFamily("Consolas")
            viewer.setFont(font)
        layout.addWidget(viewer, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_button = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.clicked.connect(dialog.reject)
        buttons.rejected.connect(dialog.reject)
        if copy_button is not None:
            copy_button.clicked.connect(lambda: QApplication.clipboard().setText(body))
        layout.addWidget(buttons)
        dialog.exec()

    def _show_documentation(self) -> None:
        """Help ▸ Documentation - open the README, or explain where it is."""
        readme = Path(__file__).resolve().parents[1] / "README.md"
        if readme.is_file() and QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(readme))
        ):
            self._set_status_message(f"Opened {readme.name}")
            return
        self._show_text_dialog(
            "Documentation",
            "The README could not be opened",
            "Documentation is shipped as README.md next to the application.\n\n"
            f"Expected location:\n    {readme}\n\n"
            "Open that file with any text editor, or reinstall the application "
            "if the file is missing.",
        )

    def _show_ocr_setup(self) -> None:
        """Help ▸ OCR Setup Guide - how to install an OCR engine."""
        available = OcrEngine.describe_availability()
        if available:
            state = f"An OCR engine is available on this computer: {available}."
        else:
            state = "No OCR engine is available on this computer."
        body = (
            f"{state}\n\n"
            "The application works without OCR - documents are still detected and "
            "exported.  OCR only adds the recognised text, the automatic document "
            "type and the identifier in the file name.\n\n"
            "Tesseract (recommended: small, fast, no model download)\n"
            f"{OcrEngine.tesseract_setup_hint()}\n\n"
            "EasyOCR (handwritten text, slower, needs model files)\n"
            f"{OcrEngine.easyocr_setup_hint()}\n\n"
            "After installing an engine, open Settings and press “Check” next to "
            "the OCR engine to verify it, then run the detection again."
        )
        self._show_text_dialog("OCR Setup Guide", "Installing an OCR engine", body)

    def _show_output_standards(self) -> None:
        """Help ▸ Output Standards - what the exported images look like."""
        settings = self.settings
        body = (
            "Exported files\n"
            "--------------\n"
            f"Format              : {settings.export_format.upper()}\n"
            f"Resolution          : {settings.render_dpi} DPI\n"
            f"JPEG quality        : {settings.jpeg_quality}\n"
            f"Output folder       : {settings.output_dir}\n"
            "Open after export   : "
            f"{'yes' if settings.open_output_after_export else 'no'}\n"
            "\n"
            "What is exported\n"
            "----------------\n"
            "Only documents marked “Include in export” are written.  Each document\n"
            "is cropped to its four corners and perspective-corrected, so a photo\n"
            "taken at an angle becomes a straight rectangle.\n"
            "\n"
            "File names\n"
            "----------\n"
            "<original file name>_p<page>_<type>_<identifier>.<ext>\n"
            "The identifier is the number the OCR engine found on the document;\n"
            "without OCR the type and identifier are omitted.\n"
            "\n"
            "Changing the values\n"
            "-------------------\n"
            "Settings ▸ Output: format, JPEG quality, resolution and folder."
        )
        self._show_text_dialog(
            "Output Standards", "How documents are exported", body, monospace=True
        )

    def _show_about(self) -> None:
        """Help ▸ About - name, version and a short description."""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            f"<p><b>{APP_SUBTITLE}</b><br>Version {APP_VERSION}</p>"
            "<p>Detects identity documents (passports, ID cards, licences) on "
            "scanned PDFs and photos, straightens them and exports them as "
            "individual images.</p>"
            "<p>Everything runs offline on this computer - no file ever leaves "
            "the machine.</p>",
        )

    def _show_diagnostics(self) -> None:
        """Help ▸ Diagnostics - paths, versions and engine availability."""
        session = self.session
        store = self._library_store
        project_file = (
            store.project_file if store is not None and store.project_file else ""
        )
        counts = session.counts() if session is not None else {"detected": 0, "approved": 0}
        lines = [
            f"{APP_NAME} {APP_VERSION}",
            f"Python              : {sys.version.split()[0]}",
            f"Platform            : {sys.platform}",
            f"Project root        : {Path(__file__).resolve().parents[1]}",
            f"Settings file       : {settings_path()}",
            f"Output folder       : {self.settings.output_dir}",
            f"Project file        : {project_file or '(not saved)'}",
            f"Project dirty       : {self._project_dirty}",
            "",
            f"Open documents      : {len(self._sessions)}",
            f"Current pages       : {session.page_count if session else 0}",
            f"Current page        : {self._page_index + 1 if session else 0}",
            f"Detected pages      : {len(session.detected_pages) if session else 0}",
            f"Cards               : {counts['detected']}",
            f"Approved            : {counts['approved']}",
            "",
            "OCR",
            "---",
            OcrEngine.diagnostic_report(smoke_test=False),
        ]
        self._show_text_dialog(
            "Diagnostics", "Application diagnostics", "\n".join(lines), monospace=True
        )

    def _set_status_message(self, text: str) -> None:
        progress = getattr(self, "_progress", None)
        if progress is not None:
            progress.set_message(text)

    def _open_library_file(self, path: str) -> None:
        """Load a library entry into the extraction viewer."""
        store = self._library_store
        actual_path = path
        if not Path(path).is_file() and store is not None:
            # Try path resolution fallback: relative_path, same-name in project dir
            file_id = self._get_library_file_id(path)
            if file_id is not None:
                try:
                    item = next(
                        (it for it in store.items() if it.id == file_id), None
                    )
                except ProjectError:
                    item = None
                if item is not None:
                    resolved = store.resolve_file_path(item)
                    if resolved is not None:
                        actual_path = resolved
        if not Path(actual_path).is_file():
            QMessageBox.warning(
                self,
                "File not found",
                f"The file is no longer on disk:\n{path}\n\n"
                "Use “Relink File…” to reconnect it to a new location.",
            )
            self._library_sidebar.refresh_statuses()
            return
        self.load_pdf(actual_path)

    def _relink_file(self, old_path: str) -> None:
        """Prompt the user to select a new location for a missing file and update the project database."""
        store = self._library_store
        if store is None:
            return
        # Find the library item ID for this path
        file_id = self._get_library_file_id(old_path)
        if file_id is None:
            self._set_status_message("Cannot find library entry to relink.")
            return
        start_dir = str(Path(old_path).parent) if Path(old_path).parent.is_dir() else str(Path.home())
        filters = (
            f"Documents and scans ({supported_file_filter()});;"
            "PDF files (*.pdf);;"
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;"
            "All files (*)"
        )
        new_path, _selected = QFileDialog.getOpenFileName(
            self, "Relink source file", start_dir, filters
        )
        if not new_path:
            return
        try:
            store.relink_file(file_id, new_path)
        except ProjectError as exc:
            QMessageBox.critical(self, "Cannot relink file", str(exc))
            return
        self._library_sidebar.refresh()
        self._set_status_message(f"File relinked: {Path(new_path).name}")
        # If this is the currently active session, reload it to refresh the view
        session = self.session
        if session is not None and str(session.path) == new_path:
            self._activate_session()

    def _add_to_library(self, path) -> None:
        """Keep the project in sync with the file that was just opened."""
        store = self._library_store
        widget = getattr(self, "_library_sidebar", None)
        if store is None or widget is None:
            return
        try:
            added = store.add_paths([path])
        except ProjectError as exc:
            self._set_status_message(f"Library not updated: {str(exc).splitlines()[0][:100]}")
            return
        if added:
            widget.refresh()

    def _mark_library_status(self, path, status: str) -> None:
        store = self._library_store
        widget = getattr(self, "_library_sidebar", None)
        if store is None or widget is None:
            return
        try:
            changed = store.set_status(path, status)
        except ProjectError:
            return
        if changed:
            widget.refresh()

    def _get_library_file_id(self, path: str) -> int | None:
        """Get the library item ID for a file path."""
        store = self._library_store
        if store is None:
            return None
        try:
            key = os.path.normcase(str(Path(path).expanduser().resolve()))
        except (OSError, RuntimeError, ValueError):
            return None
        for item in store.items():
            if os.path.normcase(item.file_path) == key:
                return item.id
        return None

    def _save_current_session_regions(self) -> None:
        """Save the current session's detected cards to the project database."""
        session = self.session
        store = self._library_store
        if session is None or store is None:
            return
        file_id = self._get_library_file_id(str(session.path))
        if file_id is None:
            return
        # Save regions for all pages in the session
        for page_index in range(session.page_count):
            cards = session.cards_for_page(page_index)
            if not cards:
                continue
            regions: list[DetectedRegion] = []
            for idx, card in enumerate(cards):
                # Persist the full normalized quad so rotated/skewed cards
                # round-trip exactly instead of collapsing to an axis-aligned
                # bounding box.
                quad = card.quad_norm.tolist()
                canonical = os.path.normcase(str(session.path))
                regions.append(
                    DetectedRegion(
                        id=0,  # Will be assigned by DB
                        file_id=file_id,
                        page_num=page_index + 1,  # 1-based page number
                        region_index=idx + 1,
                        doc_type=card.doc_type.value,
                        x=float(card.quad_norm[:, 0].min()),
                        y=float(card.quad_norm[:, 1].min()),
                        width=float(card.quad_norm[:, 0].max() - card.quad_norm[:, 0].min()),
                        height=float(card.quad_norm[:, 1].max() - card.quad_norm[:, 1].min()),
                        rotation_deg=card.rotation,
                        confidence=card.confidence,
                        orientation_ok=card.orientation_confident,
                        is_included=card.included,
                        quad_json=json.dumps(quad),
                        fine_rotation=card.fine_rotation,
                        file_path=str(session.path),
                        canonical_path=canonical,
                    )
                )
            try:
                store.save_regions(file_id, page_index + 1, regions)
            except ProjectError as exc:
                self._set_status_message(f"Failed to save regions: {str(exc).splitlines()[0][:100]}")
        # The regions now live in the project store.  That is *not* the same as
        # a saved ``.dceproj`` file (an in-memory project has no file yet), so
        # the project-level dirty flag is only cleared by save_project().
        session.dirty = False

    def _load_regions_for_session(self, session: DocumentSession) -> None:
        """Load saved regions from the project database into the session."""
        store = self._library_store
        if store is None:
            return
        file_id = self._get_library_file_id(str(session.path))
        if file_id is None:
            return
        try:
            all_regions = store.load_all_regions(file_id)
        except ProjectError as exc:
            self._set_status_message(f"Failed to load regions: {str(exc).splitlines()[0][:100]}")
            return
        # Clear existing in-memory cards before hydrating from SQLite so that
        # switching back and forth never duplicates regions.
        session.cards.clear()
        session.detected_pages.clear()
        for card in self._cards_from_regions(all_regions, session.page_count):
            session.add_card(card)

    @staticmethod
    def _cards_from_regions(
        all_regions: dict[int, list[DetectedRegion]], page_count: int
    ) -> list[DetectedCard]:
        """Convert stored regions into cards (shared by the viewer and export).

        *page_count* bounds the pages that are accepted: a project saved when the
        file had more pages must not produce cards that point past the end.
        """
        cards: list[DetectedCard] = []
        for page_num, regions in (all_regions or {}).items():
            page_index = page_num - 1  # Convert to 0-based
            if page_index < 0 or page_index >= page_count:
                continue
            for region in regions:
                # Reconstruct the full normalized quad from quad_json if present,
                # falling back to an axis-aligned bbox from x,y,w,h for old
                # projects that predate quad_json.
                if region.quad_json and region.quad_json.strip("[] "):
                    try:
                        quad_norm = np.asarray(
                            json.loads(region.quad_json), dtype=np.float32
                        ).reshape(4, 2)
                    except (json.JSONDecodeError, ValueError):
                        quad_norm = np.array([
                            [region.x, region.y],
                            [region.x + region.width, region.y],
                            [region.x + region.width, region.y + region.height],
                            [region.x, region.y + region.height],
                        ], dtype=np.float32)
                else:
                    quad_norm = np.array([
                        [region.x, region.y],
                        [region.x + region.width, region.y],
                        [region.x + region.width, region.y + region.height],
                        [region.x, region.y + region.height],
                    ], dtype=np.float32)
                cards.append(
                    DetectedCard(
                        page_index=page_index,
                        quad_norm=quad_norm,
                        confidence=region.confidence,
                        source="saved",
                        doc_type=DocumentType.from_value(region.doc_type),
                        type_confidence=region.confidence,
                        rotation=region.rotation_deg,
                        fine_rotation=region.fine_rotation,
                        included=region.is_included,
                        orientation_confident=region.orientation_ok,
                        page_size=(0, 0),  # updated on render in _regenerate_thumbnail
                    )
                )
        return cards

    # ---------------------------------------------------------- project files
    def _project_start_dir(self) -> str:
        """Folder a project file dialog should start in.

        "Open Project" must not start where "Add Folder"/"Open File" left off
        (``settings.last_input_dir``) - it starts in the folder of the last saved
        project instead.
        """
        last_project = str(getattr(self.settings, "last_project_path", "") or "")
        if last_project:
            folder = Path(last_project).expanduser().parent
            if folder.is_dir():
                return str(folder)
        store = self._library_store
        if store is not None and store.project_file is not None:
            folder = store.project_file.parent
            if folder.is_dir():
                return str(folder)
        last_input = str(getattr(self.settings, "last_input_dir", "") or "")
        if last_input and Path(last_input).is_dir():
            return last_input
        return str(Path.home())

    def open_project_dialog(self) -> None:
        if self._busy:
            self._set_status_message("Busy - wait until the current task finishes")
            return
        start = self._project_start_dir()
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Open project",
            start,
            f"ScanToCards project (*{PROJECT_SUFFIX});;All files (*)",
        )
        if path:
            self.open_project(path)

    def open_project(self, path: str) -> bool:
        """Replace the active library with the project stored in *path*."""
        if self._busy:
            self._set_status_message("Busy - wait until the current task finishes")
            return False
        try:
            store = ProjectStore.open(path)
        except ProjectError as exc:
            QMessageBox.critical(self, "Cannot open project", str(exc))
            return False

        # Close and clear any sessions from a previous project so stale
        # documents cannot leak into the newly opened project.
        self._sessions = []
        self._session_index = -1
        self._page_index = 0
        self._selected_card_id = ""

        previous, self._library_store = self._library_store, store
        self._library_sidebar.set_store(store)
        if previous is not None:
            previous.close()
        self.settings.last_project_path = str(store.project_file or "")
        self.settings.save()
        self._set_status_message(f"Project opened: {Path(path).name}")
        self._update_window_title()
        # Explain an empty/broken project and never leave the user staring at a
        # blank workspace: the first usable document is opened in the viewer.
        self._refresh_library_statuses()
        self._report_project_contents(Path(path).name, warn_empty=True)
        self._load_first_project_file()
        # A project without usable files never reaches _activate_session(), so
        # the toolbar/menu would keep the state of the *previous* project.
        self._update_state()
        self._show_extract_page()
        return True

    def _load_first_project_file(self) -> bool:
        """Load all library files into sessions so the document selector can switch between them."""
        store = self._library_store
        if store is None:
            return False
        try:
            items = store.items()
            active_file_id, active_page_num = store.get_active_file_and_page()
        except ProjectError:
            return False

        existing = [item.file_path for item in items if item.exists]
        if not existing:
            return False

        # Show progress while the project's documents are opened: restoring a
        # 100-file project used to freeze the window completely.
        # (_load_paths owns the progress panel: start, per-file updates, finish.)
        project_label = store.project_file.name if store.project_file else store.name
        self._load_paths(existing, add=True, caption=f"Opening project {project_label}…")

        if not self._sessions:
            return False

        # Activate the correct session (active file from project state, or first loaded)
        active_session_idx = -1
        if active_file_id is not None:
            for index, session in enumerate(self._sessions):
                if self._get_library_file_id(str(session.path)) == active_file_id:
                    active_session_idx = index
                    break
        self._session_index = max(active_session_idx, 0)
        self._activate_session()

        # Select the active page
        if active_page_num > 1 and self.session:
            target_page = min(active_page_num - 1, self.session.page_count - 1)
            if target_page >= 0:
                self.select_page(target_page)

        return True

    def save_project_as(self) -> bool:
        store = self._library_store
        if store is None:
            return False
        if self._busy:
            self._set_status_message("Busy - wait until the current task finishes")
            return False
        if store.project_file is not None:
            start = str(store.project_file)
        else:
            folder = Path(self._project_start_dir())
            start = str(folder / f"{store.name}{PROJECT_SUFFIX}")
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Save project as",
            start,
            f"ScanToCards project (*{PROJECT_SUFFIX})",
        )
        if not path:
            return False
        return self.save_project(path)

    def save_project_quick(self) -> bool:
        """Save the project to its current file (Ctrl+S).

        A project that was never saved has no target file yet, so this falls back
        to "Save Project As…" instead of failing silently.
        """
        store = self._library_store
        if store is None:
            return False
        if self._busy:
            self._set_status_message("Busy - wait until the current task finishes")
            return False
        if store.project_file is None:
            return self.save_project_as()
        return self.save_project(str(store.project_file))

    def save_project(self, path: str) -> bool:
        """Write the library to *path* and keep working on that file."""
        store = self._library_store
        if store is None:
            return False
        # Flush the currently active document's regions to SQLite before saving
        self._save_current_session_regions()
        try:
            saved = store.save_as(path)
        except (ProjectError, OSError) as exc:
            QMessageBox.critical(self, "Cannot save project", str(exc))
            return False
        self._library_sidebar.refresh()
        self.settings.last_project_path = str(saved)
        self.settings.save()
        for session in self._sessions:
            session.dirty = False
        self._clear_project_dirty()
        self._set_status_message(f"Project saved: {saved.name}")
        self._update_window_title()
        try:
            count = len(store.items())
        except ProjectError:
            count = 0
        if count == 0:
            QMessageBox.warning(
                self,
                "Project saved without documents",
                f"“{saved.name}” was saved, but its library is empty.\n\n"
                "Add files with the Document Library panel and save again.",
            )
        return True

    def new_project(self) -> None:
        """File → New Project: start from an empty canvas and library."""
        if self._busy:
            self._set_status_message("Busy - wait until the current task finishes")
            return
        if not self._confirm_discard_project():
            return
        self.close_project()
        self._set_status_message("New project - add files to the Document Library")
        # A new project is an empty workspace: show it, so the user is not left
        # on the settings/history page wondering where the canvas went.
        self._show_extract_page()

    def request_close_project(self) -> None:
        """File → Close Project: close the project, confirming unsaved work."""
        if self._busy:
            self._set_status_message("Busy - wait until the current task finishes")
            return
        if not self._confirm_discard_project():
            return
        self.close_project()

    def _confirm_discard_project(self) -> bool:
        """Warn before a project command throws away unsaved work.

        Returns ``False`` when the user cancelled.  Nothing is asked when the
        project is already clean, so closing an untouched window never nags.
        """
        if not self._project_dirty and not any(
            session.dirty for session in self._sessions
        ):
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "This project has changes that are not saved yet.\n\n"
            "Continue and discard them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def close_project(self) -> None:
        """Close the current project and start with an empty canvas/library."""
        store = self._library_store
        if store is not None:
            # Flush current session's regions before closing
            self._save_current_session_regions()
            store.close()
        self._library_store = None

        # Clear all document sessions
        self._sessions = []
        self._session_index = -1
        self._page_index = 0
        self._selected_card_id = ""

        # Close any open preview document
        self._close_preview_document()
        self._preview_document = None

        # Clear the page image buffer and canvas so no stale scanned page remains
        self._page_image = None
        self._view.show_page(bgr_to_qimage(None))

        # Create a fresh empty in-memory project
        self._library_store = ProjectStore.create()

        # Refresh the library sidebar with the empty store
        self._library_sidebar.set_store(self._library_store)

        # Clear the document combo box
        self._document_combo.blockSignals(True)
        self._document_combo.clear()
        self._document_combo.blockSignals(False)

        # Reset page controls
        self._page_spin.blockSignals(True)
        self._page_spin.setRange(1, 1)
        self._page_spin.setValue(1)
        self._page_spin.blockSignals(False)
        self._page_total_label.setText("of 0")

        # Clear gallery and canvas state
        self._gallery.clear()
        self._refresh_gallery()
        self._refresh_overlays()
        self._update_state()

        # The freshly created project is empty, so there is nothing unsaved.
        self._clear_project_dirty()

        # Reset window title
        self._update_window_title()

        self._set_status_message("Project closed - start with an empty canvas")
        # Closing leaves an empty workspace: show it instead of the page the
        # user happened to be on when they closed the project.
        self._show_extract_page()

    def _create_shortcuts(self) -> None:
        """Register the shortcuts that have no menu entry.

        ``Ctrl+O`` (Open File), ``Ctrl+E`` (Export All), ``Ctrl+S``/``Ctrl+Shift+S``
        (Save / Save As), ``Ctrl+N`` (New Project), ``Ctrl+W`` (Close Project),
        ``Ctrl+Q`` (Exit) and ``Ctrl+R`` (Start Detection) live on menu actions,
        so they are deliberately not duplicated here - two actions with the same
        key sequence would make the shortcut ambiguous and Qt would fire neither
        of them.
        """
        escape_action = QAction(self)
        escape_action.setShortcut(QKeySequence("Esc"))
        escape_action.triggered.connect(self._leave_add_mode)
        self.addAction(escape_action)

    def _switch_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        for position, button in enumerate(self._nav_buttons):
            button.setChecked(position == index)
            color = styles.PRIMARY_DARK if position == index else styles.TEXT_MUTED
            button.setIcon(icon(("dashboard", "extract", "history", "settings")[position], color))
        if index == 0:
            self._refresh_dashboard()
        elif index == 2:
            self._refresh_history_table()
        # The View menu greys out the page that is already on screen, so the
        # menu has to follow the navigation.
        self._update_menu_state()

    def _show_extract_page(self, require_document: bool = False) -> None:
        """Bring the extraction canvas to the front after a document action.

        Opening a file already lands on the canvas (``load_pdf`` switches), but
        the project commands and the library-only commands used to leave the
        user on whatever page they were on.  The navigation is skipped when the
        canvas is already visible so the helper never rebuilds the
        dashboard/history tables for nothing.

        *require_document* is used by Add File / Add Folder: those commands only
        fill the library, and switching to an empty canvas would hide the very
        list the user just changed.  They therefore only navigate when there is
        a document on screen to go back to.
        """
        if require_document and self.session is None:
            return
        pages = getattr(self, "_pages", None)
        if pages is not None and pages.currentIndex() != 1:
            self._switch_page(1)

    # ------------------------------------------------------------ extract page
    def _build_extract_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(10)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Extract Documents")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Detect cards on a scanned page, review them, then export each one as an image.")
        subtitle.setObjectName("PageSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        self._document_combo = QComboBox()
        self._document_combo.setMinimumWidth(300)
        self._document_combo.currentIndexChanged.connect(self._on_document_changed)
        header.addWidget(self._document_combo)
        layout.addLayout(header)

        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(8)

        self._btn_open = QPushButton(button_icon("folder", "primary"), " Open File")
        self._btn_open.setProperty("variant", "primary")
        self._btn_open.setToolTip(
            "Open a single file in the canvas for quick extraction.\n"
            "It replaces the documents currently open (they stay in the library)."
        )
        self._btn_open.clicked.connect(self.open_pdf_dialog)

        self._btn_add = QPushButton(icon("plus"), " Add Files")
        self._btn_add.setToolTip(
            "Add one or more files to the Document Library.\n"
            "Nothing is opened in the canvas - double-click a library row to open it."
        )
        self._btn_add.clicked.connect(self.add_pdf_dialog)

        self._btn_add_folder = QPushButton(icon("folder"), " Add Folder")
        self._btn_add_folder.setToolTip("Add all supported files from a folder (recursively)")
        self._btn_add_folder.clicked.connect(self.add_folder_dialog)

        self._btn_detect = QPushButton(icon("play"), " Start Detection")
        self._btn_detect.clicked.connect(lambda: self.start_detection())

        self._btn_redetect = QPushButton(icon("refresh"), " Re-run Page")
        self._btn_redetect.clicked.connect(lambda: self.start_detection([self._page_index]))

        self._btn_export = QPushButton(icon("export"), " Export All")
        self._btn_export.clicked.connect(self.export_all)

        for button in (
            self._btn_open,
            self._btn_add,
            self._btn_add_folder,
            self._btn_detect,
            self._btn_redetect,
            self._btn_export,
        ):
            button.setObjectName("ToolbarButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            toolbar_layout.addWidget(button)
        toolbar_layout.addStretch(1)
        layout.addWidget(toolbar)

        # --------------------------------------------------------- page strip
        page_bar = QHBoxLayout()
        page_bar.setSpacing(6)
        self._btn_prev = QToolButton()
        self._btn_prev.setObjectName("IconButton")
        self._btn_prev.setIcon(icon("chevron_left"))
        self._btn_prev.setToolTip("Previous page")
        self._btn_prev.clicked.connect(lambda: self.select_page(self._page_index - 1))
        self._btn_next = QToolButton()
        self._btn_next.setObjectName("IconButton")
        self._btn_next.setIcon(icon("chevron_right"))
        self._btn_next.setToolTip("Next page")
        self._btn_next.clicked.connect(lambda: self.select_page(self._page_index + 1))
        self._page_spin = QSpinBox()
        self._page_spin.setRange(1, 1)
        self._page_spin.setFixedWidth(70)
        self._page_spin.valueChanged.connect(lambda value: self.select_page(value - 1))
        self._page_total_label = QLabel("of 0")
        self._page_total_label.setObjectName("Muted")

        page_bar.addWidget(self._btn_prev)
        page_bar.addWidget(self._page_spin)
        page_bar.addWidget(self._page_total_label)
        page_bar.addWidget(self._btn_next)
        page_bar.addSpacing(16)

        # Page range for detection (optional, empty = all pages)
        self._page_range_edit = QLineEdit()
        self._page_range_edit.setPlaceholderText("Pages (e.g. 1,3,5-7)")
        self._page_range_edit.setFixedWidth(160)
        self._page_range_edit.setToolTip("Pages to run detection on. Format: 1,3,5-7 (empty = all pages)")
        page_bar.addWidget(self._page_range_edit)

        self._btn_zoom_out = QToolButton()
        self._btn_zoom_out.setObjectName("IconButton")
        self._btn_zoom_out.setIcon(icon("zoom_out"))
        self._btn_zoom_out.clicked.connect(lambda: self._view.zoom_out())
        self._btn_zoom_in = QToolButton()
        self._btn_zoom_in.setObjectName("IconButton")
        self._btn_zoom_in.setIcon(icon("zoom_in"))
        self._btn_zoom_in.clicked.connect(lambda: self._view.zoom_in())
        self._btn_fit = QToolButton()
        self._btn_fit.setObjectName("IconButton")
        self._btn_fit.setIcon(icon("fit"))
        self._btn_fit.setToolTip("Fit page to window")
        self._btn_fit.clicked.connect(lambda: self._view.fit_to_window())
        for button in (self._btn_zoom_out, self._btn_zoom_in, self._btn_fit):
            page_bar.addWidget(button)

        page_bar.addSpacing(16)
        self._btn_add_region = QPushButton(icon("plus"), " Add Region")
        self._btn_add_region.setCheckable(True)
        self._btn_add_region.setToolTip("Draw a rectangle to add a document the detector missed (Esc to cancel)")
        self._btn_add_region.toggled.connect(self._toggle_add_mode)

        self._btn_delete = QPushButton(icon("trash"), " Remove")
        self._btn_delete.setToolTip("Remove the selected document from the list")
        self._btn_delete.clicked.connect(self._delete_selected_card)

        page_bar.addWidget(self._btn_add_region)
        self._region_preset_combo = QComboBox()
        self._region_preset_combo.setMinimumWidth(190)
        for label, token, _size in REGION_PRESETS:
            self._region_preset_combo.addItem(label, token)
        self._region_preset_combo.setToolTip(
            "Size of the region you draw with 'Add Region'\n"
            "e.g. ID card 85.6 x 54 mm so the crop matches the document exactly"
        )
        self._region_preset_combo.currentIndexChanged.connect(self._on_region_preset_changed)
        # re-selecting "Custom…" while it is already current does not change the
        # index, so the dedicated button gives the dialog an always-working path
        self._region_preset_combo.activated.connect(self._on_region_preset_activated)
        page_bar.addWidget(self._region_preset_combo)

        self._region_size_button = QToolButton()
        self._region_size_button.setText("Size…")
        self._region_size_button.setToolTip("Enter a custom width and height for the drawn region")
        self._region_size_button.clicked.connect(lambda: self._open_region_size_dialog())
        page_bar.addWidget(self._region_size_button)
        page_bar.addWidget(self._btn_delete)
        page_bar.addStretch(1)

        self._page_hint = QLabel("")
        self._page_hint.setObjectName("Faint")
        page_bar.addWidget(self._page_hint)
        layout.addLayout(page_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self._view = PdfPageView()
        self._view.cardSelected.connect(self._on_card_selected)
        self._view.cardActivated.connect(self.edit_crop)
        self._view.quadEdited.connect(self._on_quad_edited)
        self._view.manualRectDrawn.connect(self._on_manual_rect)
        splitter.addWidget(self._view)
        splitter.addWidget(self._build_gallery_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([780, 560])
        layout.addWidget(splitter, 1)
        return page

    def _build_gallery_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Extracted documents")
        title.setObjectName("SectionTitle")
        self._gallery_count = QLabel("0")
        self._gallery_count.setObjectName("Faint")
        header.addWidget(title)
        header.addWidget(self._gallery_count)
        header.addStretch(1)
        self._gallery_filter = QComboBox()
        self._gallery_filter.addItem("This page", "page")
        self._gallery_filter.addItem("All pages", "all")
        self._gallery_filter.currentIndexChanged.connect(lambda _index: self._refresh_gallery())
        header.addWidget(self._gallery_filter)
        layout.addLayout(header)

        # Filter bar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(6)
        self._gallery_search = QLineEdit()
        self._gallery_search.setPlaceholderText("Search (type, identifier, OCR text, page…)")
        self._gallery_search.setClearButtonEnabled(True)
        self._gallery_search.textChanged.connect(lambda _t: self._refresh_gallery())
        self._gallery_type_filter = QComboBox()
        self._gallery_type_filter.addItem("All types", None)
        self._gallery_type_filter.setMinimumWidth(160)
        self._gallery_type_filter.currentIndexChanged.connect(lambda _i: self._refresh_gallery())
        self._gallery_page_filter = QComboBox()
        self._gallery_page_filter.addItem("All pages", None)
        self._gallery_page_filter.setMinimumWidth(100)
        self._gallery_page_filter.currentIndexChanged.connect(lambda _i: self._refresh_gallery())
        self._gallery_included_only = QCheckBox("Included only")
        self._gallery_included_only.toggled.connect(lambda _c: self._refresh_gallery())
        self._gallery_confident_only = QCheckBox("Orientation OK only")
        self._gallery_confident_only.toggled.connect(lambda _c: self._refresh_gallery())
        filter_bar.addWidget(self._gallery_search, 1)
        filter_bar.addWidget(self._gallery_type_filter)
        filter_bar.addWidget(self._gallery_page_filter)
        filter_bar.addWidget(self._gallery_included_only)
        filter_bar.addWidget(self._gallery_confident_only)
        layout.addLayout(filter_bar)

        self._gallery = DocumentGallery()
        self._gallery.cardSelected.connect(self._on_card_selected)
        self._gallery.cardActivated.connect(self.edit_crop)
        self._gallery.rotateRequested.connect(self.rotate_card)
        self._gallery.typeChangeRequested.connect(self.set_card_type)
        self._gallery.includeToggled.connect(self.toggle_card_included)
        self._gallery.cropEditRequested.connect(self.edit_crop)
        self._gallery.ocrTextRequested.connect(self.show_ocr_text)
        self._gallery.removeRequested.connect(self.remove_card)
        self._gallery.exportRequested.connect(self.export_card)
        layout.addWidget(self._gallery, 1)

        edit_row = QHBoxLayout()
        edit_row.setSpacing(6)
        self._btn_rot_left = QToolButton()
        self._btn_rot_left.setObjectName("IconButton")
        self._btn_rot_left.setIcon(icon("rotate_left"))
        self._btn_rot_left.setToolTip("Rotate 90° left")
        self._btn_rot_left.clicked.connect(lambda: self._rotate_selected(270))
        self._btn_rot_right = QToolButton()
        self._btn_rot_right.setObjectName("IconButton")
        self._btn_rot_right.setIcon(icon("rotate_right"))
        self._btn_rot_right.setToolTip("Rotate 90° right")
        self._btn_rot_right.clicked.connect(lambda: self._rotate_selected(90))
        self._btn_rot_180 = QToolButton()
        self._btn_rot_180.setObjectName("IconButton")
        self._btn_rot_180.setIcon(icon("flip"))
        self._btn_rot_180.setToolTip("Rotate 180°")
        self._btn_rot_180.clicked.connect(lambda: self._rotate_selected(180))
        self._btn_crop = QToolButton()
        self._btn_crop.setObjectName("IconButton")
        self._btn_crop.setIcon(icon("crop"))
        self._btn_crop.setToolTip("Adjust crop")
        self._btn_crop.clicked.connect(lambda: self.edit_crop(self._selected_card_id))

        self._type_combo = QComboBox()
        self._type_combo.setMinimumWidth(140)
        self._type_combo.activated.connect(self._on_type_combo_changed)
        self._populate_type_combo()

        self._include_check = QCheckBox("Include")
        self._include_check.toggled.connect(self._on_include_check_toggled)

        for widget in (self._btn_rot_left, self._btn_rot_right, self._btn_rot_180, self._btn_crop):
            edit_row.addWidget(widget)
        edit_row.addWidget(self._type_combo, 1)
        edit_row.addWidget(self._include_check)
        layout.addLayout(edit_row)
        return panel

    # ---------------------------------------------------------- dashboard page
    def _build_dashboard_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(14)

        title = QLabel("Dashboard")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Extract passport, Iqama, Istimara and driving licence cards from scanned "
            "PDF files or images."
        )
        subtitle.setObjectName("PageSubtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self._stat_cards: dict[str, QLabel] = {}
        for key, label in (
            ("loaded", "Files loaded"),
            ("detected", "Documents detected"),
            ("approved", "Approved for export"),
            ("exported", "Documents exported"),
        ):
            card = QFrame()
            card.setObjectName("StatCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 14, 16, 14)
            value = QLabel("0")
            value.setObjectName("StatValue")
            caption = QLabel(label)
            caption.setObjectName("StatLabel")
            card_layout.addWidget(value)
            card_layout.addWidget(caption)
            self._stat_cards[key] = value
            stats_row.addWidget(card)
        outer.addLayout(stats_row)

        actions = QFrame()
        actions.setObjectName("Card")
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(16, 14, 16, 14)
        actions_layout.setSpacing(8)
        heading = QLabel("Quick start")
        heading.setObjectName("SectionTitle")
        actions_layout.addWidget(heading)
        hint = QLabel(
            "1. Open a scanned PDF or image (or drag & drop it into the window).\n"
            "2. Press “Start Detection” to find the documents automatically.\n"
            "3. Review the thumbnails, fix the rotation if necessary and set the document type.\n"
            "4. Press “Export All” to write one image file per document."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        actions_layout.addWidget(hint)
        button_row = QHBoxLayout()
        open_button = QPushButton(button_icon("folder", "primary"), " Open File")
        open_button.setProperty("variant", "primary")
        open_button.clicked.connect(self.open_pdf_dialog)
        extract_button = QPushButton(icon("extract"), " Go to extraction")
        extract_button.clicked.connect(lambda: self._switch_page(1))
        settings_button = QPushButton(icon("settings"), " Open settings")
        settings_button.clicked.connect(lambda: self._switch_page(3))
        button_row.addWidget(open_button)
        button_row.addWidget(extract_button)
        button_row.addWidget(settings_button)
        button_row.addStretch(1)
        actions_layout.addLayout(button_row)
        outer.addWidget(actions)

        recent = QFrame()
        recent.setObjectName("Card")
        recent_layout = QVBoxLayout(recent)
        recent_layout.setContentsMargins(16, 14, 16, 14)
        recent_layout.setSpacing(8)
        recent_title_row = QHBoxLayout()
        recent_title_row.setSpacing(8)
        recent_title = QLabel("Recent files")
        recent_title.setObjectName("SectionTitle")
        self._recent_clear_btn = QPushButton(icon("trash"), " Clear list")
        self._recent_clear_btn.setProperty("variant", "ghost")
        self._recent_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recent_clear_btn.setToolTip("Remove every entry from the recent files list")
        self._recent_clear_btn.clicked.connect(self._clear_recent_files)
        recent_title_row.addWidget(recent_title)
        recent_title_row.addStretch(1)
        recent_title_row.addWidget(self._recent_clear_btn)
        recent_layout.addLayout(recent_title_row)

        self._recent_list = QListWidget()
        self._recent_list.setObjectName("RecentList")
        self._recent_list.setMaximumHeight(190)
        self._recent_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recent_list.itemDoubleClicked.connect(self._open_recent_item)
        recent_layout.addWidget(self._recent_list)
        outer.addWidget(recent)
        outer.addStretch(1)
        return page

    def _refresh_dashboard(self) -> None:
        detected = sum(len(session.cards) for session in self._sessions)
        approved = sum(len(session.included_cards()) for session in self._sessions)
        totals = self.history.totals()
        self._stat_cards["loaded"].setText(str(len(self._sessions)))
        self._stat_cards["detected"].setText(str(detected))
        self._stat_cards["approved"].setText(str(approved))
        self._stat_cards["exported"].setText(str(totals["documents"]))

        self._recent_list.clear()
        recent = [path for path in self.settings.recent_files if Path(path).exists()]
        if hasattr(self, "_recent_clear_btn"):
            self._recent_clear_btn.setEnabled(bool(recent))
        if not recent:
            item = QListWidgetItem("No recent files yet — open a PDF or image to get started.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._recent_list.addItem(item)
            return
        for path in recent:
            name = Path(path).name
            item = QListWidgetItem(icon("document"), f"{name}     {path}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(f"{path}\n\nDouble-click to open this file")
            self._recent_list.addItem(item)

    def _clear_recent_files(self) -> None:
        """Empty the "recent files" list (the PDF files themselves are kept)."""
        if not self.settings.recent_files:
            return
        answer = QMessageBox.question(
            self,
            "Clear recent files",
            "Remove all entries from the recent files list?\n\nThe PDF files themselves are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.recent_files = []
        self.settings.save()
        self._refresh_dashboard()
        self._progress.set_message("Recent files list cleared")

    def _open_recent_item(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.load_pdf(str(path))

    # ------------------------------------------------------------ history page
    def _build_history_page(self) -> QWidget:
        """The history widgets/actions live in ``app.history_page``."""
        page = HistoryPage(self)
        self._history_table = page._history_table
        self._history_page = page
        return page

    def _refresh_history_table(self) -> None:
        self._history_page._refresh_history_table()

    def _open_history_folder(self) -> None:
        self._history_page._open_history_folder()

    def _clear_history(self) -> None:
        self._history_page._clear_history()

    # ----------------------------------------------------------- settings page
    def _build_settings_page(self) -> QWidget:
        """The settings page widgets/actions live in ``app.settings_page``.

        The widget handles are mirrored onto the window so the Add Region
        state machine below (and the tests) can keep using the flat names.
        """
        page = SettingsPage(self)
        for name in SettingsPage.PASSTHROUGH_HANDLES:
            setattr(self, name, getattr(page, name))
        self._settings_page = page
        return page

    @property
    def _type_cells(self) -> dict[str, dict[str, object]]:
        """Rebuilt by the settings page on every change - always read live."""
        return self._settings_page._type_cells

    def _load_settings_into_form(self) -> None:
        self._settings_page._load_settings_into_form()

    def _save_settings_from_form(self) -> None:
        self._settings_page._save_settings_from_form()

    def _restore_settings_defaults(self) -> None:
        self._settings_page._restore_settings_defaults()

    def _check_ocr_engines(self) -> None:
        self._settings_page._check_ocr_engines()

    def _refresh_ocr_hint(self) -> None:
        self._settings_page._refresh_ocr_hint()

    def _browse_output_dir(self) -> None:
        self._settings_page._browse_output_dir()

    def _refresh_type_list(self) -> None:
        self._settings_page._refresh_type_list()

    def _type_grid_columns(self) -> int:
        return self._settings_page._type_grid_columns()

    def _clear_type_grid(self) -> None:
        self._settings_page._clear_type_grid()

    def _type_summary(self, doc_type, known_words, user_words) -> str:
        return self._settings_page._type_summary(doc_type, known_words, user_words)

    def _set_type_tile_enabled_look(self, token: str, enabled: bool) -> None:
        self._settings_page._set_type_tile_enabled_look(token, enabled)

    def _on_type_toggled(self, token: str, checked: bool) -> None:
        self._settings_page._on_type_toggled(token, checked)

    def _edit_keywords_for(self, token: str) -> None:
        self._settings_page._edit_keywords_for(token)

    def _add_document_type(self) -> None:
        self._settings_page._add_document_type()

    def _remove_document_type(self, token: str = "") -> None:
        self._settings_page._remove_document_type(token)

    def _update_filename_preview(self) -> None:
        self._settings_page._update_filename_preview()

    # ------------------------------------------------------- document handling
    def open_pdf_dialog(self) -> None:
        """Open one document in the canvas, replacing what is open now.

        "Open File" is single-document focus mode: the file replaces the
        workspace.  "Add File" only fills the Document Library.
        """
        path = self._ask_for_pdf("Open PDF or image")
        if not path:
            return
        if not self._confirm_discard_open_documents():
            return
        self.load_pdf(path, add=False)

    def _confirm_discard_open_documents(self) -> bool:
        """Warn before "Open File" throws away cards that are not saved yet.

        Returns ``False`` when the user cancelled.  The question is only asked
        when something would really be lost: a session is ``dirty`` while its
        cards differ from the project store, and ``_project_dirty`` covers
        library changes that are not in the saved ``.dceproj`` file yet.
        """
        if not self._project_dirty and not any(
            session.dirty for session in self._sessions
        ):
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "Opening a new file will replace the documents in the canvas.\n\n"
            "Unsaved changes will be lost. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def add_pdf_dialog(self) -> None:
        """Add one or more documents to the Document Library.

        The files are *not* opened in the canvas - the library is the batch
        workflow and the user double-clicks a row when they want to work on it.
        """
        paths = self._ask_for_pdfs("Add PDFs or images to the library")
        if not paths:
            return
        self._add_paths_with_progress(paths, caption="Adding files to the library…")
        # The library rows were just added; show the canvas that works on them
        # (only when a document is already open - see _show_extract_page).
        self._show_extract_page(require_document=True)

    def _add_paths_to_library(self, paths: list[str]) -> int:
        """Register *paths* in the project library; returns how many were new."""
        store = self._library_store
        widget = getattr(self, "_library_sidebar", None)
        if store is None or widget is None:
            return 0
        try:
            added = store.add_paths(list(paths))
        except ProjectError as exc:
            self._set_status_message(
                f"Library not updated: {str(exc).splitlines()[0][:100]}"
            )
            return 0
        if added:
            widget.refresh()
            self._mark_project_dirty()
        return len(added)

    def add_folder_dialog(self) -> None:
        """Add every supported file of a folder to the library (no canvas load)."""
        if self._busy:
            self._progress.set_message("Busy - wait until the current task finishes")
            return
        start = self.settings.last_input_dir or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Add folder to library", start)
        if not folder:
            return
        # Scan folder recursively for supported files
        discovered = self._scan_folder_for_files(folder)
        if not discovered:
            QMessageBox.warning(
                self,
                "No documents found",
                f"No supported PDF or image files were found in:\n{folder}",
            )
            return
        self._add_paths_with_progress(
            discovered, caption=f"Adding {Path(folder).name}…"
        )
        # Same as Add File: the library is the result, so only leave the
        # dashboard/history page when there is a canvas to return to.
        self._show_extract_page(require_document=True)

    def _add_paths_with_progress(self, paths: list[str], caption: str) -> int:
        """Add *paths* to the library in cancellable batches.

        Counting the pages of a large folder means opening every file, which
        used to freeze the window.  The progress panel is updated (and the event
        loop pumped) between files so the user sees the work and can cancel.

        Returns the number of files that were newly added.
        """
        paths = [str(path) for path in paths if path]
        total = len(paths)
        if total == 0:
            return 0
        if self._loading_batch:
            return self._add_paths_to_library(paths)

        self._loading_batch = True
        self._batch_cancel = False
        added = 0
        try:
            self._progress.start(caption)
            for index, path in enumerate(paths):
                if self._batch_cancel:
                    break
                self._progress.set_progress(
                    index, total, f"Adding {Path(path).name} ({index + 1}/{total})…"
                )
                QApplication.processEvents()
                added += self._add_paths_to_library([path])
        finally:
            self._loading_batch = False
            self._batch_cancel = False
            if added == total:
                self._progress.finish(
                    f"Added {added} file" + ("" if added == 1 else "s") + " to the library"
                )
            elif added:
                self._progress.finish(f"Added {added} of {total} file(s) to the library")
            else:
                self._progress.finish("Those files are already in the library")
        return added

    def _load_paths(
        self,
        paths: list[str],
        add: bool = True,
        caption: str = "Loading files…",
        first_replaces: bool = False,
    ) -> int:
        """Load *paths* one by one while the status bar shows the progress.

        Opening a document renders a page and rebuilds the gallery, so a folder
        with 50 scans used to freeze the window until the loop finished.  The
        progress panel is updated (and the event loop pumped) between files so
        the user sees what is happening and can cancel.

        *first_replaces* makes the first file replace the open workspace while the
        remaining files are added to it (used when files are dropped on an empty
        window).

        Returns the number of files that were loaded successfully.
        """
        paths = [str(path) for path in paths if path]
        total = len(paths)
        if total == 0:
            return 0
        if self._loading_batch:
            # Nested call (e.g. a project restore triggered from a drop) - just
            # load the files, the outer loop owns the progress panel.
            return sum(1 for path in paths if self.load_pdf(path, add=add))

        self._loading_batch = True
        self._batch_cancel = False
        loaded = 0
        try:
            self._progress.start(caption)
            for index, path in enumerate(paths):
                if self._batch_cancel:
                    break
                name = Path(path).name
                self._progress.set_progress(
                    index, total, f"Loading {name} ({index + 1}/{total})…"
                )
                # Repaint the label/bar before the (blocking) load of this file.
                QApplication.processEvents()
                use_add = add if not (first_replaces and index == 0) else False
                if self.load_pdf(path, add=use_add):
                    loaded += 1
        finally:
            self._loading_batch = False
            self._batch_cancel = False
            if loaded == total:
                self._progress.finish(f"Loaded {loaded} file" + ("" if loaded == 1 else "s"))
            elif loaded:
                self._progress.finish(f"Loaded {loaded} of {total} file(s)")
            else:
                self._progress.finish("No files were loaded")
        return loaded

    def _scan_folder_for_files(self, folder: str) -> list[str]:
        """Scan folder recursively for supported PDF and image files."""
        found: list[str] = []
        try:
            for path in sorted(Path(folder).rglob("*")):
                try:
                    if path.is_file() and is_supported_file(path):
                        found.append(str(path))
                except OSError:
                    continue
        except OSError:
            pass
        return found

    def _ask_for_pdf(self, caption: str) -> str:
        start = self.settings.last_input_dir or str(Path.home())
        filters = (
            f"Documents and scans ({supported_file_filter()});;"
            "PDF files (*.pdf);;"
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;"
            "All files (*)"
        )
        path, _filter = QFileDialog.getOpenFileName(self, caption, start, filters)
        return path

    def _ask_for_pdfs(self, caption: str) -> list[str]:
        """Multi-select variant of :meth:`_ask_for_pdf` (Add File)."""
        start = self.settings.last_input_dir or str(Path.home())
        filters = (
            f"Documents and scans ({supported_file_filter()});;"
            "PDF files (*.pdf);;"
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;"
            "All files (*)"
        )
        paths, _filter = QFileDialog.getOpenFileNames(self, caption, start, filters)
        return [path for path in paths if path]

    def load_pdf(self, path: str, add: bool = False, password: str = "") -> bool:
        """Open *path* and make it the active document."""
        if self._busy:
            # A detection/export worker targets the session that was current
            # when it was started - switching files (or the page reset) mid-run
            # would deliver its results to the wrong document.
            self._progress.set_message("Busy - wait until the current task finishes")
            return False
        pdf_path = Path(path)
        for index, existing in enumerate(self._sessions):
            if existing.path == pdf_path:
                # Already open: switch to it and make sure the workspace is shown
                # (opening it from the dashboard used to leave the user there).
                if add or len(self._sessions) == 1:
                    if index != self._session_index:
                        self._session_index = index
                        self._sync_document_combo()
                        self._activate_session()
                    else:
                        self._sync_document_combo()
                else:
                    # "Open File" on a document that is already open among
                    # several: keep only that one, so the command really does
                    # replace the workspace instead of silently doing nothing.
                    self._save_current_session_regions()
                    self._sessions = [existing]
                    self._session_index = 0
                    self._sync_document_combo()
                    self._activate_session()
                self._switch_page(1)
                # Ensure the file is also registered in the library database
                # (it may have been removed from the library while still open).
                self._add_to_library(pdf_path)
                return True
        try:
            document = PdfDocument(pdf_path, password=password or None)
            page_count = document.page_count
        except PdfError as exc:
            message = str(exc)
            if "password" in message.lower():
                # Prompt (or re-prompt) until the password is correct or the
                # user cancels - a wrong password should not force the user
                # through the file dialog all over again.
                if password:
                    message = f"That password was not accepted.\n{message}"
                entered, ok = QInputDialog.getText(
                    self, "Password required", message, QLineEdit.EchoMode.Password
                )
                if ok and entered:
                    return self.load_pdf(path, add=add, password=entered)
            QMessageBox.critical(self, "Cannot open file", message)
            return False
        except (OSError, RuntimeError, ValueError) as exc:  # safety net for unexpected errors
            QMessageBox.critical(self, "Cannot open file", str(exc))
            return False

        session = DocumentSession(path=pdf_path, page_count=page_count, password=password or "")
        # keep the already-open document for the preview instead of reopening
        self._pending_preview_document = document
        if add or not self._sessions:
            self._sessions.append(session)
            self._session_index = len(self._sessions) - 1
        else:
            self._sessions = [session]
            self._session_index = 0

        self.settings.remember_file(str(pdf_path))
        self.settings.save()
        self._sync_document_combo()
        self._activate_session()
        self._switch_page(1)
        self._refresh_dashboard()
        self._add_to_library(pdf_path)
        return True

    def _sync_document_combo(self) -> None:
        self._document_combo.blockSignals(True)
        self._document_combo.clear()
        for session in self._sessions:
            self._document_combo.addItem(icon("document"), session.label())
        if self._sessions:
            self._document_combo.setCurrentIndex(max(0, self._session_index))
        self._document_combo.blockSignals(False)

    def _on_document_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._sessions) or index == self._session_index:
            return
        # Save current session's regions before switching
        self._save_current_session_regions()
        self._session_index = index
        self._activate_session()

    def _activate_session(self) -> None:
        # Save current session's regions before switching (if any)
        if self._session_index >= 0 and self._session_index < len(self._sessions):
            self._save_current_session_regions()
        self._close_preview_document()
        self._selected_card_id = ""
        self._page_index = 0
        session = self.session
        if session is not None:
            probe, self._pending_preview_document = self._pending_preview_document, None
            if probe is not None and probe.path == session.path:
                self._preview_document = probe
            else:
                try:
                    self._preview_document = PdfDocument(session.path, password=session.password or None)
                except (PdfError, OSError, RuntimeError, ValueError) as exc:
                    QMessageBox.critical(self, "Cannot open file", str(exc))
                    self._preview_document = None
            # Load saved regions for this session
            self._load_regions_for_session(session)
        self._page_spin.blockSignals(True)
        self._page_spin.setRange(1, max(1, session.page_count if session else 1))
        self._page_spin.setValue(1)
        self._page_spin.blockSignals(False)
        self._page_total_label.setText(f"of {session.page_count if session else 0}")

        # Update gallery page filter
        self._gallery_page_filter.blockSignals(True)
        self._gallery_page_filter.clear()
        self._gallery_page_filter.addItem("All pages", None)
        if session:
            for p in range(session.page_count):
                # page_number is 1-based, so the filter value must be too
                self._gallery_page_filter.addItem(f"Page {p+1:02d}", p + 1)
        self._gallery_page_filter.blockSignals(False)


        self._show_current_page()
        # Regenerate thumbnails for all cards on the current page so restored
        # regions show crisp cropped previews instead of blank grey placeholders.
        session = self.session
        if session is not None:
            for card in session.cards_for_page(self._page_index):
                self._regenerate_thumbnail(card, immediate=True)
        self._refresh_gallery()
        self._update_state()
        # Update project active file/page
        self._update_project_active_file()

    def _update_project_active_file(self) -> None:
        """Update the project's active file and page in the database."""
        store = self._library_store
        session = self.session
        if store is None or session is None:
            return
        file_id = self._get_library_file_id(str(session.path))
        if file_id is not None:
            with contextlib.suppress(ProjectError):
                store.set_active_file_and_page(file_id, self._page_index + 1)

    def _close_preview_document(self) -> None:
        if self._preview_document is not None:
            try:
                self._preview_document.close()
            finally:
                self._preview_document = None

    def select_page(self, index: int) -> None:
        session = self.session
        if session is None or self._preview_document is None:
            return
        index = max(0, min(int(index), session.page_count - 1))
        if index == self._page_index:
            self._page_spin.blockSignals(True)
            self._page_spin.setValue(index + 1)
            self._page_spin.blockSignals(False)
            return
        self._page_index = index
        self._page_spin.blockSignals(True)
        self._page_spin.setValue(index + 1)
        self._page_spin.blockSignals(False)
        self._show_current_page()
        self._refresh_gallery()
        self._update_state()
        self._update_project_active_file()

    def _show_current_page(self) -> None:
        session = self.session
        if session is None or self._preview_document is None:
            self._page_image = None
            self._view.show_page(bgr_to_qimage(None))
            self._view.set_overlays([])
            return
        try:
            dpi = self._preview_document.effective_dpi(
                self._page_index, self.settings.preview_dpi, cap_to_native=self.settings.cap_dpi_to_native
            )
            self._page_image = self._preview_document.render_page(self._page_index, dpi=dpi)
            self._page_dpi = float(dpi)
        except (PdfError, OSError, RuntimeError, ValueError) as exc:
            self._page_image = None
            self._page_dpi = 0.0
            self._progress.set_message(f"Page render failed: {exc}")
        image = bgr_to_qimage(self._page_image)
        self._view.show_page(image)
        self._refresh_overlays()

    # ------------------------------------------------------------- card display
    def _visible_cards(self) -> list[DetectedCard]:
        session = self.session
        if session is None:
            return []
        # Return all cards; the gallery will filter based on its filter settings
        cards = list(session.cards)
        return sorted(
            cards,
            key=lambda card: (card.page_index, float(card.quad_norm[:, 1].mean()), float(card.quad_norm[:, 0].mean())),
        )

    def _refresh_overlays(self) -> None:
        session = self.session
        if session is None:
            self._view.set_overlays([])
            return
        specs: list[OverlaySpec] = []
        for position, card in enumerate(session.cards_for_page(self._page_index), start=1):
            color = self._type_color(card.doc_type)
            type_label = "Document" if card.doc_type is DocumentType.UNKNOWN else card.doc_type.display_name
            suffix = f"  {card.rotation}°" if card.rotation else ""
            if abs(card.fine_rotation) >= 0.05:
                suffix += f" +{card.fine_rotation:.1f}°"
            specs.append(
                OverlaySpec(
                    card_id=card.card_id,
                    quad_norm=card.effective_quad_norm(),
                    color=color,
                    label=f"#{position} {type_label}{suffix}",
                    excluded=not card.included,
                )
            )
        self._view.set_overlays(specs, self._selected_card_id)

    def _refresh_gallery(self) -> None:
        cards = self._visible_cards()
        if self._selected_card_id and all(card.card_id != self._selected_card_id for card in cards):
            self._selected_card_id = ""

        # Build gallery filter from UI controls
        from core.models import DocumentType

        type_filter = None
        type_data = self._gallery_type_filter.currentData()
        if type_data is not None:
            type_filter = DocumentType.from_value(type_data)

        # "This page" follows the page shown in the viewer; "All pages" either
        # shows everything or narrows to the page picked in the page combo.
        current_page_only = self._gallery_filter.currentData() == "page"
        self._gallery_page_filter.setEnabled(not current_page_only)
        page_filter = None
        if current_page_only and self.session is not None:
            page_filter = self._page_index + 1
        else:
            page_data = self._gallery_page_filter.currentData()
            if page_data is not None:
                page_filter = int(page_data)

        gallery_filter = GalleryFilter(
            text=self._gallery_search.text(),
            doc_type=type_filter,
            page=page_filter,
            included_only=self._gallery_included_only.isChecked(),
            orientation_confident_only=self._gallery_confident_only.isChecked(),
        )
        # One entry point: set_filter() + set_cards() rebuilt the list twice.
        self._gallery.set_cards(cards, self._selected_card_id, gallery_filter)

        self._gallery_count.setText(f"({self._gallery.count()})")
        self._sync_card_controls()

    def _schedule_gallery_refresh(self) -> None:
        """Refresh the gallery soon, at most once per burst of page updates.

        A detection run finishes one page after another; rebuilding the whole
        list (and its thumbnail cache) for every page made long documents janky.
        """
        if not self._gallery_timer.isActive():
            self._gallery_timer.start()


    def _sync_card_controls(self) -> None:
        card = self._selected_card() if self.session else None
        enabled = card is not None
        for widget in (self._btn_rot_left, self._btn_rot_right, self._btn_rot_180, self._btn_crop, self._btn_delete):
            widget.setEnabled(enabled)
        self._type_combo.setEnabled(enabled)
        self._include_check.setEnabled(enabled)
        self._type_combo.blockSignals(True)
        self._include_check.blockSignals(True)
        if card is not None:
            index = self._type_combo.findData(card.doc_type.value)
            if index >= 0:
                self._type_combo.setCurrentIndex(index)
            self._include_check.setChecked(card.included)
        self._type_combo.blockSignals(False)
        self._include_check.blockSignals(False)

    def _selected_card(self) -> DetectedCard | None:
        session = self.session
        if session is None or not self._selected_card_id:
            return None
        return session.find_card(self._selected_card_id)

    # ------------------------------------------------------------ document types
    def _populate_type_combo(self) -> None:
        """(Re)fill the gallery type drop-down, including user defined types."""
        current = self._type_combo.currentData()
        selected = self._selected_card()
        if selected is not None:
            current = selected.doc_type.value
        self._type_combo.blockSignals(True)
        self._type_combo.clear()
        for doc_type in DocumentType.choices():
            self._type_combo.addItem(doc_type.display_name, doc_type.value)
        if current:
            index = self._type_combo.findData(current)
            if index >= 0:
                self._type_combo.setCurrentIndex(index)
        self._type_combo.blockSignals(False)

        # Also populate the gallery type filter
        self._gallery_type_filter.blockSignals(True)
        self._gallery_type_filter.clear()
        self._gallery_type_filter.addItem("All types", None)
        for doc_type in DocumentType.choices():
            self._gallery_type_filter.addItem(doc_type.display_name, doc_type.value)
        self._gallery_type_filter.blockSignals(False)

    @staticmethod
    def _type_color(doc_type: DocumentType) -> str:
        """Stable colour for a card outline (built-in and custom types)."""
        known = TYPE_COLORS.get(doc_type.token)
        if known:
            return known
        if doc_type.builtin:
            return styles.TEXT_MUTED
        # Custom types: derived from the token via CRC32 so the colour is
        # stable across app restarts and does not shift when other custom
        # types are added or removed (a list index would reshuffle them).
        return styles.CARD_COLORS[zlib.crc32(doc_type.token.encode("utf-8")) % len(styles.CARD_COLORS)]

    def _on_card_selected(self, card_id: str) -> None:
        if not card_id:
            return
        self._selected_card_id = card_id
        card = self._selected_card()
        if card is not None and card.page_index != self._page_index:
            self.select_page(card.page_index)
        self._view.select_card(card_id)
        self._gallery.select_card(card_id)
        self._refresh_overlays()
        self._sync_card_controls()
        # Selecting a card is what enables the Edit/Tools menu entries, so the
        # menu has to follow the selection (a click in the gallery or on the
        # canvas does not go through any other state refresh).
        self._update_state()

    def _update_state(self) -> None:
        session = self.session
        pages = session.page_count if session else 0
        counts = session.counts() if session else {"detected": 0, "approved": 0}
        self._status_pages.setText(f"Pages: {pages}")
        self._status_detected.setText(f"Detected: {counts['detected']}")
        self._status_approved.setText(f"Approved: {counts['approved']}")

        has_session = session is not None
        busy = self._busy
        self._btn_open.setEnabled(not busy)
        self._btn_add.setEnabled(not busy)
        self._btn_add_folder.setEnabled(not busy)
        self._btn_detect.setEnabled(has_session and not busy)
        self._btn_redetect.setEnabled(has_session and not busy)
        self._btn_export.setEnabled(has_session and counts["approved"] > 0 and not busy)
        self._btn_prev.setEnabled(has_session and self._page_index > 0)
        self._btn_next.setEnabled(has_session and self._page_index < pages - 1)
        self._page_spin.setEnabled(has_session and pages > 0)
        self._document_combo.setEnabled(has_session and not busy and len(self._sessions) >= 1)
        self._btn_add_region.setEnabled(has_session and not busy)
        self._gallery_filter.setEnabled(has_session)
        self._sync_card_controls()
        self._update_menu_state()

        if not has_session:
            hint = "Open a scanned PDF or image to get started"
        elif busy:
            hint = "Working…"
        else:
            page_cards = len(session.cards_for_page(self._page_index))
            if page_cards == 0 and self._page_index not in session.detected_pages:
                hint = "No detection run on this page yet"
            elif page_cards == 0:
                hint = "No documents found on this page"
            else:
                hint = f"{page_cards} document(s) on this page"
        self._page_hint.setText(hint)

    # -------------------------------------------------------------- detection
    def start_detection(self, pages: list[int] | None = None) -> None:
        session = self.session
        if session is None or self._busy:
            return
        if self._preview_document is None:
            QMessageBox.warning(self, "No document", "Open a PDF or image first.")
            return
        # Persist current edits before detection overwrites them.  If the run
        # is cancelled, any cards added before cancellation are still saved.
        self._save_current_session_regions()
        if pages is None:
            # A page range typed in the page bar ("1,3,5-7") narrows the run.
            range_text = self._page_range_edit.text().strip()
            if range_text:
                pages = self._parse_page_range(range_text, session.page_count)
            else:
                pages = list(range(session.page_count))
        pages = [page for page in pages if 0 <= page < session.page_count]
        if not pages:
            return

        existing = [card for card in session.cards if card.page_index in pages]
        if existing:
            answer = QMessageBox.question(
                self,
                "Re-run detection",
                f"{len(existing)} detected document(s) on the selected page(s) will be replaced. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        for page_index in pages:
            session.detected_pages.discard(page_index)

        self._detection_errors: list[str] = []
        self._busy = True
        self._update_state()
        self._refresh_overlays()
        self._refresh_gallery()
        self._progress.start("Starting detection…")

        worker = DetectionWorker(session.path, pages, self.settings, password=session.password, parent=self)
        worker.page_started.connect(self._on_page_started)
        worker.progress.connect(self._on_detection_progress)

        worker.card_found.connect(self._on_card_found)
        worker.page_finished.connect(self._on_page_finished)
        worker.failed.connect(self._on_detection_failed)
        worker.finished_ok.connect(self._on_detection_finished)
        worker.cancelled.connect(self._on_detection_cancelled)
        # the reference must be cleared after the thread *really* returned
        # (finished_ok/cancelled arrive while the thread can still be running),
        # otherwise closeEvent would wait on nothing while the thread is alive
        worker.finished.connect(self._on_detection_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._detection_worker = worker
        worker.start()

    def _on_detection_progress(self, current: int, total: int, message: str) -> None:
        self._progress.set_progress(current, total, message)

    def _on_card_found(self, card: DetectedCard) -> None:
        session = self.session
        if session is not None:
            session.add_card(card)
            session.dirty = True
            self._mark_project_dirty()

    def _on_page_started(self, page_index: int) -> None:
        """Drop the old cards of a page once the worker really started on it.

        Clearing every page up front threw away good results when the file had
        moved, been renamed or needed a password: the worker opens the document
        before it emits ``page_started``, so this only happens on a real run.
        """
        session = self.session
        if session is None:
            return
        session.clear_page(page_index)
        self._schedule_gallery_refresh()

    def _parse_page_range(self, text: str, page_count: int) -> list[int]:
        """Parse ``"1,3,5-7"`` into zero-based page indices (bad parts ignored)."""
        from core.page_range import parse_page_range

        return parse_page_range(text, page_count)[0]

    def _on_page_finished(self, page_index: int, count: int) -> None:
        session = self.session
        if session is not None:
            session.detected_pages.add(page_index)
        if page_index == self._page_index:
            self._refresh_overlays()
        # Coalesced: a multi-page run finishes page after page and every page
        # would otherwise rebuild the whole gallery.
        self._schedule_gallery_refresh()
        self._update_state()

    def _on_detection_failed(self, message: str) -> None:
        self._detection_errors.append(message)
        self._progress.set_message(message.splitlines()[0][:110])

    def _on_detection_thread_finished(self) -> None:
        """Thread cleanup of the detection worker (fires after ``run()`` returned)."""
        self._detection_worker = None

    def _on_detection_finished(self, total: int) -> None:
        worker = self._detection_worker
        ocr_note = getattr(worker, "ocr_report", "") if worker is not None else ""
        self._busy = False
        session = self.session
        if session is not None:
            self._mark_library_status(session.path, STATUS_EXTRACTED)
            # Save detected regions to project database
            self._save_current_session_regions()
        message = f"Detected {total} document(s)" if total else "No documents detected"
        if ocr_note:
            message += f"  ·  OCR: {ocr_note}"
        self._progress.finish(message)
        self._refresh_overlays()
        self._refresh_gallery()
        self._update_state()
        self._refresh_dashboard()
        if self._detection_errors:
            QMessageBox.warning(self, "Detection finished with warnings", "\n".join(self._detection_errors[:8]))
        if total == 0 and not self._detection_errors:
            # A run that produced errors already reported them (e.g. the file
            # could not be opened) - the "nothing detected, do this" advice
            # would be misleading on top of it.
            QMessageBox.information(
                self,
                "No documents found",
                "No document cards were detected on the selected page(s).\n\n"
                "Tips:\n"
                "• Increase the detection sensitivity in Settings.\n"
                "• Use “Add Region” to draw a card manually.\n"
                "• Make sure the scan is not blank or extremely low resolution.",
            )

    def _on_detection_cancelled(self, total: int) -> None:
        self._busy = False
        # Persist any cards that were added before cancellation.
        session = self.session
        if session is not None:
            self._save_current_session_regions()
        self._progress.finish(f"Detection cancelled ({total} document(s) kept)")
        self._refresh_overlays()
        self._refresh_gallery()
        self._update_state()

    # ----------------------------------------------------------------- export
    def _ask_export_scope(self) -> str:
        """Ask whether to export the current file or every library file.

        Returns ``"current"``, ``"library"`` or ``""`` when the user cancelled.
        Uses ``QMessageBox.question`` (the pattern the rest of the window uses)
        so the choice stays testable and never blocks a headless run.
        """
        answer = QMessageBox.question(
            self,
            "Export documents",
            "Which files should be exported?\n\n"
            "• Yes — only the document shown in the canvas.\n"
            "• No — every file in the Document Library that has extracted "
            "regions (open or not).\n"
            "• Cancel — do not export.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            return "current"
        if answer == QMessageBox.StandardButton.No:
            return "library"
        return ""

    def _library_export_jobs(self) -> list[ExportJob]:
        """Export jobs for every library file that has extracted regions.

        Files that are open use their live in-memory cards (so unsaved edits to
        rotation/type/inclusion are honoured); the others are rebuilt from the
        regions stored in the project database.
        """
        store = self._library_store
        if store is None:
            return []
        open_sessions = {str(session.path): session for session in self._sessions}
        jobs: list[ExportJob] = []
        try:
            items = store.items()
        except ProjectError as exc:
            QMessageBox.critical(self, "Cannot read the library", str(exc))
            return []
        for item in items:
            if not item.exists:
                continue
            session = open_sessions.get(item.file_path)
            if session is not None:
                cards = session.included_cards()
                if cards:
                    jobs.append(
                        ExportJob(session.path, cards, password=session.password)
                    )
                continue
            try:
                regions = store.load_all_regions(item.id)
            except ProjectError:
                continue
            if not regions:
                continue
            cards = [
                card
                for card in self._cards_from_regions(regions, max(1, item.page_count))
                if card.included
            ]
            if cards:
                jobs.append(ExportJob(item.file_path, cards))
        return jobs

    def export_all(self) -> None:
        if self._busy:
            return
        scope = self._ask_export_scope()
        if not scope:
            return
        if scope == "current":
            session = self.session
            cards = session.included_cards() if session is not None else []
            jobs = (
                [ExportJob(session.path, cards, password=session.password)]
                if cards and session is not None
                else []
            )
        else:
            jobs = self._library_export_jobs()
        if not jobs:
            QMessageBox.information(
                self, "Nothing to export",
                "Detect and approve at least one document before exporting."
            )
            return
        options = self.settings.export_options()
        if not self._confirm_export_overwrite(options, jobs):
            return
        self._start_export(jobs, options)

    def export_card(self, card_id: str) -> None:
        if self._busy:
            return
        session = self.session
        if session is None:
            return
        card = session.find_card(card_id)
        if card is None:
            return
        if not card.included:
            answer = QMessageBox.question(
                self,
                "Document excluded",
                "This document is excluded from export. Export it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        folder = QFileDialog.getExistingDirectory(
            self, "Select the folder for this document", self.settings.output_dir or str(Path.home())
        )
        if not folder:
            return
        options = self.settings.export_options()
        options.output_dir = folder
        options.create_subfolder = False
        exportable = card.copy_card()
        exportable.included = True
        single_job = ExportJob(session.path, [exportable], password=session.password)
        if not self._confirm_export_overwrite(options, [single_job]):
            return
        # "Export it anyway?" must actually export: the job snapshot would
        # otherwise be filtered out again by included_cards.
        self._start_export([single_job], options)

    def _confirm_export_overwrite(
        self, options: ExportOptions, jobs: list[ExportJob] | None = None
    ) -> bool:
        """Ask what to do when the export folder already holds images.

        The worker thread cannot show dialogs, so the decision is made here and
        stored in ``options.overwrite_existing``:
        * ``True``  → existing files are replaced (same names).
        * ``False`` → a new file is created (``name_2.jpg``, ``name_3.jpg``, …).

        Returns ``False`` when the user cancelled the export.
        """
        extension = options.extension()
        base = Path(options.output_dir).expanduser()
        folders: list[Path] = []
        if base.is_dir():
            folders.append(base)
        if options.create_subfolder:
            # One sub folder per document is the default; those are the folders
            # that actually receive the images.
            for job in jobs or []:
                candidate = base / Path(job.pdf_path).stem
                if candidate.is_dir():
                    folders.append(candidate)
        existing = 0
        for folder in folders:
            try:
                existing += sum(
                    1
                    for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() == extension
                )
            except OSError:
                continue
        if not existing:
            options.overwrite_existing = False
            return True

        answer = QMessageBox.question(
            self,
            "Files already exist",
            f"The export folder already contains {existing} "
            f"{extension.lstrip('.').upper()} image(s).\n\n"
            "Overwrite them, or keep them and write the new images next to them "
            "with a numbered name?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            options.overwrite_existing = True
            return True
        if answer == QMessageBox.StandardButton.No:
            options.overwrite_existing = False
            return True
        return False

    def _start_export(self, jobs: list[ExportJob], options: ExportOptions) -> None:
        self._active_export_jobs = jobs
        self._export_errors = []
        self._busy = True
        self._update_state()
        self._progress.start("Exporting documents…")

        worker = ExportWorker(jobs, options, self)
        worker.progress.connect(self._on_export_progress)
        worker.failed.connect(self._on_export_failed)
        worker.finished_ok.connect(self._on_export_finished)
        worker.cancelled.connect(self._on_export_cancelled)
        worker.finished.connect(self._on_export_thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._export_worker = worker
        worker.start()

    def _on_export_thread_finished(self) -> None:
        """Thread cleanup of the export worker (fires after ``run()`` returned)."""
        self._export_worker = None

    def _on_export_progress(self, current: int, total: int, message: str) -> None:
        self._progress.set_progress(current, total, message)

    def _on_export_failed(self, message: str) -> None:
        self._export_errors.append(message)

    def _on_export_finished(self, summary: ExportSummary) -> None:
        self._finish_export(summary, cancelled=False)

    def _on_export_cancelled(self, summary: ExportSummary) -> None:
        self._finish_export(summary, cancelled=True)

    def _finish_export(self, summary: ExportSummary, cancelled: bool) -> None:
        self._busy = False
        self._update_state()
        exported = summary.success_count
        self._progress.finish(
            f"{'Export cancelled' if cancelled else 'Export finished'} · {exported} file(s) written"
        )

        jobs = getattr(self, "_active_export_jobs", [])
        entry = HistoryEntry(
            pdf_files=[str(job.pdf_path) for job in jobs],
            output_dir=str(summary.output_dir),
            document_count=exported,
            page_count=len(
                # Pages that actually contributed exported documents, not the
                # number of documents (which document_count already holds).
                # The file is part of the key: page 2 of two PDFs is two pages.
                {(str(job.pdf_path), card.page_index) for job in jobs for card in job.included_cards}
            ),
            files=[str(item.path) for item in summary.files if item.ok],
            errors=list(summary.errors) + list(getattr(self, "_export_errors", [])),
        )
        if exported == 0 and cancelled:
            # A cancelled run that wrote nothing is not a history event - it
            # would only add a row with no files to the log.
            self._progress.set_message("Export cancelled - nothing was written")
        elif not self.history.add(entry):
            self._progress.set_message("The history file could not be written")
        self._refresh_history_table()

        self._refresh_dashboard()

        if exported and self.settings.open_output_after_export:
            self._open_folder(summary.output_dir)

        message = f"{exported} document image(s) exported to:\n{summary.output_dir}"
        warnings = list(getattr(summary, "warnings", []))
        if warnings:
            message += "\n\nNote:\n" + "\n".join(f"• {item}" for item in warnings)
        if summary.failure_count:
            message += f"\n\n{summary.failure_count} file(s) failed."
            details = "\n".join(summary.errors[:6]) or "\n".join(getattr(self, "_export_errors", [])[:6])
            if details:
                message += f"\n\n{details}"
            QMessageBox.warning(self, "Export finished with errors", message)
        elif cancelled:
            QMessageBox.information(self, "Export cancelled", message)
        elif warnings:
            # the export worked, but a requested option was skipped - that must
            # not look like a clean run
            QMessageBox.warning(self, "Export finished with a note", message)
        else:
            QMessageBox.information(self, "Export finished", message)


    # -------------------------------------------------------- card operations
    def rotate_card(self, card_id: str, degrees: int) -> None:
        session = self.session
        card = session.find_card(card_id) if session else None
        if session is None:
            return
        if card is None:
            return
        card.rotation = (card.rotation + int(degrees)) % 360
        card.orientation_confident = True
        session.dirty = True
        self._mark_project_dirty()
        if card.thumbnail is not None:
            with contextlib.suppress(ValueError, cv2.error):
                card.thumbnail = rotate_image(card.thumbnail, int(degrees))
        self._selected_card_id = card_id
        self._gallery.update_card(card)
        self._refresh_overlays()
        self._sync_card_controls()
        self._update_state()
        self._save_current_session_regions()

    def _rotate_selected(self, degrees: int) -> None:
        if self._selected_card_id:
            self.rotate_card(self._selected_card_id, degrees)

    def set_card_type(self, card_id: str, doc_type) -> None:
        session = self.session
        card = session.find_card(card_id) if session else None
        if session is None:
            return
        if card is None:
            return
        card.set_type(doc_type, 1.0)
        session.dirty = True
        self._mark_project_dirty()
        self._selected_card_id = card_id
        self._gallery.update_card(card)
        self._refresh_overlays()
        self._sync_card_controls()
        # The type does not change the counts, but the menus mirror the card
        # state (Include in Export), so refresh them with the card controls.
        self._update_state()
        self._save_current_session_regions()

    def toggle_card_included(self, card_id: str) -> None:
        session = self.session
        card = session.find_card(card_id) if session else None
        if session is None:
            return
        if card is None:
            return
        card.included = not card.included
        session.dirty = True
        self._mark_project_dirty()
        self._selected_card_id = card_id
        self._gallery.update_card(card)
        self._refresh_overlays()
        self._sync_card_controls()
        self._update_state()
        self._save_current_session_regions()

    def remove_card(self, card_id: str) -> None:
        session = self.session
        if session is None:
            return
        if session.remove_card(card_id):
            session.dirty = True
            self._mark_project_dirty()
            if self._selected_card_id == card_id:
                self._selected_card_id = ""
            self._refresh_overlays()
            self._refresh_gallery()
            self._update_state()
            self._save_current_session_regions()

    def _delete_selected_card(self) -> None:
        if self._selected_card_id:
            self.remove_card(self._selected_card_id)

    def edit_crop(self, card_id: str) -> None:
        session = self.session
        card = session.find_card(card_id) if session else None
        if card is None or session is None:
            return
        dialog = CropEditorDialog(
            session.path,
            card,
            parent=self,
            password=session.password,
            render_dpi=self.settings.render_dpi,
        )
        if dialog.exec() != CropEditorDialog.DialogCode.Accepted:
            return
        edited = dialog.card
        card.quad_norm = edited.quad_norm
        card.page_size = edited.page_size or card.page_size
        card.rotation = edited.rotation
        card.fine_rotation = edited.fine_rotation
        card.orientation_confident = edited.orientation_confident
        card.doc_type = edited.doc_type
        card.type_confidence = edited.type_confidence
        card.identifier = edited.identifier
        card.included = edited.included
        session.dirty = True
        self._mark_project_dirty()
        self._selected_card_id = card.card_id
        self._regenerate_thumbnail(card, immediate=True)
        self._refresh_overlays()
        self._gallery.update_card(card)
        self._sync_card_controls()
        self._update_state()
        self._save_current_session_regions()

    def show_ocr_text(self, card_id: str) -> None:
        """Show the text the OCR engine found for one card (gallery context menu)."""
        session = self.session
        card = session.find_card(card_id) if session else None
        if card is None:
            return
        text = (card.ocr_text or "").strip()
        if not text:
            QMessageBox.information(
                self,
                "OCR text",
                "No text was recognised for this document.\n\n"
                "Enable “Enable local OCR” in the settings and run the detection again, "
                "or press Check next to the OCR engine to verify the engine works.",
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"OCR text · {card.doc_type.display_name}")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "Recognised text (used for the classification keywords and the identifier):"
        )
        box.setDetailedText(text)
        box.setInformativeText(text[:600] + ("…" if len(text) > 600 else ""))
        copy_button = box.addButton("Copy text", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy_button:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(text)
            self._progress.set_message("OCR text copied to the clipboard")

    def _regenerate_thumbnail(self, card: DetectedCard, immediate: bool = False) -> None:
        """Rebuild the thumbnail of *card* from the current page render.

        A rebuild warps the full-resolution page.  Continuous interactions
        (arrow-key nudges, mouse drags in the page view) call this on every
        step, so those rebuilds are coalesced to run ~30 ms after the last
        change; discrete actions pass ``immediate=True`` to get the new
        thumbnail right away.
        """
        if self._page_image is None or card.page_index != self._page_index:
            return
        if immediate:
            self._rebuild_thumbnail_now(card)
            self._gallery.update_card(card)
            return
        self._thumb_pending = card
        if not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def _flush_thumbnails(self) -> None:
        card = self._thumb_pending
        self._thumb_pending = None
        page_image = self._page_image
        if card is None or page_image is None or card.page_index != self._page_index:
            return
        self._rebuild_thumbnail_now(card, page_image)
        # repaint the gallery tile now that the thumbnail really changed
        self._gallery.update_card(card)

    def _rebuild_thumbnail_now(self, card: DetectedCard, page_image: np.ndarray) -> None:
        try:
            height, width = page_image.shape[:2]
            quad = card.effective_quad_pixels(width, height)
            crop = warp_quad(page_image, quad)
            if card.rotation:
                crop = rotate_image(crop, card.rotation)
            card.thumbnail = make_thumbnail(crop, int(self.settings.thumbnail_size))
        except (ValueError, cv2.error, IndexError):
            # If thumbnail generation fails (e.g., invalid quad, empty image), skip silently
            pass

    # --------------------------------------------------- add region (manual crop)
    def _normalise_region_preset(self) -> None:
        """Keep the preset token and the boolean flag consistent (also for old files)."""
        settings = self.settings
        token = str(getattr(settings, "manual_region_preset", "") or "")
        if token not in REGION_PRESET_TOKENS:
            # settings written before the token existed only had the boolean flag
            token = "custom" if settings.manual_region_fixed_size else "free"
        if token == "free" and settings.manual_region_fixed_size:
            token = "custom"
        settings.manual_region_preset = token
        settings.manual_region_fixed_size = token != "free"

    def _region_preset_index(self) -> int:
        """Index in REGION_PRESETS for the selected kind (custom stays custom)."""
        token = str(self.settings.manual_region_preset or "free")
        for index, (_label, preset_token, _size) in enumerate(REGION_PRESETS):
            if preset_token == token:
                return index
        return 0

    def _sync_region_controls(self) -> None:
        """Keep the toolbar combo and the settings widgets in sync."""
        settings = self.settings
        index = self._region_preset_index()
        if hasattr(self, "_region_preset_combo"):
            self._region_preset_combo.blockSignals(True)
            self._region_preset_combo.setCurrentIndex(index)
            self._region_preset_combo.blockSignals(False)
        if (
            self._set_region_preset is not None
            and self._set_region_fixed is not None
            and self._set_region_width is not None
            and self._set_region_height is not None
        ):
            self._set_region_preset.blockSignals(True)
            self._set_region_preset.setCurrentIndex(index)
            self._set_region_preset.blockSignals(False)
            self._set_region_fixed.blockSignals(True)
            self._set_region_fixed.setChecked(bool(settings.manual_region_fixed_size))
            self._set_region_fixed.blockSignals(False)
            self._set_region_width.blockSignals(True)
            self._set_region_width.setValue(float(settings.manual_region_width_mm))
            self._set_region_width.blockSignals(False)
            self._set_region_height.blockSignals(True)
            self._set_region_height.setValue(float(settings.manual_region_height_mm))
            self._set_region_height.blockSignals(False)
            enabled = bool(settings.manual_region_fixed_size)
            self._set_region_preset.setEnabled(True)
            self._set_region_width.setEnabled(enabled)
            self._set_region_height.setEnabled(enabled)
        if hasattr(self, "_region_size_button"):
            if settings.manual_region_fixed_size:
                self._region_size_button.setText(
                    f"{settings.manual_region_width_mm:.1f} x "
                    f"{settings.manual_region_height_mm:.1f} mm"
                )
                self._region_size_button.setToolTip(
                    "Change the size used by 'Add Region' (click to edit)"
                )
            else:
                self._region_size_button.setText("Size…")
                self._region_size_button.setToolTip(
                    "Give 'Add Region' a fixed physical size (e.g. ID card 85.6 x 54 mm)"
                )
        if hasattr(self, "_btn_add_region"):
            if settings.manual_region_fixed_size:
                self._btn_add_region.setToolTip(
                    "Draw a region; it is snapped to "
                    f"{settings.manual_region_width_mm:.1f} x {settings.manual_region_height_mm:.1f} mm "
                    "(Esc to cancel)"
                )
            else:
                self._btn_add_region.setToolTip(
                    "Draw a rectangle to add a document the detector missed (Esc to cancel)"
                )

    def _on_region_preset_changed(self, index: int) -> None:
        if index < 0 or index >= len(REGION_PRESETS):
            return
        _label, token, size = REGION_PRESETS[index]
        settings = self.settings
        if token == "custom":
            # ask for the dimensions instead of silently reusing a preset value.
            # The dialog is deferred so that the combo's "activated" signal (which
            # follows this one for the same user action) cannot open it twice.
            self._region_pending_dialog = index
            QTimer.singleShot(0, self._run_pending_region_dialog)
            return
        self._region_pending_dialog = -1
        if token == "free":
            settings.manual_region_preset = "free"
            settings.manual_region_fixed_size = False
        else:
            settings.manual_region_preset = token
            settings.manual_region_fixed_size = True
            settings.manual_region_width_mm, settings.manual_region_height_mm = size
        settings.save()
        self._sync_region_controls()

    def _run_pending_region_dialog(self) -> None:
        index = self._region_pending_dialog
        self._region_pending_dialog = -1
        if index < 0:
            return
        if not self._open_region_size_dialog():
            self._sync_region_controls()  # put the combo back where it was

    def _on_region_preset_activated(self, index: int) -> None:
        """The user picked an entry; re-picking the current one must still react."""
        if index < 0 or index >= len(REGION_PRESETS):
            return
        if self._region_pending_dialog == index:
            return  # this was a real change - the deferred call opens the dialog
        if REGION_PRESETS[index][1] != "custom":
            return  # plain presets need no dialog
        # "Custom…" was already the current entry: ask for the size again
        self._open_region_size_dialog()

    def _open_region_size_dialog(self) -> bool:
        """Ask for a custom width/height. Returns ``True`` when a size was applied."""
        settings = self.settings
        dialog = RegionSizeDialog(
            float(settings.manual_region_width_mm), float(settings.manual_region_height_mm), self
        )
        dialog.raise_()
        dialog.activateWindow()
        if not dialog.exec():
            return False
        settings.manual_region_width_mm, settings.manual_region_height_mm = dialog.size_mm()
        settings.manual_region_preset = "custom"
        settings.manual_region_fixed_size = True
        settings.save()
        self._sync_region_controls()
        return True

    def _on_region_fixed_toggled(self, checked: bool) -> None:
        settings = self.settings
        if bool(settings.manual_region_fixed_size) == bool(checked):
            return
        settings.manual_region_fixed_size = bool(checked)
        settings.manual_region_preset = "custom" if checked else "free"
        settings.save()
        self._sync_region_controls()

    def _on_region_dimensions_changed(self, _value: float) -> None:
        if self._set_region_width is None or self._set_region_height is None:
            return
        width = float(self._set_region_width.value())
        height = float(self._set_region_height.value())
        settings = self.settings
        if abs(width - settings.manual_region_width_mm) < 1e-6 and abs(
            height - settings.manual_region_height_mm
        ) < 1e-6:
            return
        settings.manual_region_width_mm = width
        settings.manual_region_height_mm = height
        if settings.manual_region_preset not in ("free",):
            # typed numbers no longer match the preset that was selected
            settings.manual_region_preset = "custom"
        settings.save()
        self._sync_region_controls()

    def _fixed_region_size_norm(self):
        """Size of the fixed region in normalised page units (or ``None``)."""
        settings = self.settings
        if not settings.manual_region_fixed_size or self._page_image is None:
            return None
        height_px, width_px = self._page_image.shape[:2]
        if not (height_px and width_px):
            return None
        dpi = float(self._page_dpi or 0.0) or 300.0
        px_per_mm = dpi / 25.4
        width_norm = (float(settings.manual_region_width_mm) * px_per_mm) / float(width_px)
        height_norm = (float(settings.manual_region_height_mm) * px_per_mm) / float(height_px)
        if not (0.0 < width_norm <= 1.0 and 0.0 < height_norm <= 1.0):
            return None
        return width_norm, height_norm

    # ------------------------------------------------------ manual corrections
    def _toggle_add_mode(self, checked: bool) -> None:
        self._view.set_mode(PdfPageView.MODE_ADD if checked else PdfPageView.MODE_SELECT)
        if checked:
            self._progress.set_message("Draw a rectangle around the document you want to add")
        # The toolbar button is the source of truth for the mode, so the
        # checkable View ▸ Add Region entry has to mirror it right away.
        self._update_menu_state()

    def _leave_add_mode(self) -> None:
        if self._btn_add_region.isChecked():
            self._btn_add_region.setChecked(False)
        self._view.set_mode(PdfPageView.MODE_SELECT)

    def _on_manual_rect(self, rect) -> None:
        self._leave_add_mode()
        session = self.session
        if session is None or rect is None:
            return

        centre_x = (rect.left() + rect.right()) / 2.0
        centre_y = (rect.top() + rect.bottom()) / 2.0
        fixed = self._fixed_region_size_norm()
        if fixed is not None:
            # snap to the configured physical size, keeping the drawn centre
            width_norm, height_norm = fixed
            x0 = min(max(centre_x - width_norm / 2.0, 0.0), 1.0 - width_norm)
            y0 = min(max(centre_y - height_norm / 2.0, 0.0), 1.0 - height_norm)
            quad = np.array(
                [
                    [x0, y0],
                    [x0 + width_norm, y0],
                    [x0 + width_norm, y0 + height_norm],
                    [x0, y0 + height_norm],
                ],
                dtype=np.float32,
            )
        else:
            quad = np.array(
                [
                    [rect.left(), rect.top()],
                    [rect.right(), rect.top()],
                    [rect.right(), rect.bottom()],
                    [rect.left(), rect.bottom()],
                ],
                dtype=np.float32,
            )
        page_size = (self._page_image.shape[1], self._page_image.shape[0]) if self._page_image is not None else (0, 0)
        card = DetectedCard(
            page_index=self._page_index,
            quad_norm=quad,
            confidence=1.0,
            source="manual",
            page_size=page_size,
        )
        card.set_type(DocumentType.UNKNOWN, 0.0)
        self._regenerate_thumbnail(card, immediate=True)
        session.add_card(card)
        session.dirty = True
        self._mark_project_dirty()
        self._selected_card_id = card.card_id
        self._refresh_overlays()
        self._refresh_gallery()
        self._gallery.select_card(card.card_id)
        self._sync_card_controls()
        self._update_state()
        self._save_current_session_regions()
        if fixed is not None:
            self._progress.set_message(
                "Region added at "
                f"{self.settings.manual_region_width_mm:.1f} x "
                f"{self.settings.manual_region_height_mm:.1f} mm"
            )

    def _on_quad_edited(self, card_id: str, quad_norm) -> None:
        session = self.session
        card = session.find_card(card_id) if session else None
        if session is None:
            return
        if card is None:
            return
        # the view always reports the *displayed* quad, so a fine rotation is
        # baked into the geometry at this point
        card.quad_norm = np.asarray(quad_norm, dtype=np.float32).reshape(4, 2)
        card.fine_rotation = 0.0
        session.dirty = True
        self._mark_project_dirty()
        self._selected_card_id = card_id
        self._regenerate_thumbnail(card)
        self._refresh_overlays()
        self._gallery.update_card(card)
        self._sync_card_controls()
        # Dragging a corner also selects the card, which enables the Edit menu;
        # the counts are unchanged so only the menu needs refreshing.
        self._update_menu_state()
        self._save_current_session_regions()

    def _on_type_combo_changed(self, index: int) -> None:
        card = self._selected_card()
        if card is None or index < 0:
            return
        raw = self._type_combo.itemData(index)
        if raw is None:
            return
        # Qt hands the DocumentType (a str enum) back as a plain string.
        self.set_card_type(card.card_id, DocumentType.from_value(raw))

    def _on_include_check_toggled(self, checked: bool) -> None:
        card = self._selected_card()
        if card is None or card.included == bool(checked):
            return
        self.toggle_card_included(card.card_id)

    # --------------------------------------------------------------- utilities
    def _cancel_current_task(self) -> None:
        if self._loading_batch:
            # Batch loading runs in the GUI thread; the loop checks this flag
            # between files (a single file cannot be interrupted).
            self._batch_cancel = True
            self._progress.set_message("Cancelling…")
        elif self._detection_worker is not None and self._detection_worker.isRunning():
            self._detection_worker.request_cancel()
            self._progress.set_message("Cancelling detection…")
        elif self._export_worker is not None and self._export_worker.isRunning():
            self._export_worker.request_cancel()
            self._progress.set_message("Cancelling export…")

    def _open_folder(self, folder) -> None:
        path = Path(folder)
        if not path.exists():
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except (OSError, FileNotFoundError, subprocess.SubprocessError):
            # Best effort - opening the folder is a convenience, not critical
            pass

    # --------------------------------------------------------- drag and drop
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._event_has_pdf(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if self._event_has_pdf(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._dropped_pdf_paths(event)
        if not paths:
            return
        event.acceptProposedAction()
        # The first file replaces the workspace only when nothing is open; the
        # rest are added.  _load_paths shows the progress while they load.
        if self._sessions:
            self._load_paths(paths, add=True, caption="Opening dropped files…")
        else:
            self._load_paths(
                paths, add=True, caption="Opening dropped files…", first_replaces=True
            )

    @staticmethod
    def _dropped_pdf_paths(event) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local and is_supported_file(local) and Path(local).is_file():
                paths.append(local)
        return paths

    def _event_has_pdf(self, event) -> bool:
        return bool(self._dropped_pdf_paths(event))

    # ----------------------------------------------------------------- closing
    def _wait_for_workers(self) -> None:
        """Wait for running workers to finish after a cancel was requested.

        Destroying the window while a QThread is still running crashes Qt
        ("QThread: Destroyed while thread is still running").  Cancel is
        honoured at page/card granularity inside the workers and the OCR read
        of a *single* card can take several seconds, so the wait must not be a
        short timeout.  The event loop is pumped in between so the UI stays
        responsive; after a generous deadline ``terminate()`` is used as the
        last resort - engine crashes would be worse than a torn export.
        """
        for worker in (self._detection_worker, self._export_worker):
            if worker is None or not worker.isRunning():
                continue
            deadline = time.monotonic() + 60.0
            while worker.isRunning() and time.monotonic() < deadline:
                worker.wait(100)
                QApplication.processEvents()
            if worker.isRunning():
                worker.terminate()
                if not worker.wait(2000):
                    worker.terminate()
                    worker.wait(3000)

    def closeEvent(self, event) -> None:
        if self._busy:
            answer = QMessageBox.question(
                self,
                "Still working",
                "A task is still running. Cancel it and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._cancel_current_task()
            self._wait_for_workers()
        # Save current session's regions before closing
        self._save_current_session_regions()
        if self._pending_preview_document is not None:
            self._pending_preview_document.close()
            self._pending_preview_document = None
        self._close_preview_document()
        if self._library_store is not None:
            self._library_store.close()
            self._library_store = None
            self._library_sidebar.set_store(None)
        self.settings.save()
        super().closeEvent(event)
