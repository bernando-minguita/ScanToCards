"""Shared pytest fixtures for ScanToCards test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Qt must run headless in the test environment.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SAMPLE_PDF = PROJECT_ROOT / "sample.pdf"


# ---------------------------------------------------------------------------
# Document fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def sample_pdf() -> Path:
    """The bundled sample scan (skipped if not present)."""
    if not SAMPLE_PDF.exists():
        pytest.skip("sample scan sample.pdf is not available")
    return SAMPLE_PDF


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory) -> Path:
    """A small generated 2-page PDF (no sample.pdf needed, works on any machine)."""
    try:
        import pymupdf as _fitz
    except ImportError:  # pragma: no cover - older PyMuPDF releases
        import fitz as _fitz

    path = tmp_path_factory.mktemp("docs") / "synthetic.pdf"
    document = _fitz.open()
    for _ in range(2):
        page = document.new_page(width=500, height=350)
        page.draw_rect(_fitz.Rect(40, 40, 280, 200), color=(0, 0, 0), fill=(0.15, 0.2, 0.55))
        page.draw_rect(_fitz.Rect(300, 40, 460, 200), color=(0, 0, 0), fill=(0.55, 0.2, 0.2))
    document.save(str(path))
    document.close()
    return path


@pytest.fixture(scope="session")
def synthetic_pdf_multi(tmp_path_factory) -> Path:
    """A generated 3-page PDF for multi-page workflows."""
    try:
        import pymupdf as _fitz
    except ImportError:
        import fitz as _fitz

    path = tmp_path_factory.mktemp("docs") / "multi.pdf"
    document = _fitz.open()
    for i in range(3):
        page = document.new_page(width=600, height=400)
        page.draw_rect(_fitz.Rect(50 + i * 20, 50, 300 + i * 20, 200), color=(0, 0, 0), fill=(0.2, 0.3, 0.6))
    document.save(str(path))
    document.close()
    return path


@pytest.fixture(scope="session")
def sample_image(tmp_path_factory) -> Path:
    """A generated PNG image file."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover
        pytest.skip("Pillow not available")
    path = tmp_path_factory.mktemp("docs") / "photo.png"
    image = Image.new("RGB", (1200, 900), (248, 248, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 150, 520, 420], fill=(200, 208, 220), outline=(110, 120, 135), width=4)
    image.save(path, dpi=(300, 300))
    return path


# ---------------------------------------------------------------------------
# Project database fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def temp_project_path(tmp_path) -> Path:
    """A temporary .dceproj path for save/open round-trip tests."""
    return tmp_path / "test_project.dceproj"


# ---------------------------------------------------------------------------
# Qt application and main window fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for all GUI tests."""
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def main_window(qapp, tmp_path, monkeypatch):
    """A MainWindow whose dialogs never block and whose state stays in tmp_path.

    The window is closed after each test to release Qt handles.
    """
    from PySide6.QtWidgets import QMessageBox

    from app.history import HistoryStore
    from app.main_window import MainWindow
    from app.settings import AppSettings

    # Stub all modal dialogs so tests never hang waiting for user input.
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    settings = AppSettings()
    settings.preview_dpi = 80
    settings.render_dpi = 200
    settings.output_dir = str(tmp_path / "out")
    settings.open_output_after_export = False
    settings.create_subfolder_per_pdf = True
    settings.save = lambda: True  # never touch the real user profile

    main = MainWindow(settings)
    main.history = HistoryStore(tmp_path / "history.json")
    yield main
    main.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def pump_until(predicate, timeout: float = 180.0) -> bool:
    """Run the Qt event loop until *predicate* is true (or the timeout passes)."""
    from PySide6.QtWidgets import QApplication
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def flush_events(rounds: int = 10, delay: float = 0.01) -> None:
    """Let deferred (zero-timer) work, modal dialogs and repaints complete."""
    from PySide6.QtWidgets import QApplication
    import time

    for _ in range(rounds):
        QApplication.processEvents()
        time.sleep(delay)
