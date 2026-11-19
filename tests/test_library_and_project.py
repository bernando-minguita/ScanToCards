"""End-to-end UI + state tests for Document Library and Project workflows.

Covers:
- Add folder → remove → re-add folder cycle
- Switch between projects cleanly
- Switch between documents without region duplication
- Close project clears canvas and panels
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QFileDialog

from app.main_window import MainWindow
from core.database import STATUS_EXTRACTED
from core.models import DetectedCard, DocumentType


def _make_pdf(folder: Path, name: str, pages: int = 1) -> Path:
    """Create a small synthetic PDF in *folder*."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz as fitz

    path = folder / name
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=500, height=350)
        page.draw_rect(fitz.Rect(40, 40, 280, 200), color=(0, 0, 0), fill=(0.2, 0.3, 0.6))
    doc.save(str(path))
    doc.close()
    return path


def _add_cards_to_session(window, count: int = 2):
    """Add synthetic cards to the current session without running detection."""
    session = window.session
    assert session is not None
    for i in range(count):
        card = DetectedCard(
            page_index=0,
            quad_norm=np.array(
                [[0.1 + i * 0.1, 0.1], [0.5 + i * 0.1, 0.1], [0.5 + i * 0.1, 0.5], [0.1 + i * 0.1, 0.5]],
                dtype=np.float32,
            ),
            confidence=0.9,
            source="manual",
            doc_type=DocumentType.PASSPORT if i == 0 else DocumentType.DRIVING_LICENSE,
            rotation=90 if i == 0 else 0,
            included=True,
        )
        session.add_card(card)
    window._refresh_gallery()
    return session


# ---------------------------------------------------------------------------
# Library batch workflow
# ---------------------------------------------------------------------------
def test_add_folder_remove_and_readd_folder(main_window, tmp_path, monkeypatch):
    """Import a folder via Add Folder, remove all files, re-import same folder,
    assert all files reappear in the Document Library."""
    # Create a folder with 2 PDFs
    folder = tmp_path / "scans"
    folder.mkdir()
    _make_pdf(folder, "doc_a.pdf")
    _make_pdf(folder, "doc_b.pdf")

    # Stub the folder dialog to return our folder
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *a, **k: str(folder)),
    )

    # Add folder
    main_window.add_folder_dialog()
    store = main_window._library_store
    assert store is not None
    items = store.items()
    assert len(items) == 2, f"expected 2 files, found {len(items)}"
    names = {item.file_name for item in items}
    assert names == {"doc_a.pdf", "doc_b.pdf"}

    # Remove all files from library
    sidebar = main_window._library_sidebar
    sidebar._list.selectAll()
    sidebar.remove_selected()
    items = store.items()
    assert len(items) == 0, "library should be empty after removal"

    # Re-import the same folder
    main_window.add_folder_dialog()
    items = store.items()
    assert len(items) == 2, f"expected 2 files after re-add, found {len(items)}"
    names = {item.file_name for item in items}
    assert names == {"doc_a.pdf", "doc_b.pdf"}, "re-imported files should match originals"


def test_add_file_duplicate_not_added_twice(main_window, tmp_path, monkeypatch, sample_pdf):
    """Adding the same file path twice should not create duplicate library entries."""
    store = main_window._library_store
    assert store is not None

    # Add via _add_to_library directly (simulates load_pdf path)
    main_window._add_to_library(str(sample_pdf))
    count_after_first = len(store.items())
    assert count_after_first == 1

    # Add again - should be skipped as duplicate
    main_window._add_to_library(str(sample_pdf))
    count_after_second = len(store.items())
    assert count_after_second == 1, "duplicate file should not be added again"


