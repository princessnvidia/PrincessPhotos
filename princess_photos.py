#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import (
    QObject,
    Qt,
    QThread,
    QTimer,
    QRect,
    QSize,
    QPoint,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
    QDesktopServices,
    QIcon,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QAbstractScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
    QDialog,
)

from PIL import Image, ImageOps, ImageDraw, ImageFont

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

try:
    import pyvips  # type: ignore
    HAS_PYVIPS = True
except Exception:
    pyvips = None
    HAS_PYVIPS = False


APP_NAME = "Princess Photos"
APP_VERSION = "2.5"
APP_ID = "princess-photos"

DEFAULT_PHOTOS_DIR = Path.home() / "Documents" / "Photos"
DATA_DIR = Path.home() / ".local" / "share" / "PrincessPhotos"
CACHE_DIR = Path.home() / ".cache" / "PrincessPhotos"
THUMB_DIR = CACHE_DIR / "thumbs_v23"
PREVIEW_DIR = CACHE_DIR / "previews_v23"
DB_PATH = DATA_DIR / "princess_photos.sqlite"
APP_ICON_PATH = Path.home() / "Applications" / "PrincessPhotos" / "princess-photos.svg"

# V2.6 : recherche OCR corrigée.
# OCR complet sur toutes les images, y compris captures/PNG sans date originale.
# En recherche, les images sans date originale ne sont plus masquées.
# Les .MOV de Live Photos restent associés à leur image et ne sont pas affichés en doublon.
DISPLAY_ONLY_ORIGINAL_DATES = True

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".webp", ".tif", ".tiff", ".gif", ".bmp", ".avif"
}

VIDEO_EXTENSIONS = {
    ".mov", ".mp4", ".m4v"
}

IGNORED_EXTENSIONS = {
    ".aae", ".xmp", ".json", ".xml", ".txt", ".db", ".ini"
}

# Important : on ne met PAS FileModifyDate ici.
# FileModifyDate = souvent date d'export, donc mauvais ordre.
DATE_PRIORITY = [
    "DateTimeOriginal",
    "SubSecDateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "ContentCreateDate",
    "CreationDate",
    "DateCreated",
]

