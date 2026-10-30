# Chess Manifest

A list of chess engine repositories ([engines.json](engines.json)) and a collection of opening books ([books.json](books.json)) that applications can install.

## Opening books

The [books/](books/) folder holds 63 opening and test-position books. Each is a zip archive containing a single EPD or PGN file.

| Folder | Contents | License |
| --- | --- | --- |
| [books/stockfish/](books/stockfish/) | All 39 books from [official-stockfish/books](https://github.com/official-stockfish/books), byte for byte: UHO (40-60, Lichess 48-52, XXL, MEGA 2022), Chess960/FRC and DFRC, noob, 2/8 moves, closed and popular positions, endgames, stalemates. | CC0-1.0 ([LICENSE](books/stockfish/LICENSE)) |
| [books/uho-2024/](books/uho-2024/) | Stefan Pohl's [UHO 2024](https://www.sp-cc.de/uho_2024.htm) sets as EPD: 8 evaluation bands, each with 6 moves, 8 moves and 8 moves big. | No license stated, © 2024 Stefan Pohl ([README.txt](books/uho-2024/README.txt)) |

Unbalanced books (UHO and similar) give one side an advantage, so play each opening twice with colours reversed.

### Installing a book

[books.json](books.json) lists every book with its metadata and checksums. The field reference is in its `metadata_notes`. To install a book:

1. Fetch `https://raw.githubusercontent.com/CodeAdopter/Chess-Engines/main/books.json`.
2. Pick a book by `id`, or filter by `collection`, `category`, `variant` or `format`.
3. Download `url` and check that its size and SHA-256 match `archive.size` and `archive.sha256`.
4. Extract the single member `file_name`. Optionally check it against `content.sha256`.
5. If `variant` is `chess960`, enable Chess960 in the engine (`UCI_Chess960`) and in the match runner.

```json
{
  "id": "uho-lichess-4852-v1",
  "name": "UHO Lichess 48-52 v1",
  "collection": "stockfish",
  "category": "openings",
  "variant": "standard",
  "format": "epd",
  "file_name": "UHO_Lichess_4852_v1.epd",
  "url": "https://raw.githubusercontent.com/CodeAdopter/Chess-Engines/main/books/stockfish/UHO_Lichess_4852_v1.epd.zip",
  "archive": { "format": "zip", "size": 42877788, "sha256": "4e298f11…" },
  "content": { "size": 174889110, "sha256": "7a7f6470…" },
  "positions": { "total": 2632036, "white_to_move": 1457270, "black_to_move": 1174766, "min_ply": 2, "max_ply": 16 },
  "license": { "name": "Creative Commons Zero v1.0 Universal", "spdx_id": "CC0-1.0", "url": "…" }
}
```

### Adding or updating a book

1. Put the zip under `books/<collection>/`, named `<file_name>.zip` and containing only that file.
2. Add or edit its entry in `books.json`: `id`, `name`, `description`, `collection`, `category`, `variant`, `format`, `file_name`, `path`, `author`, `source`, `license` and optional `notes`.
3. Run `python scripts/update_books.py` to recompute `url`, `archive`, `content` and `positions`. Use `--check` to verify `books.json` without rewriting it.
