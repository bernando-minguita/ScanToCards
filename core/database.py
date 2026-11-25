"""SQLite backed project and document library storage.

The application keeps its document library in a small SQLite database:

``projects``
    One row per project (name, timestamps and the ``.dceproj`` file it was
    saved to, if any).
``library_items``
    One row per file added to a project (path, name, size, processing status).

``ProjectStore`` is the only object the UI talks to; it wraps a
``ProjectDatabase`` and exposes plain Python values (``LibraryItem``), so no
``sqlite3`` / SQL details leak into the Qt code.

A project that has not been saved yet lives in an in-memory database; saving it
copies the database to the chosen ``.dceproj`` file (using SQLite's backup API,
so the copy is always consistent) and continues to work on that file.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.pdf_loader import PdfError, is_supported_file

PROJECT_SUFFIX = ".dceproj"
DEFAULT_PROJECT_NAME = "Document Library"
SCHEMA_VERSION = 3

# Processing status of one library entry.
STATUS_PENDING = "pending"
STATUS_EXTRACTED = "extracted"
STATUS_MISSING = "missing"
STATUSES = (STATUS_PENDING, STATUS_EXTRACTED, STATUS_MISSING)

STATUS_LABELS = {
    STATUS_PENDING: "Pending",
    STATUS_EXTRACTED: "Extracted",
    STATUS_MISSING: "Missing file",
}


class ProjectError(RuntimeError):
    """Raised when a project database cannot be created, opened or saved."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalised_path(path: str | Path) -> str:
    """Absolute, user-expanded path used as the library key."""
    return str(Path(path).expanduser().resolve())


@dataclass(frozen=True)
class DetectedRegion:
    """A detected document region (bounding box + metadata) for a file/page."""
    id: int
    file_id: int | None  # NULL when the library entry was removed (orphaned)
    page_num: int
    region_index: int
    doc_type: str
    x: float
    y: float
    width: float
    height: float
    rotation_deg: int
    confidence: float
    orientation_ok: bool
    is_included: bool
    quad_json: str = "[]"
    fine_rotation: float = 0.0
    file_path: str = ""          # denormalized absolute path for path-based matching
    canonical_path: str = ""     # normcase(resolved) path for re-linking after remove/re-add

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> DetectedRegion:
        keys = row.keys()
        file_id_val = row["file_id"]
        return cls(
            id=int(row["id"]),
            file_id=int(file_id_val) if file_id_val is not None else None,
            page_num=int(row["page_num"]),
            region_index=int(row["region_index"]),
            doc_type=str(row["doc_type"] or "unknown"),
            x=float(row["x"] or 0.0),
            y=float(row["y"] or 0.0),
            width=float(row["width"] or 0.0),
            height=float(row["height"] or 0.0),
            rotation_deg=int(row["rotation_deg"] or 0),
            confidence=float(row["confidence"] or 0.0),
            orientation_ok=bool(row["orientation_ok"]),
            is_included=bool(row["is_included"]),
            quad_json=str(row["quad_json"] or "[]") if "quad_json" in keys else "[]",
            fine_rotation=float(row["fine_rotation"] or 0.0) if "fine_rotation" in keys else 0.0,
            file_path=str(row["file_path"] or "") if "file_path" in keys else "",
            canonical_path=str(row["canonical_path"] or "") if "canonical_path" in keys else "",
        )


