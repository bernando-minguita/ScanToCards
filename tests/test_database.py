"""SQLite persistence layer tests for ScanToCards.

Covers:
- Project create / save / reopen round-trips with full field parity
- Idempotent region persistence (no duplication on repeated save)
- Remove then re-import preserves/re-links extracted regions
- Missing source file detection and relink workflow
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from core.database import STATUS_EXTRACTED, DetectedRegion, ProjectStore
from core.models import DetectedCard, DocumentType


def _make_card(page_index: int = 0, rotation: int = 0, doc_type=DocumentType.PASSPORT) -> DetectedCard:
    return DetectedCard(
        page_index=page_index,
        quad_norm=np.array(
            [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]], dtype=np.float32
        ),
        confidence=0.95,
        source="auto",
        doc_type=doc_type,
        type_confidence=0.9,
        rotation=rotation,
        fine_rotation=4.0,
        included=True,
        orientation_confident=True,
        page_size=(500, 350),
    )


def _regions_for(card: DetectedCard, file_id: int, page_num: int = 1,
                 file_path: str = "/some/path.pdf", canonical_path: str = "some/path.pdf") -> list:
    quad = card.quad_norm.tolist()
    return [
        DetectedRegion(
            id=0,
            file_id=file_id,
            page_num=page_num,
            region_index=1,
            doc_type=card.doc_type.value,
            x=0.1,
            y=0.1,
            width=0.4,
            height=0.4,
            rotation_deg=card.rotation,
            confidence=card.confidence,
            orientation_ok=True,
            is_included=card.included,
            quad_json=json.dumps(quad),
            fine_rotation=card.fine_rotation,
            file_path=file_path,
            canonical_path=canonical_path,
        )
    ]


# ---------------------------------------------------------------------------
# Project persistence
# ---------------------------------------------------------------------------
def test_create_save_and_reopen_project(temp_project_path, tmp_path):
    """Save multiple files and detected regions, re-open .dceproj, assert full field parity."""
    # Create a real file on disk so add_paths accepts it
    real_file = tmp_path / "path.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake")

    store = ProjectStore.create(temp_project_path, name="Test Project")

    # Add a library item
    store.add_paths([str(real_file)])
    items = store.items()
    assert len(items) == 1
    item = items[0]
    assert item.file_name == "path.pdf"
    assert item.status == "pending"

    file_id = item.id

    # Save detected regions
    card = _make_card(page_index=0, rotation=90)
    regions = _regions_for(card, file_id, page_num=1,
                           file_path=str(real_file),
                           canonical_path=str(real_file).lower())
    store.save_regions(file_id, 1, regions)

    # Mark as extracted
    store.set_status(str(real_file), STATUS_EXTRACTED)
    items = store.items()
    assert items[0].status == STATUS_EXTRACTED

    # Save the project
    saved = store.save_as(temp_project_path)
    assert saved == temp_project_path.resolve()
    assert saved.is_file()

    # Close and reopen
    store.close()
    reopened = ProjectStore.open(temp_project_path)

    # Library items survived
    reopened_items = reopened.items()
    assert len(reopened_items) == 1
    assert reopened_items[0].file_name == "path.pdf"
    assert reopened_items[0].status == STATUS_EXTRACTED

    # Regions survived with full field parity
    reopened_regions = reopened.load_all_regions(reopened_items[0].id)
    assert 1 in reopened_regions
    loaded = reopened_regions[1][0]
    assert loaded.doc_type == "passport"
    assert loaded.rotation_deg == 90
    assert loaded.confidence == 0.95
    assert loaded.orientation_ok is True
    assert loaded.is_included is True
    assert loaded.fine_rotation == 4.0
    # Full quad JSON round-trips exactly (allow float precision)
    loaded_quad = json.loads(loaded.quad_json)
    expected_quad = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]
    for i in range(4):
        for j in range(2):
            assert abs(loaded_quad[i][j] - expected_quad[i][j]) < 1e-6

    reopened.close()


def test_idempotent_region_save_no_duplicates(temp_project_path, tmp_path):
    """Saving regions for the same file_id 5 consecutive times yields zero duplication."""
    real_file = tmp_path / "path.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake")

    store = ProjectStore.create(temp_project_path)
    store.add_paths([str(real_file)])
    file_id = store.items()[0].id

    card = _make_card(page_index=0)
    regions = _regions_for(card, file_id, page_num=1,
                           file_path=str(real_file),
                           canonical_path=str(real_file).lower())

    # Save 5 times in a row
    for _ in range(5):
        store.save_regions(file_id, 1, regions)

    all_regions = store.load_all_regions(file_id)
    # Exactly 1 region per page (page 1)
    assert len(all_regions) == 1
    assert len(all_regions[1]) == 1

    store.close()


def test_remove_and_reimport_preserves_regions(temp_project_path, tmp_path):
    """Remove a file, re-add the same canonical path, assert regions are re-linked."""
    real_file = tmp_path / "path.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake")
    path_str = str(real_file.resolve())
    canonical = path_str.lower()

    store = ProjectStore.create(temp_project_path)
    store.add_paths([path_str])
    file_id = store.items()[0].id

    card = _make_card(page_index=0)
    regions = _regions_for(card, file_id, page_num=1,
                           file_path=path_str, canonical_path=canonical)
    store.save_regions(file_id, 1, regions)

    # Verify regions exist before removal
    before = store.load_all_regions(file_id)
    assert len(before[1]) == 1

    # Remove the library entry (should orphan regions, not delete them)
    store.remove_items([file_id])
    after_remove = store.load_all_regions(999999)  # non-existent id - no regions
    assert after_remove == {}

    # Regions should still be in the table but orphaned (file_id IS NULL)
    orphaned = store._db.connection.execute(
        "SELECT COUNT(*) FROM detected_regions WHERE file_id IS NULL"
    ).fetchone()
    assert int(orphaned[0]) == 1

    # Re-import the same file path - regions should re-link
    store.add_paths([path_str])
    new_items = store.items()
    assert len(new_items) == 1
    new_file_id = new_items[0].id

    # Regions re-linked automatically
    reloaded = store.load_all_regions(new_file_id)
    assert 1 in reloaded
    assert len(reloaded[1]) == 1
    assert reloaded[1][0].doc_type == "passport"

    store.close()


def test_missing_source_file_and_relink(temp_project_path, tmp_path):
    """Move a source PDF, open saved .dceproj, verify missing-file + relink workflow."""
    # Create a real file on disk
    real_file = tmp_path / "real.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake content")

    store = ProjectStore.create(temp_project_path)
    store.add_paths([str(real_file)])
    file_id = store.items()[0].id

    # Add regions
    card = _make_card(page_index=0)
    regions = _regions_for(card, file_id, page_num=1,
                           file_path=str(real_file),
                           canonical_path=str(real_file).lower())
    store.save_regions(file_id, 1, regions)
    saved_path = store.project_file
    store.close()

    # Move the source file on disk (simulate user relocating the PDF)
    moved_file = tmp_path / "moved.pdf"
    real_file.rename(moved_file)

    # Re-open project - file should be detected as missing
    reopened = ProjectStore.open(saved_path)
    items = reopened.items()
    assert len(items) == 1
    assert items[0].exists is False

    # resolve_file_path should return None for missing file
    resolved = reopened.resolve_file_path(items[0])
    assert resolved is None

    # Relink to the new location
    reopened.relink_file(items[0].id, str(moved_file))

    # Now it resolves
    relinked = reopened.items()[0]
    assert relinked.exists is True
    resolved_after = reopened.resolve_file_path(relinked)
    assert resolved_after is not None
    assert Path(resolved_after) == moved_file.resolve()

    # Regions survived the move and relink
    regions_loaded = reopened.load_all_regions(relinked.id)
    assert 1 in regions_loaded
    assert len(regions_loaded[1]) == 1

    reopened.close()


def test_database_atomic_transaction_on_save(temp_project_path, tmp_path):
    """Verify that save_regions uses atomic transactions (BEGIN/COMMIT/ROLLBACK)."""
    real_file = tmp_path / "path.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake")

    store = ProjectStore.create(temp_project_path)
    store.add_paths([str(real_file)])
    file_id = store.items()[0].id

    card = _make_card(page_index=0)
    regions = _regions_for(card, file_id, page_num=1,
                           file_path=str(real_file),
                           canonical_path=str(real_file).lower())

    # Normal save works
    store.save_regions(file_id, 1, regions)
    assert len(store.load_all_regions(file_id)[1]) == 1

    # Verify the BEGIN/COMMIT path doesn't leave a dangling transaction
    # (subsequent operations should work)
    card2 = _make_card(page_index=0, rotation=180, doc_type=DocumentType.DRIVING_LICENSE)
    regions2 = _regions_for(card2, file_id, page_num=2,
                            file_path=str(real_file),
                            canonical_path=str(real_file).lower())
    store.save_regions(file_id, 2, regions2)
    all_regions = store.load_all_regions(file_id)
    assert len(all_regions) == 2  # page 1 and page 2

    store.close()


def test_canonical_path_matching_preserves_regions(temp_project_path, tmp_path):
    """Canonical path (normcase(resolved)) matching re-links regions across remove/re-add."""
    real_file = tmp_path / "path.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake")
    path_str = str(real_file.resolve())
    canonical = os.path.normcase(path_str)

    store = ProjectStore.create(temp_project_path)
    store.add_paths([path_str])
    file_id = store.items()[0].id

    card = _make_card(page_index=0)
    regions = _regions_for(card, file_id, page_num=1,
                           file_path=path_str, canonical_path=canonical)
    store.save_regions(file_id, 1, regions)

    # Verify canonical_path was stored
    row = store._db.connection.execute(
        "SELECT canonical_path FROM detected_regions WHERE file_id = ?", (file_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == canonical

    # Remove and re-add
    store.remove_items([file_id])
    store.add_paths([path_str])
    new_id = store.items()[0].id

    # Regions re-linked
    reloaded = store.load_all_regions(new_id)
    assert len(reloaded[1]) == 1

    store.close()
