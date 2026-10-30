"""Recompute the generated fields of books.json from the archives under books/.

Hand-maintained fields (name, description, source, license, ...) are kept as
they are. Generated fields (url, archive, content, positions) are recomputed
from the files, so run this after adding, replacing or removing a book.

usage:
  python scripts/update_books.py          rewrite books.json
  python scripts/update_books.py --check  exit 1 if books.json is out of date

Only the Python standard library is required.
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import sys
import urllib.parse
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "books.json"
BOOKS_DIR = ROOT / "books"

KEY_ORDER = [
    "id", "name", "description", "collection", "category", "variant", "format",
    "file_name", "path", "url", "archive", "content", "positions",
    "author", "source", "license", "notes",
]
REQUIRED = ["id", "name", "description", "collection", "category", "variant",
            "format", "file_name", "path", "source", "license"]
CATEGORIES = {"openings", "endgames", "stalemates"}
VARIANTS = {"standard", "chess960"}
FORMATS = {"epd", "pgn"}

RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}
HEADER_RE = re.compile(r'\[(\w+)\s+"(.*)"\]')


def ply_of(fullmove, side):
    return (fullmove - 1) * 2 + (1 if side == "b" else 0)


class Stats:
    def __init__(self):
        self.white = self.black = 0
        self.min_ply = self.max_ply = None

    def add(self, side, ply):
        if side == "w":
            self.white += 1
        else:
            self.black += 1
        if ply is not None:
            self.min_ply = ply if self.min_ply is None else min(self.min_ply, ply)
            self.max_ply = ply if self.max_ply is None else max(self.max_ply, ply)

    def as_dict(self):
        return {
            "total": self.white + self.black,
            "white_to_move": self.white,
            "black_to_move": self.black,
            "min_ply": self.min_ply,
            "max_ply": self.max_ply,
        }


def epd_stats(text):
    stats = Stats()
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 4 or fields[1] not in ("w", "b"):
            raise ValueError(f"line {number} is not a FEN/EPD record: {line[:80]!r}")
        ply = None
        if len(fields) > 5 and fields[5].isdigit():
            ply = ply_of(int(fields[5]), fields[1])
        stats.add(fields[1], ply)
    return stats.as_dict()


def pgn_games(text):
    headers, movetext = {}, []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            match = HEADER_RE.match(stripped)
            if movetext or (match and match[1] in headers):
                yield headers, "\n".join(movetext)
                headers, movetext = {}, []
            if match:
                headers[match[1]] = match[2]
        elif stripped and not stripped.startswith("%"):
            movetext.append(line)
    if headers or movetext:
        yield headers, "\n".join(movetext)


def count_moves(movetext):
    movetext = re.sub(r"\{[^}]*\}|;[^\n]*", " ", movetext)
    previous = None
    while previous != movetext:  # strip nested variations from the inside out
        previous, movetext = movetext, re.sub(r"\([^()]*\)", " ", movetext)
    moves = 0
    for token in movetext.split():
        if token in RESULTS or token.startswith("$"):
            continue
        if re.sub(r"^\d+\.+", "", token):
            moves += 1
    return moves


def pgn_stats(text):
    stats = Stats()
    for headers, movetext in pgn_games(text):
        start = 0
        if "FEN" in headers:
            fields = headers["FEN"].split()
            fullmove = int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 1
            start = ply_of(fullmove, fields[1])
        ply = start + count_moves(movetext)
        stats.add("w" if ply % 2 == 0 else "b", ply)
    return stats.as_dict()


def describe_archive(book):
    archive_path = ROOT / book["path"]
    if not archive_path.is_file():
        raise ValueError(f"{book['path']} does not exist")
    raw = archive_path.read_bytes()
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if names != [book["file_name"]]:
            raise ValueError(f"{book['path']} must contain only {book['file_name']}, found {names}")
        content = archive.read(book["file_name"])
    text = content.decode("utf-8", errors="replace")
    positions = epd_stats(text) if book["format"] == "epd" else pgn_stats(text)
    return (
        {"format": "zip", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
        {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()},
        positions,
    )


def validate(book):
    missing = [key for key in REQUIRED if not book.get(key)]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    if book["category"] not in CATEGORIES:
        raise ValueError(f"unknown category {book['category']!r}")
    if book["variant"] not in VARIANTS:
        raise ValueError(f"unknown variant {book['variant']!r}")
    if book["format"] not in FORMATS or not book["file_name"].endswith("." + book["format"]):
        raise ValueError(f"format {book['format']!r} does not match {book['file_name']}")
    if not book["path"].startswith("books/") or not book["path"].endswith(".zip"):
        raise ValueError(f"path {book['path']!r} must be a .zip under books/")


def build(manifest):
    base_url = manifest["base_url"]
    books, seen = [], set()
    for book in manifest["books"]:
        book_id = book.get("id", "?")
        try:
            validate(book)
            if book_id in seen:
                raise ValueError("duplicate id")
            seen.add(book_id)
            book["url"] = base_url + urllib.parse.quote(book["path"])
            book["archive"], book["content"], book["positions"] = describe_archive(book)
        except (ValueError, zipfile.BadZipFile) as error:
            sys.exit(f"{book_id}: {error}")
        ordered = {key: book[key] for key in KEY_ORDER if key in book}
        ordered.update({key: value for key, value in book.items() if key not in ordered})
        books.append(ordered)
        print(f"{book_id}: {book['positions']['total']} positions", flush=True)

    listed = {book["path"] for book in books}
    unlisted = sorted(
        path.relative_to(ROOT).as_posix()
        for path in BOOKS_DIR.rglob("*.zip")
        if path.relative_to(ROOT).as_posix() not in listed
    )
    if unlisted:
        sys.exit("archives without a books.json entry:\n  " + "\n  ".join(unlisted))

    books.sort(key=lambda book: book["path"].lower())
    return {**manifest, "books": books}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify books.json without rewriting it")
    args = parser.parse_args()

    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    updated = build(json.loads(json.dumps(current)))

    def without_date(manifest):
        return {key: value for key, value in manifest.items() if key != "updated_at"}

    if without_date(updated) == without_date(current):
        print("books.json is up to date.")
        return
    if args.check:
        sys.exit("books.json is out of date; run python scripts/update_books.py")

    updated["updated_at"] = datetime.date.today().isoformat()
    MANIFEST.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
    print(f"Wrote {len(updated['books'])} books to {MANIFEST.name}.")


if __name__ == "__main__":
    main()