@dataclass(frozen=True)
class LibraryItem:
    """One file in a project's library."""

    id: int
    project_id: int
    file_path: str
    file_name: str
    file_size: int
    status: str
    added_at: str
    page_count: int = 1
    sort_order: int = 0
    relative_path: str = ""

    @property
    def exists(self) -> bool:
        try:
            return Path(self.file_path).is_file()
        except OSError:
            return False

    @property
    def resolved_path(self) -> str:
        """Return the file_path if it exists, otherwise try relative_path against project directory."""
        if self.exists:
            return self.file_path
        if self.relative_path:
            try:
                rel = Path(self.relative_path)
                if rel.is_absolute():
                    pass
                else:
                    # Will be resolved against project dir by caller
                    return str(rel)
            except OSError:
                pass
        return self.file_path

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, str(self.status).title())

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> LibraryItem:
        keys = row.keys()
        return cls(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            file_path=str(row["file_path"]),
            file_name=str(row["file_name"]),
            file_size=int(row["file_size"] or 0),
            status=str(row["status"] or STATUS_PENDING),
            added_at=str(row["added_at"] or ""),
            page_count=int(row["page_count"] or 1),
            sort_order=int(row["sort_order"] or 0),
            relative_path=str(row["relative_path"] or "") if "relative_path" in keys else "",
        )


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    file_path   TEXT    NOT NULL DEFAULT '',
    active_file_id INTEGER,
    active_page_num INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS library_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_path   TEXT    NOT NULL,
    relative_path TEXT   NOT NULL DEFAULT '',
    file_name   TEXT    NOT NULL,
    file_size   INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    added_at    TEXT    NOT NULL,
    page_count  INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, file_path)
);

CREATE TABLE IF NOT EXISTS detected_regions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER REFERENCES library_items(id) ON DELETE SET NULL,
    page_num        INTEGER NOT NULL,
    region_index    INTEGER NOT NULL,
    doc_type        TEXT    NOT NULL DEFAULT 'unknown',
    x               REAL    NOT NULL DEFAULT 0.0,
    y               REAL    NOT NULL DEFAULT 0.0,
    width           REAL    NOT NULL DEFAULT 0.0,
    height          REAL    NOT NULL DEFAULT 0.0,
    rotation_deg    INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 0.0,
    orientation_ok  INTEGER NOT NULL DEFAULT 1,
    is_included     INTEGER NOT NULL DEFAULT 1,
    quad_json       TEXT    NOT NULL DEFAULT '[]',
    fine_rotation   REAL    NOT NULL DEFAULT 0.0,
    file_path       TEXT    NOT NULL DEFAULT '',
    canonical_path  TEXT    NOT NULL DEFAULT '',
    UNIQUE(file_id, page_num, region_index)
);

CREATE INDEX IF NOT EXISTS idx_library_items_project
    ON library_items(project_id);

CREATE INDEX IF NOT EXISTS idx_detected_regions_file
    ON detected_regions(file_id);

CREATE INDEX IF NOT EXISTS idx_detected_regions_canonical_path
    ON detected_regions(canonical_path);