DEFAULT_TILE_SIZE = 132
MIN_TILE_SIZE = 84
MAX_TILE_SIZE = 260
TILE_GAP = 3
PRELOAD_ROWS = 5
THUMB_WORKERS = max(2, min(6, (os.cpu_count() or 4) // 2))
OCR_VERSION = "v26_all_images_fra_eng_psm6_norm2"


@dataclass(frozen=True)
class PhotoRow:
    path: str
    filename: str
    media_type: str
    original_dt: Optional[str]
    fallback_dt: str
    has_original_date: int
    date_source: str
    live_video: Optional[str]
    signature: str

    @property
    def display_date(self) -> str:
        return self.original_dt or self.fallback_dt


# -----------------------------------------------------------------------------
# Paths / database
# -----------------------------------------------------------------------------

def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def db_connect() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-64000")
    init_db(con)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            path TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            filename TEXT NOT NULL,
            ext TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'image',
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            signature TEXT NOT NULL,
            original_dt TEXT,
            fallback_dt TEXT NOT NULL,
            has_original_date INTEGER NOT NULL DEFAULT 0,
            date_source TEXT NOT NULL DEFAULT 'unknown',
            live_video TEXT,
            ocr_text TEXT,
            ocr_norm TEXT,
            ocr_signature TEXT,
            ocr_version TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Migration douce depuis les versions <= 1.8 : les anciennes bases n'avaient pas media_type.
    columns = {row[1] for row in con.execute("PRAGMA table_info(photos)").fetchall()}
    if "media_type" not in columns:
        con.execute("ALTER TABLE photos ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'")
    if "ocr_norm" not in columns:
        con.execute("ALTER TABLE photos ADD COLUMN ocr_norm TEXT")
    if "ocr_version" not in columns:
        con.execute("ALTER TABLE photos ADD COLUMN ocr_version TEXT")
    con.execute("UPDATE photos SET media_type='image' WHERE media_type IS NULL OR media_type=''")

    con.execute("CREATE INDEX IF NOT EXISTS idx_photos_root ON photos(root)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_photos_media ON photos(root, media_type)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(has_original_date, original_dt, fallback_dt)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_photos_ocr ON photos(ocr_text)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_photos_ocr_norm ON photos(ocr_norm)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    con.commit()


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO app_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def get_setting(con: sqlite3.Connection, key: str, default: str) -> str:
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def file_signature(path: Path) -> tuple[int, int, str]:
    st = path.stat()
    size = int(st.st_size)
    mtime_ns = int(st.st_mtime_ns)
    return size, mtime_ns, f"{size}:{mtime_ns}"


def media_key(path: Path) -> str:
    return str(path.with_suffix("")).lower()


def thumb_path_for(path: str, signature: str, tile_size: int) -> Path:
    raw = f"{path}|{signature}|thumb|{tile_size}|v23_square_video_frames_duration_badge"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return THUMB_DIR / f"{digest}.jpg"


def preview_path_for(path: str, signature: str, max_side: int = 2200) -> Path:
    raw = f"{path}|{signature}|preview|{max_side}|v23_video_frames_duration_badge"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return PREVIEW_DIR / f"{digest}.jpg"


# -----------------------------------------------------------------------------
# Metadata dates
# -----------------------------------------------------------------------------

def parse_metadata_date(value) -> Optional[datetime]:
    if not value:
        return None

    text = str(value).strip()

    # ExifTool: 2022:04:13 17:22:10+02:00 / 2022:04:13 17:22:10
    match = re.search(
        r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        text,
    )
    if match:
        y, mo, d, h, mi, s = map(int, match.groups())
        try:
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            return None

    # ISO fallback
    try:
        text_iso = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text_iso)
        return parsed.replace(tzinfo=None)
    except Exception:
        return None


def best_original_date(metadata: dict) -> tuple[Optional[datetime], str]:
    for tag in DATE_PRIORITY:
        dt = parse_metadata_date(metadata.get(tag))
        if dt:
            return dt, tag
    return None, "NoOriginalDate"


def run_exiftool(paths: list[Path]) -> dict[str, dict]:
    exiftool = shutil.which("exiftool")
    if not exiftool or not paths:
        return {}

    out: dict[str, dict] = {}
    tags = [f"-{tag}" for tag in DATE_PRIORITY]
    chunk_size = 160

    for start in range(0, len(paths), chunk_size):
        chunk = paths[start:start + chunk_size]
        cmd = [
            exiftool,
            "-j",
            "-api", "QuickTimeUTC=1",
            *tags,
            *[str(p) for p in chunk],
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            data = json.loads(result.stdout or "[]")
            for item in data:
                source = item.get("SourceFile")
                if source:
                    out[str(Path(source))] = item
        except Exception:
            continue
    return out


def xmp_sidecar_candidates(path: Path) -> list[Path]:
    candidates = [
        path.with_suffix(".xmp"),
        path.with_suffix(".XMP"),
        Path(str(path) + ".xmp"),
        Path(str(path) + ".XMP"),
    ]
    unique: list[Path] = []
    seen = set()
    for c in candidates:
        if c not in seen and c.exists() and c.is_file():
            unique.append(c)
            seen.add(c)
    return unique


# -----------------------------------------------------------------------------
# Image creation helpers
# -----------------------------------------------------------------------------

def make_placeholder(path: str, out_path: Path, size: int) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (size, size), (37, 37, 43))
    draw = ImageDraw.Draw(img)
    ext = Path(path).suffix.upper().replace(".", "") or "IMG"
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(13, size // 7))
    except Exception:
        font = ImageFont.load_default()
    draw.text((size / 2, size / 2), ext[:8], anchor="mm", fill=(220, 214, 230), font=font)
    img.save(out_path, quality=85)
    return out_path


def video_duration_seconds(path: str) -> Optional[float]:
    """Retourne la durée d'une vidéo via ffprobe, ou None si indisponible."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        if not value or value.upper() == "N/A":
            return None
        seconds = float(value)
        if seconds <= 0:
            return None
        return seconds
    except Exception:
        return None


def format_video_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "VID"

    total = max(0, int(round(seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60

    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def draw_video_badge(img: Image.Image, path: str) -> Image.Image:
    """V2.3 : dessine uniquement la durée en haut à gauche, sans icône play."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    duration_text = format_video_duration(video_duration_seconds(path))

    overlay = img.convert("RGBA")
    draw = ImageDraw.Draw(overlay)

    w, h = overlay.size
    font_size = max(11, min(20, w // 8))
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    try:
        bbox = draw.textbbox((0, 0), duration_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w = len(duration_text) * font_size // 2
        text_h = font_size

    pad_x = max(5, w // 35)
    pad_y = max(3, w // 55)
    margin = max(4, w // 45)
    radius = max(5, w // 35)

    x0 = margin
    y0 = margin
    x1 = x0 + text_w + pad_x * 2
    y1 = y0 + text_h + pad_y * 2

    # Fond sombre semi-transparent, discret comme Apple Photos.
    draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=(0, 0, 0, 150))
    draw.text((x0 + pad_x, y0 + pad_y - 1), duration_text, fill=(255, 255, 255, 245), font=font)

    return overlay.convert("RGB")


def make_video_placeholder(path: str, out_path: Path, size: int) -> Path:
    """Fallback si ffmpeg est absent ou si la vidéo est illisible."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (size, size), (22, 22, 28))
    img = draw_video_badge(img, path)
    img.save(out_path, quality=85)
    return out_path


def make_video_thumbnail_ffmpeg(path: str, out_path: Path, size: int) -> Path:
    """Extrait une vraie frame vidéo avec ffmpeg, puis ajoute la durée en haut à gauche."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg unavailable")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Écriture dans un fichier temporaire pour éviter les miniatures corrompues si ffmpeg échoue.
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    vf = (
        f"scale={size}:{size}:force_original_aspect_ratio=increase,"
        f"crop={size}:{size}"
    )

    # On tente d'abord à 1s pour éviter les frames noires, puis au tout début si la vidéo est courte.
    attempts = ["1", "0.05", "0"]
    last_error = None

    for ss in attempts:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", ss,
            "-i", path,
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "3",
            str(tmp_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=25,
                check=False,
            )
            if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                with Image.open(tmp_path) as img:
                    img = ImageOps.exif_transpose(img).convert("RGB")
                    img = ImageOps.fit(
                        img,
                        (size, size),
                        method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5),
                    )
                    img = draw_video_badge(img, path)
                    img.save(out_path, quality=84, optimize=True)
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
                return out_path
            last_error = result.stderr
        except Exception as e:
            last_error = str(e)

    try:
        tmp_path.unlink()
    except Exception:
        pass

    raise RuntimeError(f"ffmpeg thumbnail failed: {last_error}")


def make_square_thumbnail_pillow(path: str, out_path: Path, size: int) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = ImageOps.fit(
            img,
            (size, size),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (27, 27, 32))
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        else:
            img = img.convert("RGB")
        img.save(out_path, quality=82, optimize=True)
    return out_path


def make_square_thumbnail_vips(path: str, out_path: Path, size: int) -> Path:
    # libvips est beaucoup plus rapide si le support HEIC/JPEG est dispo.
    # Si ça échoue, on repasse automatiquement sur Pillow.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not HAS_PYVIPS or pyvips is None:
        raise RuntimeError("pyvips unavailable")
    image = pyvips.Image.thumbnail(
        path,
        size,
        height=size,
        crop="centre",
        auto_rotate=True,
    )
    if image.hasalpha():
        image = image.flatten(background=[27, 27, 32])
    image.jpegsave(str(out_path), Q=82, optimize_coding=True)
    return out_path


def ensure_thumbnail(path: str, signature: str, size: int) -> str:
    out_path = thumb_path_for(path, signature, size)
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)

    if Path(path).suffix.lower() in VIDEO_EXTENSIONS:
        try:
            return str(make_video_thumbnail_ffmpeg(path, out_path, size))
        except Exception:
            return str(make_video_placeholder(path, out_path, size))

    try:
        return str(make_square_thumbnail_vips(path, out_path, size))
    except Exception:
        try:
            return str(make_square_thumbnail_pillow(path, out_path, size))
        except Exception:
            return str(make_placeholder(path, out_path, size))


def make_preview(path: str, signature: str, max_side: int = 2200) -> str:
    out_path = preview_path_for(path, signature, max_side)
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)

    if Path(path).suffix.lower() in VIDEO_EXTENSIONS:
        preview_size = min(900, max_side)
        try:
            return str(make_video_thumbnail_ffmpeg(path, out_path, preview_size))
        except Exception:
            return str(make_video_placeholder(path, out_path, preview_size))

    # V2.0 : preview plus rapide. On tente libvips d'abord, puis Pillow.
    if HAS_PYVIPS and pyvips is not None:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            image = pyvips.Image.thumbnail(
                path,
                max_side,
                height=max_side,
                size="down",
                auto_rotate=True,
            )
            if image.hasalpha():
                image = image.flatten(background=[20, 20, 24])
            image.jpegsave(str(out_path), Q=90, optimize_coding=True)
            return str(out_path)
        except Exception:
            pass

    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, (20, 20, 24))
                bg.paste(img, mask=img.getchannel("A"))
                img = bg
            else:
                img = img.convert("RGB")
            img.save(out_path, quality=90, optimize=True)
            return str(out_path)
    except Exception:
        return str(make_placeholder(path, out_path, 900))


# -----------------------------------------------------------------------------
# Library loading and scanning
# -----------------------------------------------------------------------------

ORDER_SQL = """
ORDER BY
    CASE WHEN has_original_date = 1 THEN 0 ELSE 1 END ASC,
    CASE WHEN has_original_date = 1 THEN original_dt ELSE fallback_dt END ASC,
    filename COLLATE NOCASE ASC,
    path COLLATE NOCASE ASC
"""


def row_to_photo(row: sqlite3.Row) -> PhotoRow:
    return PhotoRow(
        path=row["path"],
        filename=row["filename"],
        media_type=row["media_type"],
        original_dt=row["original_dt"],
        fallback_dt=row["fallback_dt"],
        has_original_date=int(row["has_original_date"]),
        date_source=row["date_source"],
        live_video=row["live_video"],
        signature=row["signature"],
    )


def normalize_ocr_text(text: str) -> str:
    """Normalise le texte pour une recherche tolérante.

    Exemple : "Été 2024 - Café" devient "ete 2024 cafe".
    Ça rend la recherche moins fragile avec les accents, majuscules,
    apostrophes, retours à la ligne et ponctuation OCR.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def search_terms(text: str) -> list[str]:
    normalized = normalize_ocr_text(text)
    return [t for t in normalized.split() if t]


def load_photos_from_db(root: Path, search_text: str = "") -> list[PhotoRow]:
    """Charge la photothèque visible.

    V2.6 : correction de la recherche OCR.
    - sans recherche : on garde le comportement Apple/propre, donc on masque par défaut
      les fichiers sans vraie date originale ;
    - avec recherche : on cherche dans TOUTES les images indexées, y compris les
      captures d'écran / PNG / documents qui n'ont souvent pas d'EXIF original ;
    - la recherche se fait dans ocr_norm, donc sans accents/majuscules/ponctuation.
    """
    con = db_connect()
    root_str = str(root)
    try:
        terms = search_terms(search_text)
        if terms:
            clauses = []
            params: list[str] = [root_str]
            for term in terms:
                like = f"%{term}%"
                # En mode recherche on ne filtre PAS has_original_date=1 :
                # beaucoup de captures d'écran/documents utiles n'ont pas d'EXIF original.
                clauses.append("(ocr_norm LIKE ? OR lower(filename) LIKE ?)")
                params.extend([like, like])
            where_terms = " AND ".join(clauses)
            rows = con.execute(
                f"""
                SELECT * FROM photos
                WHERE root=?
                  AND media_type IN ('image', 'video')
                  AND {where_terms}
                {ORDER_SQL}
                """,
                tuple(params),
            ).fetchall()
        else:
            only_original_clause = "AND has_original_date=1" if DISPLAY_ONLY_ORIGINAL_DATES else ""
            rows = con.execute(
                f"""
                SELECT * FROM photos
                WHERE root=? {only_original_clause}
                  AND media_type IN ('image', 'video')
                {ORDER_SQL}
                """,
                (root_str,),
            ).fetchall()
        return [row_to_photo(r) for r in rows]
    finally:
        con.close()

def count_hidden_without_original_date(root: Path) -> int:
    con = db_connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM photos WHERE root=? AND media_type IN ('image', 'video') AND has_original_date=0",
            (str(root),),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        con.close()


def count_ocr_stats(root: Path) -> tuple[int, int]:
    """Retourne (images_ocr_indexees, images_total_images).

    V2.6 : on compte toutes les images, pas seulement celles avec date originale.
    Les captures d'écran et documents exportés peuvent ne pas avoir d'EXIF original,
    mais ce sont souvent précisément les images où la recherche texte est utile.
    """
    con = db_connect()
    try:
        indexed = con.execute(
            """
            SELECT COUNT(*) AS n FROM photos
            WHERE root=? AND media_type='image'
              AND ocr_norm IS NOT NULL AND ocr_norm != ''
              AND ocr_signature = signature
              AND ocr_version = ?
            """,
            (str(root), OCR_VERSION),
        ).fetchone()
        total = con.execute(
            """
            SELECT COUNT(*) AS n FROM photos
            WHERE root=? AND media_type='image'
            """,
            (str(root),),
        ).fetchone()
        return int(indexed["n"] or 0), int(total["n"] or 0)
    finally:
        con.close()


class ScanWorker(QThread):
    status = pyqtSignal(str)
    database_changed = pyqtSignal()
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, force_full_rescan: bool = False):
        super().__init__()
        self.root = root
        self.force_full_rescan = force_full_rescan
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if not self.root.exists():
                self.failed.emit(f"Dossier introuvable : {self.root}")
                return

            ensure_dirs()
            con = db_connect()
            root_str = str(self.root)
            now = datetime.now().isoformat(timespec="seconds")

            self.status.emit("Scan rapide du dossier…")
            images: list[Path] = []
            videos_by_key: dict[str, Path] = {}
            sidecars = 0
            total_files = 0

            for path in self.root.rglob("*"):
                if self._cancel:
                    con.close()
                    return
                if not path.is_file():
                    continue
                total_files += 1
                ext = path.suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    images.append(path)
                elif ext in VIDEO_EXTENSIONS:
                    videos_by_key[media_key(path)] = path
                elif ext in IGNORED_EXTENSIONS:
                    sidecars += 1

            # Les vidéos qui ont le même chemin sans extension qu'une image sont des Live Photos.
            # Elles ne sont pas affichées en doublon ; elles sont rattachées à l'image.
            live_video_keys = {media_key(img) for img in images if media_key(img) in videos_by_key}
            standalone_videos = [video for key, video in videos_by_key.items() if key not in live_video_keys]
            all_visible_media_candidates = images + standalone_videos
            found_paths = {str(p) for p in all_visible_media_candidates}

            cached_rows = con.execute(
                "SELECT path, signature FROM photos WHERE root=?",
                (root_str,),
            ).fetchall()
            cached = {r["path"]: r["signature"] for r in cached_rows}

            if self.force_full_rescan:
                to_update = all_visible_media_candidates
            else:
                to_update = []
                for p in all_visible_media_candidates:
                    try:
                        _, _, sig = file_signature(p)
                    except Exception:
                        continue
                    if cached.get(str(p)) != sig:
                        to_update.append(p)

            missing = [path for path in cached.keys() if path not in found_paths]
            if missing:
                self.status.emit(f"Nettoyage des fichiers supprimés : {len(missing)}")
                con.executemany("DELETE FROM photos WHERE path=?", [(p,) for p in missing])
                con.commit()
                self.database_changed.emit()

            self.status.emit(
                f"Bibliothèque : {len(images)} images · {len(standalone_videos)} vidéos affichables · "
                f"{len(live_video_keys)} Live .MOV masqués · {len(to_update)} nouvelles/modifiées"
            )

            # On met à jour les Live Photos même pour les fichiers déjà connus.
            # C'est rapide et ça évite les Live .MOV non associés si le scan précédent était incomplet.
            batch_live = []
            for p in images:
                live = videos_by_key.get(media_key(p))
                batch_live.append((str(live) if live else None, str(p)))
                if len(batch_live) >= 1000:
                    con.executemany("UPDATE photos SET live_video=? WHERE path=?", batch_live)
                    con.commit()
                    batch_live.clear()
            if batch_live:
                con.executemany("UPDATE photos SET live_video=? WHERE path=?", batch_live)
                con.commit()

            if not to_update:
                stats = self.make_stats(con, root_str, total_files, sidecars)
                self.finished_ok.emit(stats)
                con.close()
                return

            if shutil.which("exiftool"):
                self.status.emit("Lecture des vraies dates originales EXIF/XMP…")
            else:
                self.status.emit("ExifTool absent : les photos sans cache iront à la fin.")

            chunk_size = 220
            total = len(to_update)
            changed_since_emit = 0

            for start in range(0, total, chunk_size):
                if self._cancel:
                    con.close()
                    return

                chunk = to_update[start:start + chunk_size]
                meta = run_exiftool(chunk)

                # Sidecar XMP uniquement pour les images où l'image elle-même n'a pas de date originale.
                # Les vidéos utilisent leurs tags QuickTime/MediaCreateDate via ExifTool.
                missing_original: list[tuple[Path, list[Path]]] = []
                for p in chunk:
                    if p.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    dt, _source = best_original_date(meta.get(str(p), {}))
                    if not dt:
                        xmp = xmp_sidecar_candidates(p)
                        if xmp:
                            missing_original.append((p, xmp))

                if missing_original:
                    all_xmp = []
                    owner_by_xmp = {}
                    for owner, xmps in missing_original:
                        for x in xmps:
                            all_xmp.append(x)
                            owner_by_xmp[str(x)] = owner
                    xmp_meta = run_exiftool(all_xmp)
                    for xmp_path_str, item in xmp_meta.items():
                        owner = owner_by_xmp.get(xmp_path_str)
                        if owner:
                            # On stocke la metadata XMP sous une clé spéciale pour fallback.
                            meta[f"XMP::{owner}"] = item

                rows = []
                for p in chunk:
                    try:
                        size, mtime_ns, sig = file_signature(p)
                    except Exception:
                        continue

                    fallback_dt = datetime.fromtimestamp(mtime_ns / 1_000_000_000).isoformat(timespec="seconds")
                    dt, date_source = best_original_date(meta.get(str(p), {}))

                    if not dt:
                        dt, date_source = best_original_date(meta.get(f"XMP::{p}", {}))
                        if dt:
                            date_source = f"XMP:{date_source}"

                    original_dt = dt.isoformat(timespec="seconds") if dt else None
                    has_original_date = 1 if dt else 0
                    media_type = "video" if p.suffix.lower() in VIDEO_EXTENSIONS else "image"
                    live = videos_by_key.get(media_key(p)) if media_type == "image" else None

                    rows.append((
                        str(p),
                        root_str,
                        p.name,
                        p.suffix.lower(),
                        media_type,
                        size,
                        mtime_ns,
                        sig,
                        original_dt,
                        fallback_dt,
                        has_original_date,
                        date_source,
                        str(live) if live else None,
                        now,
                        now,
                    ))

                con.executemany(
                    """
                    INSERT INTO photos(
                        path, root, filename, ext, media_type, size, mtime_ns, signature,
                        original_dt, fallback_dt, has_original_date, date_source,
                        live_video, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        root=excluded.root,
                        filename=excluded.filename,
                        ext=excluded.ext,
                        media_type=excluded.media_type,
                        size=excluded.size,
                        mtime_ns=excluded.mtime_ns,
                        signature=excluded.signature,
                        original_dt=excluded.original_dt,
                        fallback_dt=excluded.fallback_dt,
                        has_original_date=excluded.has_original_date,
                        date_source=excluded.date_source,
                        live_video=excluded.live_video,
                        updated_at=excluded.updated_at,
                        ocr_text=CASE
                            WHEN photos.signature = excluded.signature THEN photos.ocr_text
                            ELSE NULL
                        END,
                        ocr_norm=CASE
                            WHEN photos.signature = excluded.signature THEN photos.ocr_norm
                            ELSE NULL
                        END,
                        ocr_signature=CASE
                            WHEN photos.signature = excluded.signature THEN photos.ocr_signature
                            ELSE NULL
                        END,
                        ocr_version=CASE
                            WHEN photos.signature = excluded.signature THEN photos.ocr_version
                            ELSE NULL
                        END
                    """,
                    rows,
                )
                con.commit()

                done = min(start + chunk_size, total)
                changed_since_emit += len(rows)
                self.status.emit(f"Dates originales indexées : {done}/{total}")

                if changed_since_emit >= 1000 or done == total:
                    changed_since_emit = 0
                    self.database_changed.emit()

            stats = self.make_stats(con, root_str, total_files, sidecars)
            self.finished_ok.emit(stats)
            con.close()

        except Exception as e:
            self.failed.emit(str(e))

    def make_stats(self, con: sqlite3.Connection, root_str: str, total_files: int, sidecars: int) -> dict:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN media_type='image' THEN 1 ELSE 0 END) AS images,
                SUM(CASE WHEN media_type='video' THEN 1 ELSE 0 END) AS videos,
                SUM(CASE WHEN has_original_date=1 THEN 1 ELSE 0 END) AS with_dates,
                SUM(CASE WHEN has_original_date=0 THEN 1 ELSE 0 END) AS no_dates,
                SUM(CASE WHEN media_type='image' AND live_video IS NOT NULL THEN 1 ELSE 0 END) AS live_pairs,
                MIN(CASE WHEN has_original_date=1 THEN original_dt ELSE NULL END) AS oldest,
                MAX(CASE WHEN has_original_date=1 THEN original_dt ELSE NULL END) AS newest
            FROM photos WHERE root=? AND media_type IN ('image', 'video')
            """,
            (root_str,),
        ).fetchone()
        return {
            "total_files": total_files,
            "sidecars": sidecars,
            "total": int(row["total"] or 0),
            "images": int(row["images"] or 0),
            "videos": int(row["videos"] or 0),
            "with_dates": int(row["with_dates"] or 0),
            "no_dates": int(row["no_dates"] or 0),
            "live_pairs": int(row["live_pairs"] or 0),
            "oldest": row["oldest"],
            "newest": row["newest"],
        }


# -----------------------------------------------------------------------------
# Thumbnail service
# -----------------------------------------------------------------------------

class ThumbSignals(QObject):
    ready = pyqtSignal(str, str, int)  # path, thumb_path, size


class ThumbnailService(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.signals = ThumbSignals()
        self.executor = ThreadPoolExecutor(max_workers=THUMB_WORKERS)
        self.inflight: set[tuple[str, str, int]] = set()
        self.done: set[tuple[str, str, int]] = set()
        self.max_inflight = THUMB_WORKERS * 8

    def request_many(self, rows: list[PhotoRow], size: int) -> None:
        submitted = 0
        for row in rows:
            key = (row.path, row.signature, size)
            if key in self.done or key in self.inflight:
                continue

            path_obj = Path(row.path)
            if not path_obj.exists():
                continue

            thumb = thumb_path_for(row.path, row.signature, size)
            if thumb.exists() and thumb.stat().st_size > 0:
                self.done.add(key)
                # Signal quand même, mais en file d'attente UI, pour que l'écran se remplisse vite.
                self.signals.ready.emit(row.path, str(thumb), size)
                continue

            if len(self.inflight) >= self.max_inflight:
                break

            self.inflight.add(key)
            future = self.executor.submit(ensure_thumbnail, row.path, row.signature, size)
            future.add_done_callback(lambda fut, k=key, p=row.path, s=size: self._finished(fut, k, p, s))
            submitted += 1

    def _finished(self, future, key: tuple[str, str, int], path: str, size: int) -> None:
        self.inflight.discard(key)
        self.done.add(key)
        try:
            thumb_path = future.result()
        except Exception:
            thumb_path = str(thumb_path_for(path, key[1], size))
        self.signals.ready.emit(path, thumb_path, size)

    def shutdown(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


# -----------------------------------------------------------------------------
# Virtual grid
# -----------------------------------------------------------------------------

class PhotoGrid(QAbstractScrollArea):
    photo_open_requested = pyqtSignal(int)
    visible_range_changed = pyqtSignal(int, int)

    def __init__(self, thumb_service: ThumbnailService, parent=None):
        super().__init__(parent)
        self.thumb_service = thumb_service
        self.photos: list[PhotoRow] = []
        self.tile_size = DEFAULT_TILE_SIZE
        self.columns = 1
        self.total_rows = 0
        self.pixmaps: dict[tuple[str, str, int], QPixmap] = {}
        self.pixmap_lru: list[tuple[str, str, int]] = []
        self.max_pixmaps = 900
        self.pending_repaint = False
        self._press_pos: Optional[QPoint] = None
        self._press_scroll_value = 0

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self.on_scroll)
        self.thumb_service.signals.ready.connect(self.on_thumb_ready)

        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setStyleSheet("QAbstractScrollArea { background: #111116; border: none; }")

    def set_photos(self, photos: list[PhotoRow], keep_position: bool = False, scroll_bottom: bool = False):
        old_value = self.verticalScrollBar().value()
        old_max = self.verticalScrollBar().maximum()
        was_near_bottom = old_max > 0 and old_value >= old_max - 500

        self.photos = photos
        self.recalculate_scrollbar()
        self.viewport().update()

        if scroll_bottom:
            QTimer.singleShot(0, self.scroll_to_bottom)
        elif keep_position:
            if was_near_bottom:
                QTimer.singleShot(0, self.scroll_to_bottom)
            else:
                QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(min(old_value, self.verticalScrollBar().maximum())))

        QTimer.singleShot(0, self.request_visible_thumbs)

    def set_tile_size(self, size: int):
        self.tile_size = max(MIN_TILE_SIZE, min(MAX_TILE_SIZE, int(size)))
        self.recalculate_scrollbar()
        self.viewport().update()
        QTimer.singleShot(0, self.request_visible_thumbs)

    def recalculate_scrollbar(self):
        viewport_width = max(1, self.viewport().width())
        pitch = self.tile_size + TILE_GAP
        self.columns = max(1, viewport_width // pitch)
        self.total_rows = (len(self.photos) + self.columns - 1) // self.columns if self.photos else 0
        content_height = self.total_rows * pitch
        viewport_height = self.viewport().height()
        scroll = self.verticalScrollBar()
        scroll.setPageStep(viewport_height)
        scroll.setSingleStep(max(32, self.tile_size // 2))
        scroll.setRange(0, max(0, content_height - viewport_height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.recalculate_scrollbar()
        self.request_visible_thumbs()

    def on_scroll(self):
        self.viewport().update()
        self.request_visible_thumbs()

    def scroll_to_bottom(self):
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self.request_visible_thumbs()

    def scroll_to_top(self):
        self.verticalScrollBar().setValue(0)
        self.request_visible_thumbs()

    def visible_indexes(self, preload: bool = False) -> tuple[int, int]:
        if not self.photos:
            return 0, 0
        pitch = self.tile_size + TILE_GAP
        y0 = self.verticalScrollBar().value()
        y1 = y0 + self.viewport().height()
        extra = PRELOAD_ROWS if preload else 1
        row0 = max(0, y0 // pitch - extra)
        row1 = min(self.total_rows, y1 // pitch + extra + 1)
        start = int(row0 * self.columns)
        end = int(min(len(self.photos), row1 * self.columns))
        return start, end

    def request_visible_thumbs(self):
        start, end = self.visible_indexes(preload=True)
        if end <= start:
            return
        rows = self.photos[start:end]
        self.thumb_service.request_many(rows, self.tile_size)
        self.visible_range_changed.emit(start, end)

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor("#111116"))

        if not self.photos:
            painter.setPen(QColor("#b8b2c7"))
            font = painter.font()
            font.setPointSize(14)
            painter.setFont(font)
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, "Aucune photo à afficher")
            painter.end()
            return

        pitch = self.tile_size + TILE_GAP
        y_scroll = self.verticalScrollBar().value()
        start, end = self.visible_indexes(preload=False)

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.setPen(Qt.PenStyle.NoPen)

        for index in range(start, end):
            if index < 0 or index >= len(self.photos):
                continue
            row = index // self.columns
            col = index % self.columns
            x = col * pitch
            y = row * pitch - y_scroll
            rect = QRect(x, y, self.tile_size, self.tile_size)

            if not rect.intersects(self.viewport().rect()):
                continue

            photo = self.photos[index]
            key = (photo.path, photo.signature, self.tile_size)
            pix = self.pixmaps.get(key)

            drew = False
            if pix and not pix.isNull():
                painter.drawPixmap(rect, pix)
                drew = True
            else:
                thumb = thumb_path_for(photo.path, photo.signature, self.tile_size)
                if thumb.exists() and thumb.stat().st_size > 0:
                    pix = QPixmap(str(thumb))
                    if not pix.isNull():
                        self.store_pixmap(key, pix)
                        painter.drawPixmap(rect, pix)
                        drew = True

                if not drew:
                    # Placeholder très léger.
                    painter.fillRect(rect, QColor("#24242b"))

            # V2.3 : pas de symbole play.
            # La durée des vidéos est dessinée directement dans la miniature cache.

        painter.end()

    def draw_video_overlay(self, painter: QPainter, rect: QRect):
        # Conservé uniquement pour compatibilité interne, mais volontairement désactivé : pas de symbole play.
        return

    def on_thumb_ready(self, path: str, thumb_path: str, size: int):
        if size != self.tile_size:
            return
        pix = QPixmap(thumb_path)
        if pix.isNull():
            return

        # On retrouve la signature courante pour éviter d'afficher une ancienne miniature.
        for p in self.photos:
            if p.path == path:
                key = (p.path, p.signature, size)
                self.store_pixmap(key, pix)
                break

        if not self.pending_repaint:
            self.pending_repaint = True
            QTimer.singleShot(30, self.coalesced_repaint)

    def store_pixmap(self, key: tuple[str, str, int], pix: QPixmap):
        self.pixmaps[key] = pix
        self.pixmap_lru.append(key)
        if len(self.pixmap_lru) > self.max_pixmaps:
            # Nettoyage simple, assez bon pour une grille qui scrolle beaucoup.
            while len(self.pixmap_lru) > self.max_pixmaps:
                old = self.pixmap_lru.pop(0)
                self.pixmaps.pop(old, None)

    def coalesced_repaint(self):
        self.pending_repaint = False
        self.viewport().update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._press_scroll_value = self.verticalScrollBar().value()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # V2.0 : un simple clic ouvre la photo.
        # On ignore le clic si la souris a bougé, pour éviter d'ouvrir pendant un scroll/drag.
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            pos = event.position().toPoint()
            moved = (pos - self._press_pos).manhattanLength()
            scrolled = abs(self.verticalScrollBar().value() - self._press_scroll_value)
            self._press_pos = None
            if moved <= 6 and scrolled <= 6:
                index = self.index_at(pos.x(), pos.y())
                if index is not None:
                    self.photo_open_requested.emit(index)
                    event.accept()
                    return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        # Double-clic conservé aussi, mais le simple clic suffit maintenant.
        index = self.index_at(event.position().toPoint().x(), event.position().toPoint().y())
        if index is not None:
            self.photo_open_requested.emit(index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def index_at(self, x: int, y: int) -> Optional[int]:
        if not self.photos:
            return None
        pitch = self.tile_size + TILE_GAP
        y_absolute = y + self.verticalScrollBar().value()
        col = x // pitch
        row = y_absolute // pitch
        if col < 0 or col >= self.columns:
            return None
        index = int(row * self.columns + col)
        if 0 <= index < len(self.photos):
            # Ne pas ouvrir le petit espace entre deux tuiles.
            if x % pitch <= self.tile_size and y_absolute % pitch <= self.tile_size:
                return index
        return None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_End:
            self.scroll_to_bottom()
        elif event.key() == Qt.Key.Key_Home:
            self.scroll_to_top()
        elif event.key() == Qt.Key.Key_PageDown:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + self.verticalScrollBar().pageStep())
        elif event.key() == Qt.Key.Key_PageUp:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - self.verticalScrollBar().pageStep())
        else:
            super().keyPressEvent(event)


# -----------------------------------------------------------------------------
# Viewer
# -----------------------------------------------------------------------------

class PreviewWorker(QThread):
    ready = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, photo: PhotoRow):
        super().__init__()
        self.photo = photo

    def run(self):
        try:
            self.ready.emit(make_preview(self.photo.path, self.photo.signature))
        except Exception as e:
            self.failed.emit(str(e))


class ViewerDialog(QDialog):
    def __init__(self, photos: list[PhotoRow], index: int, parent=None):
        super().__init__(parent)
        self.photos = photos
        self.index = index
        self.worker: Optional[PreviewWorker] = None

        self.setWindowTitle(APP_NAME)
        self.resize(1120, 820)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.prev_btn = QPushButton("←")
        self.next_btn = QPushButton("→")
        self.info = QLabel()
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.open_video_btn = QPushButton("Ouvrir le fichier")
        self.open_video_btn.clicked.connect(self.open_current_external)

        self.prev_btn.clicked.connect(self.prev_photo)
        self.next_btn.clicked.connect(self.next_photo)
        top.addWidget(self.prev_btn)
        top.addWidget(self.next_btn)
        top.addWidget(self.open_video_btn)
        top.addWidget(self.info, stretch=1)

        self.image = QLabel("Chargement…")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root.addLayout(top)
        root.addWidget(self.image, stretch=1)

        self.setStyleSheet("""
            QDialog { background: #121217; color: #f2eef7; }
            QLabel { color: #f2eef7; }
            QPushButton {
                background: #2a2a33;
                color: #f2eef7;
                border: 1px solid #454552;
                border-radius: 10px;
                padding: 8px 12px;
            }
            QPushButton:hover { background: #373742; }
        """)

        self.load_current()

    def load_current(self):
        photo = self.photos[self.index]
        kind = " · Vidéo" if photo.media_type == "video" else ""
        live = " · Live" if photo.live_video else ""
        date = photo.original_dt or "date originale absente"
        self.info.setText(f"{self.index + 1}/{len(self.photos)} · {date}{kind}{live}\n{photo.path}")
        self.image.setText("Chargement…")
        self.image.setPixmap(QPixmap())
        self.open_video_btn.setVisible(True)
        self.open_video_btn.setText("Ouvrir la vidéo" if photo.media_type == "video" else "Ouvrir l'original")
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < len(self.photos) - 1)

        # V2.0 : afficher immédiatement la miniature déjà en cache,
        # puis remplacer par une preview plus grande dès qu'elle est prête.
        self.show_cached_thumb_or_placeholder(photo)

        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.quit()
        self.worker = PreviewWorker(photo)
        self.worker.ready.connect(self.show_preview)
        self.worker.failed.connect(lambda msg: self.image.setText(msg))
        self.worker.start()

    def show_cached_thumb_or_placeholder(self, photo: PhotoRow):
        # Utilise la miniature carrée si elle existe déjà, donc le viewer réagit tout de suite au clic.
        candidates = []
        for size in (self.parent().grid.tile_size if hasattr(self.parent(), "grid") else DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, 160, 132):
            try:
                candidates.append(thumb_path_for(photo.path, photo.signature, int(size)))
            except Exception:
                pass
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                pix = QPixmap(str(candidate))
                if not pix.isNull():
                    scaled = pix.scaled(
                        self.image.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.FastTransformation,
                    )
                    self.image.setPixmap(scaled)
                    return
        if photo.media_type == "video":
            self.image.setText("Vidéo — Entrée/Espace pour ouvrir")
        else:
            self.image.setText("Chargement de la photo…")

    def show_preview(self, preview_path: str):
        pix = QPixmap(preview_path)
        if pix.isNull():
            self.image.setText("Impossible d'afficher cette photo")
            return
        scaled = pix.scaled(
            self.image.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.worker and self.worker.isRunning():
            return
        photo = self.photos[self.index]
        path = preview_path_for(photo.path, photo.signature)
        if path.exists():
            self.show_preview(str(path))

    def prev_photo(self):
        if self.index > 0:
            self.index -= 1
            self.load_current()

    def next_photo(self):
        if self.index < len(self.photos) - 1:
            self.index += 1
            self.load_current()

    def open_current_external(self):
        if 0 <= self.index < len(self.photos):
            photo = self.photos[self.index]
            QDesktopServices.openUrl(QUrl.fromLocalFile(photo.path))

    def mouseDoubleClickEvent(self, event):
        photo = self.photos[self.index]
        if photo.media_type == "video":
            self.open_current_external()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self.prev_photo()
        elif event.key() == Qt.Key.Key_Right:
            self.next_photo()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.photos[self.index].media_type == "video":
                self.open_current_external()
        elif event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


# -----------------------------------------------------------------------------
# OCR worker
# -----------------------------------------------------------------------------

class OCRWorker(QThread):
    status = pyqtSignal(str)
    database_changed = pyqtSignal()
    finished_ok = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if not shutil.which("tesseract"):
                self.failed.emit("Tesseract n'est pas installé. Installe : sudo apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng")
                return

            con = db_connect()
            rows = con.execute(
                """
                SELECT path, signature FROM photos
                WHERE root=? AND media_type='image'
                  AND (
                    ocr_text IS NULL OR ocr_norm IS NULL OR ocr_signature IS NULL
                    OR ocr_signature != signature OR ocr_version IS NULL OR ocr_version != ?
                  )
                ORDER BY
                  CASE WHEN has_original_date=1 THEN 0 ELSE 1 END ASC,
                  CASE WHEN has_original_date=1 THEN original_dt ELSE fallback_dt END DESC
                """,
                (str(self.root), OCR_VERSION),
            ).fetchall()

            total = len(rows)
            done = 0
            changed = 0
            self.status.emit(f"OCR complet à indexer : {total} image(s)")

            for row in rows:
                if self._cancel:
                    con.close()
                    return
                path = row["path"]
                sig = row["signature"]
                p = Path(path)
                if not p.exists():
                    continue

                text = self.ocr_one(path)
                norm = normalize_ocr_text(text)
                con.execute(
                    "UPDATE photos SET ocr_text=?, ocr_norm=?, ocr_signature=?, ocr_version=?, updated_at=? WHERE path=?",
                    (text, norm, sig, OCR_VERSION, datetime.now().isoformat(timespec="seconds"), path),
                )
                done += 1
                changed += 1
                if changed >= 25:
                    changed = 0
                    con.commit()
                    self.database_changed.emit()
                if done % 10 == 0 or done == total:
                    self.status.emit(f"OCR complet : {done}/{total}")

            con.commit()
            self.database_changed.emit()
            self.finished_ok.emit(done)
            con.close()
        except Exception as e:
            self.failed.emit(str(e))

    def ocr_one(self, path: str) -> str:
        # Tesseract ne lit pas toujours HEIC directement : on crée une image temporaire légère.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "ocr.jpg"
            try:
                with Image.open(path) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
                    img = img.convert("RGB")
                    img.save(tmp, quality=88)
            except Exception:
                return ""

            # On essaie deux modes :
            # - psm 6 pour blocs de texte/documents ;
            # - psm 11 pour texte épars sur captures, affiches, memes, interfaces.
            texts = []
            for psm in ("6", "11"):
                try:
                    result = subprocess.run(
                        ["tesseract", str(tmp), "stdout", "-l", "fra+eng", "--psm", psm],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=45,
                        check=False,
                    )
                    out = (result.stdout or "").strip()
                    if out:
                        texts.append(out)
                except Exception:
                    continue
            return "\n".join(texts).strip().lower()


# -----------------------------------------------------------------------------
# Main window
# -----------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_dirs()

        self.con = db_connect()
        saved_root = get_setting(self.con, "photos_root", str(DEFAULT_PHOTOS_DIR))
        self.photos_root = Path(saved_root)
        self.photos: list[PhotoRow] = []
        self.search_text = ""
        self.scan_worker: Optional[ScanWorker] = None
        self.ocr_worker: Optional[OCRWorker] = None
        self.thumb_service = ThumbnailService(self)

        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(lambda: self.load_from_db(keep_position=True))

        self.setup_ui()
        self.apply_style()

        # Chargement quasi immédiat depuis SQLite, puis scan arrière-plan.
        self.load_from_db(scroll_bottom=True)
        self.start_scan(force=False)

    def setup_ui(self):
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1280, 860)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.title = QLabel("Princess Photos V2.5")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        self.title.setFont(font)

        self.folder_btn = QPushButton("Dossier")
        self.scan_btn = QPushButton("Scanner")
        self.full_rescan_btn = QPushButton("Rescan dates originales")
        self.ocr_btn = QPushButton("Indexer OCR complet")
        self.export_btn = QPushButton("CSV")

        self.folder_btn.clicked.connect(self.choose_folder)
        self.scan_btn.clicked.connect(lambda: self.start_scan(force=False))
        self.full_rescan_btn.clicked.connect(lambda: self.start_scan(force=True))
        self.ocr_btn.clicked.connect(self.start_ocr)
        self.export_btn.clicked.connect(self.export_csv)

        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.folder_btn)
        top.addWidget(self.scan_btn)
        top.addWidget(self.full_rescan_btn)
        top.addWidget(self.ocr_btn)
        top.addWidget(self.export_btn)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Recherche OCR : texte visible dans les photos/captures, même sans date originale…")
        self.search.textChanged.connect(self.on_search_changed)

        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setMinimum(MIN_TILE_SIZE)
        self.zoom.setMaximum(MAX_TILE_SIZE)
        self.zoom.setValue(DEFAULT_TILE_SIZE)
        self.zoom.setFixedWidth(170)
        self.zoom.valueChanged.connect(self.on_zoom_changed)

        self.status = QLabel("Prête.")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        search_row.addWidget(self.search, stretch=2)
        search_row.addWidget(QLabel("Zoom"))
        search_row.addWidget(self.zoom)
        search_row.addWidget(self.status, stretch=3)

        self.grid = PhotoGrid(self.thumb_service)
        self.grid.photo_open_requested.connect(self.open_photo)

        root.addLayout(top)
        root.addLayout(search_row)
        root.addWidget(self.grid, stretch=1)

        quit_action = QAction("Quitter", self)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #151519; color: #f2eef7; }
            QLabel { color: #f2eef7; }
            QLineEdit {
                background: #23232a;
                color: #f2eef7;
                border: 1px solid #3a3a45;
                border-radius: 10px;
                padding: 8px;
                selection-background-color: #d66bff;
            }
            QPushButton {
                background: #2b2b34;
                color: #f2eef7;
                border: 1px solid #484856;
                border-radius: 10px;
                padding: 8px 12px;
            }
            QPushButton:hover { background: #373743; }
            QPushButton:disabled {
                color: #777782;
                background: #202026;
                border-color: #303038;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #2b2b34;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 16px;
                height: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: #d66bff;
            }
        """)

    def load_from_db(self, keep_position: bool = False, scroll_bottom: bool = False):
        self.photos = load_photos_from_db(self.photos_root, self.search_text)
        self.grid.set_photos(
            self.photos,
            keep_position=keep_position,
            scroll_bottom=scroll_bottom and not self.search_text,
        )
        hidden_no_dates = count_hidden_without_original_date(self.photos_root)
        ocr_indexed, ocr_total = count_ocr_stats(self.photos_root)
        if self.search_text:
            self.status.setText(
                f"{len(self.photos)} résultat(s) OCR · OCR indexé {ocr_indexed}/{ocr_total} · "
                f"sans date originale masqués : {hidden_no_dates}"
            )
        else:
            live = sum(1 for p in self.photos if p.live_video)
            videos = sum(1 for p in self.photos if p.media_type == "video")
            images = len(self.photos) - videos
            self.status.setText(
                f"{images} photos · {videos} vidéos · OCR indexé {ocr_indexed}/{ocr_total} · "
                f"{hidden_no_dates} sans date originale masqués · "
                f"{live} Live Photos · cache SQLite"
            )

    def schedule_reload(self):
        self.reload_timer.start(500)

    def start_scan(self, force: bool = False):
        if self.scan_worker and self.scan_worker.isRunning():
            self.status.setText("Scan déjà en cours…")
            return

        self.scan_worker = ScanWorker(self.photos_root, force_full_rescan=force)
        self.scan_worker.status.connect(self.status.setText)
        self.scan_worker.database_changed.connect(self.schedule_reload)
        self.scan_worker.finished_ok.connect(self.on_scan_finished)
        self.scan_worker.failed.connect(self.on_worker_failed)
        self.scan_worker.start()
        self.status.setText("Scan arrière-plan lancé…")

    def on_scan_finished(self, stats: dict):
        self.load_from_db(keep_position=True)
        oldest = stats.get("oldest") or "?"
        newest = stats.get("newest") or "?"
        self.status.setText(
            f"{stats.get('with_dates', 0)} médias affichables · "
            f"{stats.get('images', 0)} images en base · {stats.get('videos', 0)} vidéos standalone · "
            f"{stats.get('no_dates', 0)} sans date originale masqués · "
            f"{stats.get('live_pairs', 0)} Live · {oldest[:10]} → {newest[:10]}"
        )

    def on_worker_failed(self, msg: str):
        self.status.setText("Erreur.")
        QMessageBox.critical(self, APP_NAME, msg)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choisir le dossier Photos", str(self.photos_root))
        if not folder:
            return
        self.photos_root = Path(folder)
        set_setting(self.con, "photos_root", str(self.photos_root))
        self.search.clear()
        self.search_text = ""
        self.load_from_db(scroll_bottom=True)
        self.start_scan(force=False)

    def on_search_changed(self, text: str):
        self.search_text = text.strip()
        # Petite latence pour ne pas requêter SQLite à chaque frappe ultra rapide.
        self.reload_timer.stop()
        QTimer.singleShot(180, lambda: self.load_from_db(scroll_bottom=False))

    def on_zoom_changed(self, value: int):
        self.grid.set_tile_size(value)

    def open_photo(self, index: int):
        if 0 <= index < len(self.photos):
            dialog = ViewerDialog(self.photos, index, self)
            dialog.exec()

    def start_ocr(self):
        if self.ocr_worker and self.ocr_worker.isRunning():
            self.status.setText("OCR complet déjà en cours…")
            return
        self.ocr_worker = OCRWorker(self.photos_root)
        self.ocr_worker.status.connect(self.status.setText)
        self.ocr_worker.database_changed.connect(self.schedule_reload)
        self.ocr_worker.finished_ok.connect(lambda n: (self.load_from_db(keep_position=True), self.status.setText(f"OCR complet terminé : {n} images traitées — recherche texte prête")))
        self.ocr_worker.failed.connect(self.on_worker_failed)
        self.ocr_worker.start()

    def export_csv(self):
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Exporter l'index CSV",
            str(Path.home() / "Documents" / "princess_photos_index_v20.csv"),
            "CSV (*.csv)",
        )
        if not out_path:
            return

        rows = load_photos_from_db(self.photos_root, "")
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["index", "media_type", "original_dt", "fallback_dt", "date_source", "filename", "path", "live_video"])
            for i, p in enumerate(rows, start=1):
                writer.writerow([i, p.media_type, p.original_dt or "", p.fallback_dt, p.date_source, p.filename, p.path, p.live_video or ""])
        QMessageBox.information(self, APP_NAME, f"CSV exporté :\n{out_path}")

    def closeEvent(self, event):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.cancel()
        if self.ocr_worker and self.ocr_worker.isRunning():
            self.ocr_worker.cancel()
        self.thumb_service.shutdown()
        try:
            self.con.close()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    ensure_dirs()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setOrganizationName("PrincessApps")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setStyle("Fusion")
    window = MainWindow()
    if APP_ICON_PATH.exists():
        window.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
