#!/usr/bin/env python3
"""
Convert _OceanofPDF.com_*.epub files from ~/Downloads into mac_books.

For each matching epub:
  - Derive a CamelCase title from the filename
  - Create mac_books/<Title>/ if it doesn't exist
  - Copy the epub as mac_books/<Title>/<Title>.epub
  - Extract plain text and write mac_books/<Title>/<Title>.txt

No external dependencies required.
"""

import re
import shutil
import zipfile
from html.parser import HTMLParser
from pathlib import Path

DOWNLOADS = Path.home() / "Downloads"
MAC_BOOKS = Path(__file__).parent  # same dir as this script


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "head"}
    BLOCK_TAGS = {
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "blockquote", "section", "article", "aside",
    }

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self._skip_depth += 1
        if t in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return p.get_text()


# ---------------------------------------------------------------------------
# EPUB parsing
# ---------------------------------------------------------------------------

def extract_text_from_epub(epub_path: Path) -> str:
    with zipfile.ZipFile(epub_path) as zf:
        names = set(zf.namelist())

        # Locate OPF via META-INF/container.xml
        opf_path: str | None = None
        if "META-INF/container.xml" in names:
            container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
            m = re.search(r'full-path="([^"]+\.opf)"', container)
            if m:
                opf_path = m.group(1)

        ordered: list[str] = []
        if opf_path and opf_path in names:
            opf_dir = str(Path(opf_path).parent).rstrip("/")
            opf = zf.read(opf_path).decode("utf-8", errors="replace")

            # manifest: id → href
            manifest = {
                m.group(1): m.group(2)
                for m in re.finditer(r'<item\s[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf)
            }

            # spine reading order
            spine = re.search(r"<spine[^>]*>(.*?)</spine>", opf, re.DOTALL)
            if spine:
                for m in re.finditer(r'idref="([^"]+)"', spine.group(1)):
                    href = manifest.get(m.group(1), "")
                    if not href:
                        continue
                    # Strip any fragment
                    href = href.split("#")[0]
                    full = f"{opf_dir}/{href}" if opf_dir and opf_dir != "." else href
                    # Normalise path separators (epub uses /)
                    full = "/".join(
                        p for p in full.split("/") if p and p != "."
                    )
                    ordered.append(full)

        # Fallback: all html/xhtml sorted by name
        if not ordered:
            ordered = sorted(
                n for n in names if n.lower().endswith((".html", ".xhtml", ".htm"))
            )

        parts: list[str] = []
        seen: set[str] = set()
        for item in ordered:
            if item in seen or item not in names:
                continue
            seen.add(item)
            raw = zf.read(item).decode("utf-8", errors="replace")
            text = _html_to_text(raw)
            if text:
                parts.append(text)

        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def _to_camel_case(raw: str) -> str:
    """'Half_of_a_yellow_sun' → 'HalfOfAYellowSun'"""
    words = re.split(r"[_\s\-]+", raw)
    return "".join(w.capitalize() for w in words if w)


def derive_title(filename: str) -> str:
    """
    '_OceanofPDF.com_Half_of_a_yellow_sun_-_Chimamnda_adichie.epub'
    → 'HalfOfAYellowSun'
    """
    name = re.sub(r"^_OceanofPDF\.com_", "", filename, flags=re.IGNORECASE)
    name = name.removesuffix(".epub")
    # Drop author portion after _-_
    name = re.split(r"_-_", name)[0]
    return _to_camel_case(name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_epub(epub_path: Path) -> None:
    title = derive_title(epub_path.name)
    dest_dir = MAC_BOOKS / title
    dest_dir.mkdir(parents=True, exist_ok=True)

    epub_dest = dest_dir / f"{title}.epub"
    txt_dest  = dest_dir / f"{title}.txt"

    print(f"  Title  : {title}")
    print(f"  Folder : {dest_dir}")

    if epub_dest.exists():
        print(f"  epub   : already exists — skipped")
    else:
        shutil.copy2(epub_path, epub_dest)
        print(f"  epub   : copied → {epub_dest}")

    if txt_dest.exists():
        print(f"  txt    : already exists — skipped")
    else:
        print(f"  txt    : extracting …")
        text = extract_text_from_epub(epub_path)
        txt_dest.write_text(text, encoding="utf-8")
        kb = len(text.encode()) // 1024
        print(f"  txt    : written ({kb:,} KB) → {txt_dest}")


def main() -> None:
    epubs = sorted(DOWNLOADS.glob("_Ocean*.epub"))
    if not epubs:
        print("No _Ocean*.epub files found in ~/Downloads.")
        return

    for epub in epubs:
        print(f"\nProcessing: {epub.name}")
        process_epub(epub)

    print("\nDone.")


if __name__ == "__main__":
    main()
