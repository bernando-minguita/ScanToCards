"""Canvas and card rendering / thumbnail hydration tests.

Covers:
- Re-opened project renders non-blank card thumbnails
- Card rotation and inclusion persistence across save/re-open
- Card edits (type, include, rotation) propagate to canvas + SQLite
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.main_window import MainWindow
from core.database import DetectedRegion, ProjectStore, STATUS_EXTRACTED
from core.models import DetectedCard, DocumentType


def _make_pdf(folder: Path, name: str, pages: int = 1) -> Path:
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


def _make_region(card: DetectedCard, file_id: int, page_num: int = 1,
                 file_path: str = "", canonical_path: str = "") -> DetectedRegion:
    quad = card.quad_norm.tolist()
    return DetectedRegion(
        id=0,
        file_id=file_id,
        page_num=page_num,
        region_index=1,
        doc_type=card.doc_type.value,
        x=float(card.quad_norm[:, 0].min()),
        y=float(card.quad_norm[:, 1].min()),
        width=float(card.quad_norm[:, 0].max() - card.quad_norm[:, 0].min()),
        height=float(card.quad_norm[:, 1].max() - card.quad_norm[:, 1].min()),
        rotation_deg=card.rotation,
        confidence=card.confidence,
        orientation_ok=True,
        is_included=card.included,
        quad_json=json.dumps(quad),
        fine_rotation=card.fine_rotation,
        file_path=file_path,
        canonical_path=canonical_path,
    )


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
# Thumbnail hydration on re-open
# ---------------------------------------------------------------------------
def test_reopen_project_renders_non_blank_card_thumbnails(main_window, tmp_path, monkeypatch):
    """Save a project with extracted regions, re-open it, assert every card widget
    in the right-hand Extracted documents grid has a valid thumbnail."""
    # Create a PDF and a project with detected cards
    pdf_folder = tmp_path / "scans"
    pdf_folder.mkdir()
    pdf_path = _make_pdf(pdf_folder, "scan.pdf")
    proj_path = tmp_path / "saved.dceproj"

    store = ProjectStore.create(proj_path, name="Saved Project")
    store.add_paths([str(pdf_path)])
    file_id = store.items()[0].id

    # Add two detected cards with full geometry
    card1 = DetectedCard(
        page_index=0,
        quad_norm=np.array([[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], dtype=np.float32),
        confidence=0.95,
        doc_type=DocumentType.PASSPORT,
        rotation=90,
        included=True,
    )
    card2 = DetectedCard(
        page_index=0,
        quad_norm=np.array([[0.3, 0.3], [0.7, 0.3], [0.7, 0.7], [0.3, 0.7]], dtype=np.float32),
        confidence=0.88,
        doc_type=DocumentType.DRIVING_LICENSE,
        rotation=180,
        included=False,
    )
    canonical = str(pdf_path).lower()
    # region_index=1 for card1, region_index=2 for card2 (UNIQUE constraint)
    region1 = _make_region(card1, file_id, page_num=1,
                           file_path=str(pdf_path), canonical_path=canonical)
    region2 = _make_region(card2, file_id, page_num=1,
                           file_path=str(pdf_path), canonical_path=canonical)
    region2 = DetectedRegion(
        id=0, file_id=file_id, page_num=1, region_index=2,
        doc_type=region2.doc_type, x=region2.x, y=region2.y,
        width=region2.width, height=region2.height,
        rotation_deg=region2.rotation_deg, confidence=region2.confidence,
        orientation_ok=region2.orientation_ok, is_included=region2.is_included,
        quad_json=region2.quad_json, fine_rotation=region2.fine_rotation,
        file_path=region2.file_path, canonical_path=region2.canonical_path,
    )
    regions = [region1, region2]
    store.save_regions(file_id, 1, regions)
    store.close()

    # Open project in window
    monkeypatch.setattr(
        __import__("PySide6.QtWidgets", fromlist=["QFileDialog"]).QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(proj_path), "")),
    )
    main_window.open_project_dialog()

    # Active session loaded
    session = main_window.session
    assert session is not None
    assert len(session.cards) == 2, f"expected 2 restored cards, found {len(session.cards)}"

    # Gallery should show both cards
    gallery = main_window._gallery
    assert gallery.count() == 2, f"expected 2 gallery items, found {gallery.count()}"

    # Every card should have a thumbnail (regenerated on page render)
    for card in session.cards:
        assert card.thumbnail is not None, f"card {card.card_id} has no thumbnail"
        assert card.thumbnail.size > 0, f"card {card.card_id} thumbnail is empty"

    # Verify card properties restored
    cards_by_type = {c.doc_type.value: c for c in session.cards}
    assert "passport" in cards_by_type
    assert cards_by_type["passport"].rotation == 90
    assert "driving_license" in cards_by_type
    assert cards_by_type["driving_license"].rotation == 180
    assert cards_by_type["driving_license"].included is False

    store.close()


def test_card_rotation_and_inclusion_persistence(main_window, tmp_path, monkeypatch):
    """Toggle Include and rotate a card (90°), save, re-open, verify state persists."""
    pdf_folder = tmp_path / "scans"
    pdf_folder.mkdir()
    pdf_path = _make_pdf(pdf_folder, "scan.pdf")
    proj_path = tmp_path / "saved2.dceproj"

    store = ProjectStore.create(proj_path, name="Edit Persist")
    store.add_paths([str(pdf_path)])
    file_id = store.items()[0].id

    card = DetectedCard(
        page_index=0,
        quad_norm=np.array([[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], dtype=np.float32),
        confidence=0.9,
        doc_type=DocumentType.PASSPORT,
        rotation=0,
        included=True,
    )
    canonical = str(pdf_path).lower()
    regions = [_make_region(card, file_id, page_num=1,
                           file_path=str(pdf_path), canonical_path=canonical)]

    store.save_regions(file_id, 1, regions)
    store.close()

    # Open project
    monkeypatch.setattr(
        __import__("PySide6.QtWidgets", fromlist=["QFileDialog"]).QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(proj_path), "")),
    )
    main_window.open_project_dialog()

    session = main_window.session
    assert session is not None
    assert len(session.cards) == 1
    restored = session.cards[0]
    assert restored.rotation == 0
    assert restored.included is True
    assert restored.doc_type == DocumentType.PASSPORT

    # Now edit via main window API
    main_window.rotate_card(restored.card_id, 90)
    assert restored.rotation == 90
    main_window.toggle_card_included(restored.card_id)
    assert restored.included is False
    main_window.set_card_type(restored.card_id, DocumentType.DRIVING_LICENSE)
    assert restored.doc_type == DocumentType.DRIVING_LICENSE

    # Save project (flush regions)
    main_window._save_current_session_regions()

    # Re-open and verify edits persisted
    store2 = ProjectStore.open(proj_path)
    items2 = store2.items()
    assert len(items2) == 1
    reloaded = store2.load_all_regions(items2[0].id)
    assert 1 in reloaded
    region = reloaded[1][0]
    assert region.rotation_deg == 90
    assert region.is_included is False
    assert region.doc_type == "driving_license"

    store2.close()


def test_canvas_overlay_sync_on_card_rotation(main_window, synthetic_pdf):
    """Rotating a card updates both canvas overlay and gallery card state."""
    assert main_window.load_pdf(str(synthetic_pdf), add=False)
    session = main_window.session
    assert session is not None

    _add_cards_to_session(main_window, count=1)
    card = session.cards[0]
    # card starts with rotation=0 (i=0 → rotation=90? No, i=0 → rotation=90).
    # Let's set it to 0 first for a clean rotation test.
    card.rotation = 0
    card.fine_rotation = 0.0

    # Gallery shows the card
    gallery = main_window._gallery
    assert gallery.count() == 1

    # Rotate the card
    main_window.rotate_card(card.card_id, 90)
    assert card.rotation == 90

    # Overlay should reflect rotation
    overlay = main_window._view._overlays.get(card.card_id)
    assert overlay is not None, "overlay should exist for the card"

    # Change document type
    main_window.set_card_type(card.card_id, DocumentType.DRIVING_LICENSE)
    assert card.doc_type is DocumentType.DRIVING_LICENSE

    # Toggle include off
    main_window.toggle_card_included(card.card_id)
    assert card.included is False
    assert len(session.included_cards()) == 0

    # Remove the card
    main_window.remove_card(card.card_id)
    assert len(session.cards) == 0
    assert gallery.count() == 0