# ---------------------------------------------------------------------------
# Project switching
# ---------------------------------------------------------------------------
def test_open_different_project_switches_cleanly(main_window, tmp_path, monkeypatch):
    """Open Project A, then open Project B; Project A files/regions must be unloaded."""
    from core.database import ProjectStore, DetectedRegion

    # Create Project A with one PDF
    proj_a = tmp_path / "project_a.dceproj"
    folder_a = tmp_path / "scans_a"
    folder_a.mkdir()
    pdf_a = _make_pdf(folder_a, "doc_a.pdf")

    # Create Project B with one different PDF
    proj_b = tmp_path / "project_b.dceproj"
    folder_b = tmp_path / "scans_b"
    folder_b.mkdir()
    pdf_b = _make_pdf(folder_b, "doc_b.pdf")

    # Save Project A with a detected card
    store_a = ProjectStore.create(proj_a, name="Project A")
    store_a.add_paths([str(pdf_a)])
    file_id_a = store_a.items()[0].id
    card = DetectedCard(
        page_index=0,
        quad_norm=np.array([[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], dtype=np.float32),
        confidence=0.9,
        doc_type=DocumentType.PASSPORT,
    )
    quad = card.quad_norm.tolist()
    regions = [
        DetectedRegion(
            id=0,
            file_id=file_id_a,
            page_num=1,
            region_index=1,
            doc_type="Passport",
            x=0.1, y=0.1, width=0.4, height=0.4,
            rotation_deg=0, confidence=0.9, orientation_ok=True, is_included=True,
            quad_json=json.dumps(quad), fine_rotation=0.0,
            file_path=str(pdf_a), canonical_path=str(pdf_a).lower(),
        )
    ]
    store_a.save_regions(file_id_a, 1, regions)
    store_a.close()

    # Save Project B with a different detected card
    store_b = ProjectStore.create(proj_b, name="Project B")
    store_b.add_paths([str(pdf_b)])
    file_id_b = store_b.items()[0].id
    card_b = DetectedCard(
        page_index=0,
        quad_norm=np.array([[0.2, 0.2], [0.6, 0.2], [0.6, 0.6], [0.2, 0.6]], dtype=np.float32),
        confidence=0.8,
        doc_type=DocumentType.DRIVING_LICENSE,
    )
    quad_b = card_b.quad_norm.tolist()
    regions_b = [
        DetectedRegion(
            id=0,
            file_id=file_id_b,
            page_num=1,
            region_index=1,
            doc_type="Driving Licence",
            x=0.2, y=0.2, width=0.4, height=0.4,
            rotation_deg=0, confidence=0.8, orientation_ok=True, is_included=True,
            quad_json=json.dumps(quad_b), fine_rotation=0.0,
            file_path=str(pdf_b), canonical_path=str(pdf_b).lower(),
        )
    ]
    store_b.save_regions(file_id_b, 1, regions_b)
    store_b.close()

    # Open Project A in the window
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(proj_a), "")),
    )
    main_window.open_project_dialog()
    assert main_window._library_store is not None
    items_a = main_window._library_store.items()
    assert len(items_a) == 1
    assert items_a[0].file_name == "doc_a.pdf"

    # Open Project B - should replace A completely
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(proj_b), "")),
    )
    main_window.open_project_dialog()
    items_b = main_window._library_store.items()
    assert len(items_b) == 1
    assert items_b[0].file_name == "doc_b.pdf", "Project B file should be loaded"

    # Verify Project A's data is gone (no stale sessions)
    assert main_window.session is not None
    assert main_window.session.path == pdf_b, "active session should be Project B's file"
    assert len(main_window._sessions) == 1, "only Project B's file should be in sessions"


# ---------------------------------------------------------------------------
# Document switching without duplication
# ---------------------------------------------------------------------------
def test_switch_documents_no_region_duplication(main_window, synthetic_pdf, synthetic_pdf_multi):
    """Load 2 PDFs with extracted cards, switch back and forth 4 times,
    assert extracted document count never multiplies."""
    # Load first PDF and add cards
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    session_a = main_window.session
    assert session_a is not None
    _add_cards_to_session(main_window, count=2)
    count_a_initial = len(session_a.cards)

    # Load second PDF
    assert main_window.load_pdf(str(synthetic_pdf_multi), add=True)
    session_b = main_window.session
    assert session_b is not None
    assert session_b.path == synthetic_pdf_multi
    _add_cards_to_session(main_window, count=3)
    count_b_initial = len(session_b.cards)

    # Verify document combo shows both
    combo = main_window._document_combo
    assert combo.count() == 2, f"expected 2 documents in combo, got {combo.count()}"

    # Switch back and forth 4 times via the document combo
    for switch in range(4):
        # Switch to session A (index 0)
        combo.setCurrentIndex(0)
        assert main_window.session is session_a
        assert len(session_a.cards) == count_a_initial, \
            f"session A card count changed after switch {switch}: {len(session_a.cards)}"

        # Switch to session B (index 1)
        combo.setCurrentIndex(1)
        assert main_window.session is session_b
        assert len(session_b.cards) == count_b_initial, \
            f"session B card count changed after switch {switch}: {len(session_b.cards)}"

    # Final counts must match initial
    assert len(session_a.cards) == count_a_initial
    assert len(session_b.cards) == count_b_initial


# ---------------------------------------------------------------------------
# Close project
# ---------------------------------------------------------------------------
def test_close_project_clears_canvas_and_panels(main_window, synthetic_pdf):
    """Open a populated project, trigger close_project(), assert canvas and panels reset."""
    # Load a PDF with cards
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    _add_cards_to_session(main_window, count=3)
    session = main_window.session
    assert session is not None
    assert len(session.cards) == 3
    assert main_window._page_image is not None

    # Close project
    main_window.close_project()

    # Library reset to 0 files
    store = main_window._library_store
    assert store is not None
    assert len(store.items()) == 0

    # No active session
    assert main_window.session is None
    assert main_window._session_index == -1
    assert main_window._sessions == []

    # Canvas image cleared
    assert main_window._page_image is None

    # Page controls reset
    assert main_window._page_spin.value() == 1
    assert main_window._page_spin.maximum() == 1
    assert "of 0" in main_window._page_total_label.text()

    # Gallery cleared
    assert main_window._gallery.count() == 0

    # Window title reset (no project name)
    from app.main_window import APP_NAME, APP_VERSION
    title = main_window.windowTitle()
    assert APP_NAME in title
    assert APP_VERSION in title