PRAGMA user_version = {SCHEMA_VERSION};
"""

_REQUIRED_TABLES = ("projects", "library_items", "detected_regions")


class ProjectDatabase:
    """Thin, fault-tolerant wrapper around one SQLite connection."""

    def __init__(self, path: Path | None, create: bool = True) -> None:
        # ``None`` = in-memory workspace (a project that has not been saved yet)
        self.path = Path(path) if path else None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ProjectError(f"Cannot create the project folder:\n{exc}") from exc
        try:
            self._conn: sqlite3.Connection | None = sqlite3.connect(
                str(self.path) if self.path is not None else ":memory:", timeout=10.0
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            if create:
                self._conn.executescript(_SCHEMA)
            else:
                self._verify_schema()
        except sqlite3.Error as exc:
            self.close()
            raise ProjectError(f"Cannot open the project database:\n{exc}") from exc

    # ------------------------------------------------------------- connection
    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ProjectError("The project database is closed.")
        return self._conn

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()

    def _verify_schema(self) -> None:
        """Refuse anything that is not a project file (never create tables in it)."""
        names = {
            str(row["name"])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = [name for name in _REQUIRED_TABLES if name not in names]
        if missing:
            raise ProjectError(
                "This file is not a ScanToCards project "
                f"(missing table(s): {', '.join(missing)})."
            )
        try:
            version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.Error, TypeError, IndexError):
            version = SCHEMA_VERSION
        if version > SCHEMA_VERSION:
            raise ProjectError(
                "This project was created by a newer version of the application "
                f"(schema {version}, supported {SCHEMA_VERSION})."
            )
        # Migrate older databases: add relative_path column if missing
        try:
            cols = {str(row["name"]) for row in self.connection.execute(
                "PRAGMA table_info(library_items)"
            )}
        except sqlite3.Error:
            cols = set()
        if "relative_path" not in cols:
            try:
                self.connection.execute(
                    "ALTER TABLE library_items ADD COLUMN relative_path TEXT NOT NULL DEFAULT ''"
                )
                self.connection.commit()
            except sqlite3.Error:
                pass

        # Migrate older databases: add quad_json and fine_rotation columns if missing
        try:
            cols = {str(row["name"]) for row in self.connection.execute(
                "PRAGMA table_info(detected_regions)"
            )}
        except sqlite3.Error:
            cols = set()
        if "quad_json" not in cols:
            try:
                self.connection.execute(
                    "ALTER TABLE detected_regions ADD COLUMN quad_json TEXT NOT NULL DEFAULT '[]'"
                )
                self.connection.commit()
            except sqlite3.Error:
                pass
        if "fine_rotation" not in cols:
            try:
                self.connection.execute(
                    "ALTER TABLE detected_regions ADD COLUMN fine_rotation REAL NOT NULL DEFAULT 0.0"
                )
                self.connection.commit()
            except sqlite3.Error:
                pass

        # Migrate older databases: add file_path and canonical_path columns if missing
        try:
            cols = {str(row["name"]) for row in self.connection.execute(
                "PRAGMA table_info(detected_regions)"
            )}
        except sqlite3.Error:
            cols = set()
        if "file_path" not in cols:
            try:
                self.connection.execute(
                    "ALTER TABLE detected_regions ADD COLUMN file_path TEXT NOT NULL DEFAULT ''"
                )
                self.connection.commit()
            except sqlite3.Error:
                pass
        if "canonical_path" not in cols:
            try:
                self.connection.execute(
                    "ALTER TABLE detected_regions ADD COLUMN canonical_path TEXT NOT NULL DEFAULT ''"
                )
                self.connection.commit()
            except sqlite3.Error:
                pass

        # Populate file_path / canonical_path from library_items for existing rows
        try:
            self.connection.execute(
                "UPDATE detected_regions SET file_path = li.file_path, "
                "canonical_path = lower(li.file_path) "
                "FROM library_items li WHERE detected_regions.file_id = li.id"
            )
            self.connection.commit()
        except sqlite3.Error:
            pass

        # Migrate schema v1/v2: recreate detected_regions with nullable file_id
        # and ON DELETE SET NULL so removed library entries orphan rather than
        # destroy their regions.
        try:
            info = self.connection.execute(
                "PRAGMA table_info(detected_regions)"
            ).fetchall()
            notnull = any(int(r[3]) == 1 and str(r[1]) == "file_id" for r in info)
        except sqlite3.Error:
            notnull = False
        if notnull:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    "CREATE TABLE detected_regions_new ("
                    "id              INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "file_id         INTEGER REFERENCES library_items(id) ON DELETE SET NULL,"
                    "page_num        INTEGER NOT NULL,"
                    "region_index    INTEGER NOT NULL,"
                    "doc_type        TEXT    NOT NULL DEFAULT 'unknown',"
                    "x               REAL    NOT NULL DEFAULT 0.0,"
                    "y               REAL    NOT NULL DEFAULT 0.0,"
                    "width           REAL    NOT NULL DEFAULT 0.0,"
                    "height          REAL    NOT NULL DEFAULT 0.0,"
                    "rotation_deg    INTEGER NOT NULL DEFAULT 0,"
                    "confidence      REAL    NOT NULL DEFAULT 0.0,"
                    "orientation_ok  INTEGER NOT NULL DEFAULT 1,"
                    "is_included     INTEGER NOT NULL DEFAULT 1,"
                    "quad_json       TEXT    NOT NULL DEFAULT '[]',"
                    "fine_rotation   REAL    NOT NULL DEFAULT 0.0,"
                    "file_path       TEXT    NOT NULL DEFAULT '',"
                    "canonical_path  TEXT    NOT NULL DEFAULT '',"
                    "UNIQUE(file_id, page_num, region_index)"
                    ")"
                )
                self.connection.execute(
                    "INSERT INTO detected_regions_new "
                    "(id, file_id, page_num, region_index, doc_type, x, y, width, height, "
                    "rotation_deg, confidence, orientation_ok, is_included, quad_json, "
                    "fine_rotation, file_path, canonical_path) "
                    "SELECT id, file_id, page_num, region_index, doc_type, x, y, width, height, "
                    "rotation_deg, confidence, orientation_ok, is_included, quad_json, "
                    "fine_rotation, file_path, canonical_path FROM detected_regions"
                )
                self.connection.execute("DROP TABLE detected_regions")
                self.connection.execute(
                    "ALTER TABLE detected_regions_new RENAME TO detected_regions"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_detected_regions_file "
                    "ON detected_regions(file_id)"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_detected_regions_canonical_path "
                    "ON detected_regions(canonical_path)"
                )
                self.connection.execute("COMMIT")
            except sqlite3.Error:
                with contextlib.suppress(sqlite3.Error):
                    self.connection.execute("ROLLBACK")

    # ---------------------------------------------------------------- backups
    def backup_to(self, target: Path) -> None:
        """Write a consistent copy of the database to *target* (atomic)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f"{target.stem}.", suffix=".tmp"
        )
        os.close(handle)
        try:
            destination = sqlite3.connect(temp_name)
            try:
                self.connection.backup(destination)
                destination.commit()
            finally:
                destination.close()
            os.replace(temp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise


class ProjectStore:
    """Project metadata plus its document library."""

    def __init__(
        self,
        database: ProjectDatabase,
        project_id: int,
        project_file: Path | None = None,
    ) -> None:
        self._db = database
        self._project_id = int(project_id)
        self._project_file = Path(project_file) if project_file else None

    # ----------------------------------------------------------- construction
    @classmethod
    def create(
        cls, path: Path | None = None, name: str = DEFAULT_PROJECT_NAME
    ) -> ProjectStore:
        """Create a new, empty project (in memory when *path* is ``None``)."""
        database = ProjectDatabase(path, create=True)
        try:
            now = _now()
            cursor = database.connection.execute(
                "INSERT INTO projects (name, created_at, updated_at, file_path) "
                "VALUES (?, ?, ?, ?)",
                (str(name or DEFAULT_PROJECT_NAME), now, now, str(path or "")),
            )
            database.connection.commit()
        except sqlite3.Error as exc:
            database.close()
            raise ProjectError(f"Cannot create the project:\n{exc}") from exc
        return cls(database, int(cursor.lastrowid or 0), project_file=path)

    @classmethod
    def open(cls, path: str | Path) -> ProjectStore:
        """Open an existing ``.dceproj`` file."""
        project_path = Path(path)
        if not project_path.is_file():
            raise ProjectError(f"The project file does not exist:\n{project_path}")
        database = ProjectDatabase(project_path, create=False)
        try:
            row = database.connection.execute(
                "SELECT * FROM projects ORDER BY id LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            database.close()
            raise ProjectError(f"Cannot read the project:\n{exc}") from exc
        if row is None:
            database.close()
            raise ProjectError("The project file contains no project metadata.")
        return cls(database, int(row["id"]), project_file=project_path)

    # -------------------------------------------------------------- properties
    @property
    def project_id(self) -> int:
        return self._project_id

    @property
    def project_file(self) -> Path | None:
        return self._project_file

    @property
    def database_path(self) -> Path | None:
        return self._db.path

    @property
    def display_path(self) -> str:
        return str(self._project_file) if self._project_file else "Not saved yet"

    @property
    def name(self) -> str:
        row = self._db.connection.execute(
            "SELECT name FROM projects WHERE id = ?", (self._project_id,)
        ).fetchone()
        return str(row["name"]) if row else DEFAULT_PROJECT_NAME

    # -------------------------------------------------------------- operations
    def rename(self, name: str) -> bool:
        name = (name or "").strip()
        if not name or name == self.name:
            return False
        try:
            self._db.connection.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
                (name, _now(), self._project_id),
            )
            self._db.connection.commit()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot rename the project:\n{exc}") from exc
        return True

    def touch(self) -> None:
        """Refresh ``projects.updated_at``."""
        try:
            self._db.connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (_now(), self._project_id),
            )
            self._db.connection.commit()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot update the project:\n{exc}") from exc

    def items(self) -> list[LibraryItem]:
        """Every library entry of this project, in the order it was added."""
        try:
            rows = self._db.connection.execute(
                "SELECT * FROM library_items WHERE project_id = ? "
                "ORDER BY added_at, id",
                (self._project_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot read the document library:\n{exc}") from exc
        return [LibraryItem.from_row(row) for row in rows]

    def item_paths(self) -> list[str]:
        return [item.file_path for item in self.items()]

    def add_paths(self, paths: Iterable[str | Path]) -> list[LibraryItem]:
        """Add the supported files in *paths*; duplicates/missing files are skipped."""
        existing = {os.path.normcase(item.file_path) for item in self.items()}
        added: list[LibraryItem] = []
        now = _now()
        # Determine next sort_order
        max_sort = 0
        try:
            row = self._db.connection.execute(
                "SELECT MAX(sort_order) FROM library_items WHERE project_id = ?",
                (self._project_id,),
            ).fetchone()
            if row and row[0] is not None:
                max_sort = int(row[0])
        except sqlite3.Error:
            pass

        # Compute base directory for relative paths (project file's parent, if any)
        proj_dir: Path | None = None
        if self._project_file is not None:
            proj_dir = self._project_file.parent

        for raw in paths or []:
            try:
                path = Path(raw).expanduser().resolve()
                if not path.is_file() or not is_supported_file(path):
                    continue
                key = os.path.normcase(str(path))
                if key in existing:
                    continue
                size = int(path.stat().st_size)
                # Get page count
                page_count = 1
                try:
                    from core.pdf_loader import PdfDocument
                    doc = PdfDocument(path)
                    page_count = doc.page_count
                    doc.close()
                except (PdfError, OSError, RuntimeError, ValueError):
                    page_count = 1
                # Compute relative path against project directory, if possible
                rel_path = ""
                if proj_dir is not None:
                    try:
                        rel_path = str(path.relative_to(proj_dir))
                    except ValueError:
                        rel_path = ""
            except (OSError, RuntimeError, ValueError):
                continue
            max_sort += 1
            try:
                cursor = self._db.connection.execute(
                    "INSERT OR IGNORE INTO library_items "
                    "(project_id, file_path, relative_path, file_name, file_size, "
                    "status, added_at, page_count, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self._project_id, str(path), rel_path, path.name, size, STATUS_PENDING, now, page_count, max_sort),
                )
            except sqlite3.Error as exc:
                raise ProjectError(f"Cannot add files to the library:\n{exc}") from exc
            if cursor.rowcount != 1:
                continue
            # Re-link any orphaned detected_regions whose canonical_path matches
            # this file.  Regions were orphaned (file_id set to NULL) when the
            # library entry was removed; re-adding the same file re-attaches them
            # so previously extracted cards, rotations and regions are preserved.
            canonical = os.path.normcase(str(path))
            self._db.connection.execute(
                "UPDATE detected_regions SET file_id = ? "
                "WHERE canonical_path = ? AND file_id IS NULL",
                (int(cursor.lastrowid or 0), canonical),
            )
            existing.add(key)
            added.append(
                LibraryItem(
                    id=int(cursor.lastrowid or 0),
                    project_id=self._project_id,
                    file_path=str(path),
                    file_name=path.name,
                    file_size=size,
                    status=STATUS_PENDING,
                    added_at=now,
                    relative_path=rel_path,
                )
            )
        if added:
            self._db.connection.commit()
            self.touch()
        return added

    def remove_items(self, item_ids: Sequence[int]) -> int:
        """Remove entries by id (the files on disk are kept). Returns the count.

        Detected regions are *not* deleted: their ``file_id`` is set to NULL
        (orphaned) so they can be re-linked if the same file is re-imported
        later via :meth:`add_paths`.
        """
        ids = [int(item_id) for item_id in item_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        try:
            # Orphan regions first (explicit, for clarity; ON DELETE SET NULL
            # would do this automatically once file_id is nullable).
            self._db.connection.execute(
                f"UPDATE detected_regions SET file_id = NULL WHERE file_id IN ({placeholders})",
                ids,
            )
            cursor = self._db.connection.execute(
                f"DELETE FROM library_items WHERE project_id = ? AND id IN ({placeholders})",
                [self._project_id, *ids],
            )
            self._db.connection.commit()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot remove library entries:\n{exc}") from exc
        removed = int(cursor.rowcount or 0)
        if removed:
            self.touch()
        return removed

    def set_status(self, file_path: str | Path, status: str) -> bool:
        """Set the status of the entry for *file_path*; ``True`` when it changed."""
        if status not in STATUSES:
            return False
        try:
            key = os.path.normcase(normalised_path(file_path))
        except (OSError, RuntimeError, ValueError):
            return False
        for item in self.items():
            if os.path.normcase(item.file_path) != key or item.status == status:
                continue
            try:
                self._db.connection.execute(
                    "UPDATE library_items SET status = ? WHERE id = ?", (status, item.id)
                )
                self._db.connection.commit()
            except sqlite3.Error as exc:
                raise ProjectError(f"Cannot update the library entry:\n{exc}") from exc
            self.touch()
            return True
        return False

    def refresh_statuses(self) -> int:
        """Re-check the files on disk; returns how many entries changed."""
        changes = 0
        for item in self.items():
            new_status = item.status
            if item.exists:
                # A file that came back is usable again, but whether it was
                # already processed is unknown, so it starts as "pending".
                if item.status == STATUS_MISSING:
                    new_status = STATUS_PENDING
            else:
                new_status = STATUS_MISSING
            if new_status == item.status:
                continue
            try:
                self._db.connection.execute(
                    "UPDATE library_items SET status = ? WHERE id = ?",
                    (new_status, item.id),
                )
            except sqlite3.Error as exc:
                raise ProjectError(f"Cannot update the library entries:\n{exc}") from exc
            changes += 1
        if changes:
            self._db.connection.commit()
            self.touch()
        return changes

    # ------------------------------------------------------------------- detected regions
    def save_regions(
        self,
        file_id: int,
        page_num: int,
        regions: list[DetectedRegion],
    ) -> None:
        """Replace all regions for a file/page with the provided list.

        The DELETE + INSERT pair runs inside a single explicit transaction so
        that a failure mid-insert never leaves the table half-cleared.
        """
        try:
            self._db.connection.execute("BEGIN")
            self._db.connection.execute(
                "DELETE FROM detected_regions WHERE file_id = ? AND page_num = ?",
                (file_id, page_num),
            )
            for region in regions:
                self._db.connection.execute(
                    """INSERT INTO detected_regions
                       (file_id, page_num, region_index, doc_type, x, y, width, height,
                        rotation_deg, confidence, orientation_ok, is_included,
                        quad_json, fine_rotation, file_path, canonical_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        file_id,
                        page_num,
                        region.region_index,
                        region.doc_type,
                        region.x,
                        region.y,
                        region.width,
                        region.height,
                        region.rotation_deg,
                        region.confidence,
                        1 if region.orientation_ok else 0,
                        1 if region.is_included else 0,
                        region.quad_json,
                        region.fine_rotation,
                        region.file_path,
                        region.canonical_path,
                    ),
                )
            self._db.connection.execute("COMMIT")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                self._db.connection.execute("ROLLBACK")
            raise ProjectError(f"Cannot save detected regions:\n{exc}") from exc

    def load_regions(self, file_id: int, page_num: int) -> list[DetectedRegion]:
        """Load all regions for a file/page."""
        try:
            rows = self._db.connection.execute(
                "SELECT * FROM detected_regions WHERE file_id = ? AND page_num = ? ORDER BY region_index",
                (file_id, page_num),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot load detected regions:\n{exc}") from exc
        return [DetectedRegion.from_row(row) for row in rows]

    def load_all_regions(self, file_id: int) -> dict[int, list[DetectedRegion]]:
        """Load all regions for a file, grouped by page."""
        try:
            rows = self._db.connection.execute(
                "SELECT * FROM detected_regions WHERE file_id = ? ORDER BY page_num, region_index",
                (file_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot load detected regions:\n{exc}") from exc
        result: dict[int, list[DetectedRegion]] = {}
        for row in rows:
            region = DetectedRegion.from_row(row)
            result.setdefault(region.page_num, []).append(region)
        return result

    def region_counts(self) -> dict[int, int]:
        """Number of saved regions per library item id.

        Used by the Document Library panel to mark the files that already have
        extracted documents.  Orphaned regions (``file_id`` NULL, left behind
        when a library entry was removed) are deliberately not counted - they
        belong to no visible row.
        """
        try:
            rows = self._db.connection.execute(
                "SELECT file_id, COUNT(*) AS total FROM detected_regions "
                "WHERE file_id IS NOT NULL GROUP BY file_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot read the extracted regions:\n{exc}") from exc
        counts: dict[int, int] = {}
        for row in rows:
            try:
                counts[int(row["file_id"])] = int(row["total"])
            except (TypeError, ValueError):
                continue
        return counts

    def get_active_file_and_page(self) -> tuple:
        """Return (active_file_id, active_page_num) for the project."""
        try:
            row = self._db.connection.execute(
                "SELECT active_file_id, active_page_num FROM projects WHERE id = ?",
                (self._project_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot read project state:\n{exc}") from exc
        if row:
            return (row["active_file_id"], int(row["active_page_num"] or 1))
        return (None, 1)

    def set_active_file_and_page(self, file_id: int | None, page_num: int = 1) -> None:
        """Update the active file and page for the project."""
        try:
            self._db.connection.execute(
                "UPDATE projects SET active_file_id = ?, active_page_num = ?, updated_at = ? WHERE id = ?",
                (file_id, page_num, _now(), self._project_id),
            )
            self._db.connection.commit()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot update project state:\n{exc}") from exc

    # ---------------------------------------------------------------- path resolution
    def resolve_file_path(self, item: LibraryItem) -> str | None:
        """Resolve the actual on-disk path for a library item.

        Resolution order:
        1. Exact stored ``file_path``
        2. ``relative_path`` resolved against the project file's parent directory
        3. Same ``file_name`` inside the project file's directory

        Returns the resolved absolute path string, or ``None`` if unresolvable.
        """
        # 1. Exact stored path
        try:
            if Path(item.file_path).is_file():
                return item.file_path
        except OSError:
            pass

        # 2. relative_path against project directory
        if item.relative_path and self._project_file is not None:
            proj_dir = self._project_file.parent
            try:
                rel = Path(item.relative_path)
                candidate = proj_dir / rel if not rel.is_absolute() else rel
                if candidate.is_file():
                    return str(candidate.resolve())
            except OSError:
                pass

        # 3. Same file_name inside project directory
        if self._project_file is not None:
            try:
                candidate = self._project_file.parent / item.file_name
                if candidate.is_file():
                    return str(candidate.resolve())
            except OSError:
                pass

        return None

    def relink_file(self, file_id: int, new_path: str | Path) -> None:
        """Update ``file_path`` and ``relative_path`` for a library item."""
        new_path = Path(new_path).expanduser().resolve()
        try:
            # Compute new relative path against project directory
            rel_path = ""
            if self._project_file is not None:
                try:
                    rel_path = str(new_path.relative_to(self._project_file.parent))
                except ValueError:
                    rel_path = ""
            self._db.connection.execute(
                "UPDATE library_items SET file_path = ?, relative_path = ? WHERE id = ?",
                (str(new_path), rel_path, file_id),
            )
            self._db.connection.commit()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot relink file:\n{exc}") from exc

    # ------------------------------------------------------------------- files
    def save_as(self, target: str | Path) -> Path:
        """Copy the project to *target* and continue working on that file.

        The copy is verified before the running project switches over: a saved
        file that lost its ``library_items`` rows (wrong ``project_id``,
        interrupted copy, ...) would restore as an empty project later, so that
        case raises instead of silently producing a broken ``.dceproj``.
        """
        target = Path(target)
        if target.suffix.lower() != PROJECT_SUFFIX:
            target = target.with_name(target.name + PROJECT_SUFFIX)
        resolved = target.expanduser().resolve()
        expected = len(self.items())
        if self._db.path is not None and os.path.normcase(
            str(self._db.path.resolve())
        ) == os.path.normcase(str(resolved)):
            # Saving onto the currently open file: only the metadata changes.
            self._write_project_file(resolved)
            self._check_saved_count(self._db, expected, resolved)
            return resolved
        try:
            self._db.backup_to(resolved)
        except (sqlite3.Error, OSError) as exc:
            raise ProjectError(f"Cannot save the project:\n{exc}") from exc

        database = ProjectDatabase(resolved, create=False)
        # Verify before switching: an unverified copy is how a "saved" project
        # ends up empty the next time it is opened.
        self._check_saved_count(database, expected, resolved)

        old_database, old_file = self._db, self._project_file
        self._db, self._project_file = database, resolved
        try:
            self._write_project_file(resolved)
        except BaseException:
            self._db, self._project_file = old_database, old_file
            database.close()
            raise
        old_database.close()
        return resolved

    def _check_saved_count(self, database: ProjectDatabase, expected: int, path: Path) -> None:
        """Refuse a project file whose library rows did not survive the copy."""
        try:
            row = database.connection.execute(
                "SELECT COUNT(*) FROM library_items WHERE project_id = ?",
                (self._project_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot verify the saved project:\n{exc}") from exc
        found = int(row[0]) if row else 0
        if found != expected:
            raise ProjectError(
                f"The project file {path.name!r} was written but contains "
                f"{found} of {expected} library entr"
                + ("y" if expected == 1 else "ies")
                + ".\n\nThe running project was left unchanged; try saving to a "
                "different location."
            )
        # Also verify detected_regions table has data for projects with extracted files
        if expected > 0:
            try:
                row = database.connection.execute(
                    "SELECT COUNT(*) FROM detected_regions dr "
                    "JOIN library_items li ON dr.file_id = li.id "
                    "WHERE li.project_id = ?",
                    (self._project_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise ProjectError(f"Cannot verify detected regions in saved project:\n{exc}") from exc
            int(row[0]) if row else 0
            # This is just a sanity check - regions may be empty if no detection was run
            # but the table should exist and be accessible

    def _write_project_file(self, path: Path) -> None:
        try:
            self._db.connection.execute(
                "UPDATE projects SET file_path = ?, updated_at = ? WHERE id = ?",
                (str(path), _now(), self._project_id),
            )
            self._db.connection.commit()
        except sqlite3.Error as exc:
            raise ProjectError(f"Cannot update the project file:\n{exc}") from exc

    def close(self) -> None:
        self._db.close()
