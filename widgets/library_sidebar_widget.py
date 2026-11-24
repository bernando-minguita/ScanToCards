"""Compact document library widget for the left sidebar."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.icons import icon
from core.database import (
    STATUS_MISSING,
    LibraryItem,
    ProjectError,
    ProjectStore,
)


class LibrarySidebarWidget(QWidget):
    """Compact project file list for the sidebar."""

    #: A file was double-clicked (or "Open File" was chosen).
    fileActivated = Signal(str)
    #: A file needs to be relinked (user clicked "Relink File...").
    relinkRequested = Signal(str)
    #: A short status line for the main window's status bar.
    message = Signal(str)

    def __init__(self, store: ProjectStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LibrarySidebarPanel")
        self._store = store

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header with project name and file count
        header = QHBoxLayout()
        header.setSpacing(6)
        self._project_label = QLabel("No project open")
        self._project_label.setObjectName("SectionTitle")
        self._project_label.setWordWrap(True)
        self._count_label = QLabel("")
        self._count_label.setObjectName("Faint")
        header.addWidget(self._project_label, 1)
        header.addWidget(self._count_label)
        layout.addLayout(header)

        # Compact action buttons (icon-only, tooltip)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self._btn_refresh = QPushButton()
        self._btn_refresh.setIcon(icon("refresh"))
        self._btn_refresh.setToolTip("Re-check whether the library files still exist")
        self._btn_refresh.setFixedSize(28, 28)
        self._btn_refresh.clicked.connect(self.refresh_statuses)
        self._btn_remove = QPushButton()
        self._btn_remove.setIcon(icon("trash"))
        self._btn_remove.setToolTip("Remove selected file(s) from the project (files kept on disk)")
        self._btn_remove.setFixedSize(28, 28)
        self._btn_remove.clicked.connect(self.remove_selected)
        for btn in (self._btn_refresh, self._btn_remove):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("IconButton")
        actions.addWidget(self._btn_refresh)
        actions.addWidget(self._btn_remove)
        actions.addStretch(1)
        layout.addLayout(actions)

        # Search box - filters the list by file name
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search files…")
        self._search.setClearButtonEnabled(True)
        self._search.setToolTip("Filter the library by file name")
        self._search.textChanged.connect(lambda _text: self.refresh())
        layout.addWidget(self._search)

        # File list
        self._list = QListWidget()
        self._list.setObjectName("LibrarySidebarList")
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemDoubleClicked.connect(self._on_item_activated)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self._list, 1)

        # Empty state hint
        self._empty_label = QLabel("No files yet — use “Add File” or “Add Folder” in the toolbar.")
        self._empty_label.setObjectName("Faint")
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()
        layout.addWidget(self._empty_label)

        self.refresh()

    # ------------------------------------------------------------------ store
    def set_store(self, store: ProjectStore | None) -> None:
        self._store = store
        self.refresh()

    @property
    def store(self) -> ProjectStore | None:
        return self._store

    # ------------------------------------------------------------------- view
    def refresh(self) -> None:
        """Rebuild the list from the store, keeping the current selection."""
        selected = set(self._selected_ids())
        self._list.clear()
        store = self._store
        if store is None:
            self._project_label.setText("No project open")
            self._count_label.setText("")
            self._empty_label.hide()
            self._update_buttons()
            return
        try:
            items = store.items()
        except ProjectError as exc:
            self._project_label.setText("Library unavailable")
            self._count_label.setText("")
            self._empty_label.setText(f"Could not read the project: {str(exc).splitlines()[0]}")
            self._empty_label.show()
            self.message.emit(str(exc).splitlines()[0][:110])
            self._update_buttons()
            return

        self._project_label.setText(store.name)
        self._project_label.setToolTip(store.display_path)
        self.setToolTip(store.display_path)

        try:
            region_counts = store.region_counts()
        except ProjectError:
            region_counts = {}

        query = self._search.text().strip().lower() if hasattr(self, "_search") else ""
        visible = [item for item in items if self._matches(item, query)]

        if query:
            self._count_label.setText(f"{len(visible)} of {len(items)} file" + ("" if len(items) == 1 else "s"))
        else:
            self._count_label.setText(f"{len(items)} file" + ("" if len(items) == 1 else "s"))

        if not visible:
            if query:
                self._empty_label.setText(f"No files match “{self._search.text().strip()}”.")
            else:
                self._empty_label.setText(
                    "No files yet — use “Add File” or “Add Folder” in the toolbar."
                )
            self._empty_label.show()
        else:
            self._empty_label.hide()
            for item in visible:
                self._list.addItem(
                    self._make_item(item, region_counts.get(item.id, 0))
                )

        if selected:
            for index in range(self._list.count()):
                item = self._list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) in selected:
                    item.setSelected(True)
        self._update_buttons()

    @staticmethod
    def _matches(item: LibraryItem, query: str) -> bool:
        """Case-insensitive file name (or full path) match for the search box."""
        if not query:
            return True
        return query in item.file_name.lower() or query in item.file_path.lower()

    def _make_item(self, item: LibraryItem, region_count: int = 0) -> QListWidgetItem:
        page_text = f" · {item.page_count} pg" if item.page_count > 1 else ""
        display_name = item.file_name
        # Truncate long filenames
        if len(display_name) > 35:
            display_name = display_name[:32] + "…"
        # A check mark marks files that already have extracted documents.
        marker = " ✓" if region_count else ""
        text = f"{display_name}{page_text}{marker}"
        lwi = QListWidgetItem(text)
        lwi.setData(Qt.ItemDataRole.UserRole, item.id)
        lwi.setData(Qt.ItemDataRole.UserRole + 1, item.file_path)
        lwi.setData(Qt.ItemDataRole.UserRole + 2, region_count)
        missing = item.status == STATUS_MISSING or not item.exists
        # Show warning icon for missing files, a check for extracted ones
        if missing:
            icon_name = "warning"
        elif region_count:
            icon_name = "check"
        else:
            icon_name = "document"
        lwi.setIcon(icon(icon_name))
        details = ""
        if region_count:
            details = (
                f"\n\n{region_count} extracted region"
                + ("" if region_count == 1 else "s")
                + " saved for this file."
            )
        lwi.setToolTip(
            f"{item.file_path}\n"
            f"{item.file_size:,} bytes\n"
            f"Added {item.added_at}"
            + details
            + (
                "\n\nThe file is missing on disk. Use “Relink File…” to reconnect it."
                if missing
                else ""
            )
        )
        return lwi

    def _selected_ids(self) -> list[int]:
        ids: list[int] = []
        for item in self._list.selectedItems():
            value = item.data(Qt.ItemDataRole.UserRole)
            if value is not None:
                ids.append(int(value))
        return ids

    def _path_of(self, item: QListWidgetItem) -> str:
        return str(item.data(Qt.ItemDataRole.UserRole + 1) or "")

    def _update_buttons(self) -> None:
        has_store = self._store is not None
        has_selection = bool(self._list.selectedItems())
        self._btn_refresh.setEnabled(has_store)
        self._btn_remove.setEnabled(has_store and has_selection)

    # ---------------------------------------------------------------- actions
    def _start_dir(self) -> str:
        for item in self._list.selectedItems():
            path = Path(self._path_of(item))
            if path.parent.is_dir():
                return str(path.parent)
        items = []
        try:
            items = self._store.items() if self._store is not None else []
        except ProjectError:
            items = []
        for item in items:
            parent = Path(item.file_path).parent
            if parent.is_dir():
                return str(parent)
        return str(Path.home())

    def remove_selected(self) -> None:
        store = self._store
        if store is None:
            return
        rows = self._list.selectedItems()
        if not rows:
            return
        names = [row.text().split(" · ")[0] for row in rows]
        preview = names[0] if len(names) == 1 else f"{len(names)} files"
        answer = QMessageBox.question(
            self,
            "Remove from library",
            f"Remove {preview} from the project?\n\nThe file(s) themselves are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = store.remove_items(self._selected_ids())
        except ProjectError as exc:
            QMessageBox.critical(self, "Document library", str(exc))
            return
        self.refresh()
        if removed:
            self.message.emit(f"Removed {removed} file(s) from the library")

    def refresh_statuses(self) -> None:
        store = self._store
        if store is None:
            return
        try:
            changes = store.refresh_statuses()
        except ProjectError as exc:
            QMessageBox.critical(self, "Document library", str(exc))
            return
        self.refresh()
        if changes:
            self.message.emit(f"{changes} library entr" + ("y changed status" if changes == 1 else "ies changed status"))
        else:
            self.message.emit("Library statuses are up to date")

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        path = self._path_of(item)
        if path:
            self.fileActivated.emit(path)

    # ------------------------------------------------------------ context menu
    def _show_context_menu(self, position) -> None:
        if not self._list.selectedItems():
            return
        menu = QMenu(self)
        open_action = menu.addAction(icon("document"), "Open File")
        selected_item = self._list.selectedItems()[0]
        missing = not self._path_of(selected_item) or not Path(self._path_of(selected_item)).is_file()
        if missing:
            relink_action = menu.addAction(icon("refresh"), "Relink File…")
            menu.addSeparator()
            remove_action = menu.addAction(icon("trash"), "Remove from Library")
            refresh_action = menu.addAction(icon("refresh"), "Refresh Status")
        else:
            menu.addSeparator()
            remove_action = menu.addAction(icon("trash"), "Remove from Library")
            refresh_action = menu.addAction(icon("refresh"), "Refresh Status")
            relink_action = None
        chosen = menu.exec(self._list.viewport().mapToGlobal(position))
        if chosen is open_action:
            self._on_item_activated(selected_item)
        elif chosen is relink_action:
            self.relinkRequested.emit(self._path_of(selected_item))
        elif chosen is remove_action:
            self.remove_selected()
        elif chosen is refresh_action:
            self.refresh_statuses()