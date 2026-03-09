#!/usr/bin/env python3
"""Enforce mac_books naming convention.

Folders must be CamelCase (no spaces/punctuation).
Primary media file inside must match the folder name.
Additional assets (cover.jpg, etc.) are left untouched.
"""

import re
from pathlib import Path

MEDIA_EXTENSIONS = {".mp3", ".m4b", ".m4a", ".epub"}
BOOKS_DIR = Path.home() / "Documents" / "mac_books"


def to_camel(name: str) -> str:
    # Strip author portion (everything from " - " onward)
    name = re.split(r" - ", name)[0]
    # Remove apostrophes, replace punctuation/hyphens with spaces
    name = re.sub(r"'", "", name)
    name = re.sub(r"[^a-zA-Z0-9 ]", " ", name)
    # CamelCase each word
    return "".join(word.capitalize() for word in name.split())


def main():
    renamed = 0

    for folder in sorted(BOOKS_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue

        name = folder.name

        # Skip if already compliant (no spaces)
        if " " not in name:
            continue

        new_name = to_camel(name)

        if new_name == name:
            continue

        print(f"Renaming folder: '{name}' → '{new_name}'")

        # Rename primary media file if present
        for ext in MEDIA_EXTENSIONS:
            old_file = folder / f"{name}{ext}"
            if old_file.exists():
                old_file.rename(folder / f"{new_name}{ext}")
                print(f"  File: '{name}{ext}' → '{new_name}{ext}'")

        folder.rename(BOOKS_DIR / new_name)
        renamed += 1

    if renamed == 0:
        print("All folders already compliant.")
    else:
        print(f"Done. {renamed} folder(s) renamed.")


if __name__ == "__main__":
    main()
