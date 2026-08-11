"""On-disk library layout compatible with litres-downloader / Audiobookshelf.

Layout:
    {root}/{Author}/{Title}/metadata.json
    {root}/{Author}/{Title}/<media files...>

`metadata.json` field names match litres-downloader's ABS-oriented schema
(see that project's pkg/library/abs.go), including camelCase keys like
`publisherYear` and string series entries of the form \"Name #N\".
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

METADATA_NAME = "metadata.json"
_INVALID = re.compile(r'[<>:"/\\|?*]')


def library_root_from_env() -> Path | None:
    """Return configured library root, or None if auto-library is disabled."""
    raw = (os.environ.get("LITRES_LIBRARY_DIR") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def sanitize_component(name: str) -> str:
    """Replace filesystem-invalid characters the same way litres-downloader does.

    A component of only dots ("." / "..") is path traversal, not a name, so it
    also degrades to "_" -- the `_INVALID` set does not include the dot (legit
    in real titles), so this is the guard that stops a crafted author/title from
    walking out of the library root."""
    cleaned = _INVALID.sub("_", name or "").strip()
    if not cleaned or set(cleaned) <= {"."}:
        return "_"
    return cleaned


def book_dir(root: Path, meta: dict) -> Path:
    """Author/Title directory under `root` for this book metadata dict."""
    authors = meta.get("authors") or []
    author = authors[0] if authors else "Unknown"
    title = meta.get("title") or str(meta.get("id") if meta.get("id") is not None else "book")
    return Path(root) / sanitize_component(str(author)) / sanitize_component(str(title))


def book_dir_for_art(root: Path, meta: dict) -> Path:
    """Like book_dir, but on collision with a different art id, append (id)."""
    base = book_dir(root, meta)
    art_id = meta.get("id")
    if art_id is None:
        return base
    marker = base / METADATA_NAME
    if not marker.exists():
        return base
    try:
        existing = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    existing_id = existing.get("_art_id")
    if existing_id is None or existing_id == art_id:
        return base
    title = meta.get("title") or str(art_id)
    return base.parent / f"{sanitize_component(str(title))} ({art_id})"


def pick_last_release(src: dict) -> str:
    """Idempotency key preferred order for skip/re-download decisions."""
    for key in (
        "last_released_at",
        "last_release",
        "last_updated_at",
        "purchased_at",
        "publication_date",
    ):
        val = src.get(key)
        if val:
            return str(val)
    return ""


def _year(value) -> str:
    if not value:
        return ""
    s = str(value)
    m = re.match(r"(\d{4})", s)
    return m.group(1) if m else s


def abs_metadata(item: dict, details: dict | None = None) -> dict:
    """Build litres-downloader-compatible metadata.json payload."""
    src = {**(item or {}), **(details or {})}
    authors = list(src.get("authors") or [])
    if not authors:
        authors = ["Unknown"]

    series_list = src.get("series_list")
    if not series_list:
        primary = src.get("series")
        series_list = [primary] if isinstance(primary, dict) else []
    series_strs: list[str] = []
    for s in series_list or []:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not name:
            continue
        order = s.get("art_order")
        if order is None:
            order = s.get("sequence_number")
        if order is not None and str(order) != "":
            series_strs.append(f"{name} #{order}")
        else:
            series_strs.append(str(name))

    lang = src.get("language_code") or src.get("language") or ""
    if lang == "rus":
        lang = "Russian"

    last = pick_last_release(src)
    out = {
        "last_release": last,
        "title": src.get("title") or "",
        "subtitle": src.get("subtitle") or "",
        "authors": authors,
        "narrators": list(src.get("narrators") or []),
        "series": series_strs,
        "genres": list(src.get("genres") or []),
        "tags": list(src.get("tags") or []),
        "publisherYear": _year(
            src.get("date_written_at") or src.get("publication_date") or src.get("publisherYear")
        ),
        "publishedDate": src.get("publication_date") or src.get("publishedDate") or last,
        "publisher": src.get("publisher") or "",
        "description": src.get("description") or "",
        "isbn": src.get("isbn") or "",
        "asin": src.get("asin") or "",
        "language": lang,
        "explicit": bool(src.get("is_adult_content") or src.get("explicit")),
        "abridged": bool(src.get("abridged")),
    }
    if src.get("id") is not None:
        out["_art_id"] = src["id"]
    return out


def write_metadata(book_path: Path, meta: dict) -> Path:
    book_path.mkdir(parents=True, exist_ok=True)
    path = book_path / METADATA_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_metadata(book_path: Path) -> dict | None:
    path = Path(book_path) / METADATA_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable metadata at %s: %s", path, exc)
        return None


def media_files(book_path: Path) -> list[Path]:
    """Non-metadata files in the book directory (not recursive)."""
    p = Path(book_path)
    if not p.is_dir():
        return []
    out = []
    for child in p.iterdir():
        if child.is_file() and child.name != METADATA_NAME and not child.name.endswith(".tmp"):
            out.append(child)
    return out


def is_up_to_date(book_path: Path, last_release: str) -> bool:
    """True when metadata matches last_release and at least one media file exists."""
    if not last_release:
        return False
    meta = read_metadata(book_path)
    if not meta:
        return False
    if str(meta.get("last_release") or "") != str(last_release):
        return False
    return len(media_files(book_path)) > 0


def _safe_extract_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    base = Path(normalized).name
    if not base or base in (".", ".."):
        raise ValueError(f"Refusing unsafe zip member name: {name!r}")
    return base


def extract_audio_zip(src_zip: Path, dest_dir: Path) -> list[Path]:
    """Extract audio-zip members as basenames into dest_dir (zip-slip safe)."""
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(src_zip) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = _safe_extract_member_name(info.filename)
            target = (dest_dir / base).resolve()
            if target.parent != dest_dir:
                raise ValueError(f"Zip slip blocked for member {info.filename!r}")
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            written.append(target)
    if not written:
        raise ValueError(f"Audio zip contained no files: {src_zip}")
    return written


def clear_media(book_path: Path) -> None:
    """Remove previous media files (keep directory); used before re-install."""
    for f in media_files(book_path):
        try:
            f.unlink()
        except OSError as exc:
            logger.warning("Could not remove old media %s: %s", f, exc)


def install_book(
    root: Path,
    meta: dict,
    src: Path,
    *,
    is_audio: bool = False,
) -> Path:
    """Install downloaded `src` into the ABS book folder and write metadata.

    For audiobook zip bundles (`zip_with_mp3`), extracts tracks into the folder.
    Otherwise moves/copies the single file. Metadata is written only after media
    is in place. Returns the book directory path.

    `meta` should already be an abs_metadata() dict (or compatible).
    """
    dest_dir = book_dir_for_art(root, meta)
    dest_dir.mkdir(parents=True, exist_ok=True)
    clear_media(dest_dir)

    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"Download missing: {src}")

    installed_from_tmp = src
    try:
        if is_audio and zipfile.is_zipfile(src):
            extract_audio_zip(src, dest_dir)
        else:
            target = dest_dir / src.name
            try:
                os.replace(src, target)
                installed_from_tmp = None  # moved
            except OSError:
                shutil.copy2(src, target)

        if not media_files(dest_dir):
            raise RuntimeError(f"No media files installed into {dest_dir}")

        write_metadata(dest_dir, meta)
    except Exception:
        # Don't leave success-looking metadata after a failed install.
        meta_path = dest_dir / METADATA_NAME
        meta_path.unlink(missing_ok=True)
        raise
    finally:
        if installed_from_tmp is not None and installed_from_tmp.exists():
            try:
                installed_from_tmp.unlink()
            except OSError:
                pass

    return dest_dir


# Index of what we actually wrote into a mirror folder, kept beside the files.
# Shape: {"<art_id>": {"name": "Title.epub", "size": 1234, "tracks": 12|null}}
MIRROR_INDEX = ".bookvault-index.json"


def read_mirror_index(root: Path) -> dict:
    try:
        data = json.loads((root / MIRROR_INDEX).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_in_mirror_index(root: Path, art_id, name: str, size: int, tracks=None) -> None:
    """Remember what a successful download actually produced.

    **Why not compare against the catalogue's size.** litres.ru's file listing
    reports a size that is *not* the byte length of what it serves: measured
    across a real 239-book library, only 10 files matched their listed size and
    147 did not, the delivered file usually being smaller by a variable amount.
    Checking against it would mark almost every book incomplete on every run and
    re-download the entire library each time -- the exact request pattern that
    gets an account anti-bot flagged, and far worse than the stale-file problem
    it was meant to solve.

    So the only trustworthy statement is the one we can make ourselves: "this
    is what the finished download looked like when we wrote it." That is direct
    evidence, it survives restarts, and it still catches the case that matters
    -- a transfer interrupted part-way leaves a file whose length differs from
    what we recorded on the run that completed."""
    index = read_mirror_index(root)
    index[str(art_id)] = {"name": name, "size": int(size), "tracks": tracks}
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (MIRROR_INDEX + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=0))
    os.replace(tmp, root / MIRROR_INDEX)


def file_is_complete(path: Path, expected_size) -> bool:
    """Whether an already-downloaded file can be left alone.

    `expected_size` is the length recorded when we last finished writing this
    file (see `record_in_mirror_index`) -- NOT a size from litres.ru's listing,
    which does not describe the bytes it serves.

    None/0 means we have no record of writing it: the user put the file there,
    or it predates the index. There is nothing to verify against, and
    re-downloading a library because we lack a record is worse than trusting
    what is on disk, so it counts as complete."""
    try:
        if not path.is_file():
            return False
        actual = path.stat().st_size
        # A zero-length file is never a book, whatever we do or don't have
        # recorded -- it's a transfer that died before the first chunk.
        if actual == 0:
            return False
        if not expected_size:
            return True
        return actual == int(expected_size)
    except OSError:  # pragma: no cover - unreadable path
        return False


def download_to_temp_then_rename(client, art_id, file_id, dest: Path, **kwargs) -> Path:
    """Download into a sibling `.part` file and rename it into place on success.

    Writing straight to `dest` means a failed or cancelled transfer leaves
    wreckage exactly where the finished file belongs -- and the next run, which
    can only judge by what is on disk, inspects the wreckage. Staging alongside
    (not in the system temp dir) keeps the rename atomic instead of a
    cross-filesystem copy of a possibly multi-gigabyte file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(f".{dest.name}.part")
    try:
        client.download_file(art_id, file_id, staging.name, staging, **kwargs)
        os.replace(staging, dest)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return dest
