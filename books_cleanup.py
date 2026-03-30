#!/usr/bin/env python3
"""
Normalize and import books into ~/Documents/mac_books.

Processing order:
1. Import `_Ocean*.epub` and `_Ocean*.pdf` files from ~/Downloads.
2. Organize loose media files found directly in mac_books/.
3. Rename book folders to canonical CamelCase.
4. Rename primary media/text files inside each book folder to match the folder.

The script is idempotent: existing target files are left in place and reported.
"""

from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import zipfile
from html.parser import HTMLParser
from pathlib import Path

try:
    import pypdf as _pypdf
except ImportError:
    _pypdf = None

DOWNLOADS = Path.home() / "Downloads"
BOOKS_DIR = Path.home() / "Documents" / "mac_books"
APOLLO_DIR = Path("/Volumes/APOLLO/books")
ARCHIVE_DIR_NAME = "_archive"
SPECIAL_DIRS = {ARCHIVE_DIR_NAME, "txt"}
MEDIA_EXTENSIONS = {".epub", ".pdf", ".m4b", ".m4a", ".mp3"}
PRIMARY_EXTENSIONS = MEDIA_EXTENSIONS | {".txt"}
SIDECAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".cue", ".nfo"}
ARCHIVE_EXTENSIONS = MEDIA_EXTENSIONS | {".txt"} | SIDECAR_EXTENSIONS
SPLIT_PARTS = 10
PROTECTED_TRACK_BUCKET_NAMES = {"HarryPotterMixedChapters", "WeDoNotPart"}
HARRY_POTTER_CHAPTER_TITLES = {
    "BagmanAndCrouch",
    "MudbloodsAndMurmurs",
    "TheAdvanceGuard",
    "TheDarkMark",
    "TheDeathEaters",
    "TheDeathlyHallows",
    "TheDuellingClub",
    "TheElderWand",
    "TheFirstTask",
    "TheForbiddenForest",
    "TheForestAgain",
    "TheFourChampions",
    "TheHungarianHorntail",
    "TheKnightBus",
    "TheLeakyCauldron",
    "TheLostDiadem",
    "TheLostProphecy",
    "TheMidnightDuel",
    "TheMissingMirror",
    "TheOtherMinister",
    "ThePhoenixLament",
    "ThePolyjuicePotion",
    "ThePotionsMaster",
    "TheQuidditchFinal",
    "TheRiddleHouse",
    "TheRogueBludger",
    "TheSecondTask",
    "TheSecretRiddle",
    "TheSeerOverheard",
    "TheSevenPotters",
    "TheSilverDoe",
    "TheSlugClub",
    "TheSortingHat",
    "TheThirdTask",
    "TheTriwizardTournament",
    "TheUnbreakableVow",
    "TheUnexpectedTask",
    "TheUnforgivableCurses",
    "TheUnknowableRoom",
    "TheVanishingGlass",
    "TheWhiteTomb",
    "TheWhompingWillow",
    "TheWorstBirthday",
    "TheYuleBall",
}


class Actions:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def log(self, message: str) -> None:
        print(message)

    def mkdir(self, path: Path) -> None:
        if path.exists():
            return
        if self.dry_run:
            self.log(f"[dry-run] mkdir {path}")
            return
        path.mkdir(parents=True, exist_ok=True)

    def copy(self, src: Path, dest: Path) -> bool:
        if dest.exists():
            self.log(f"  exists : {dest}")
            return False
        if self.dry_run:
            self.log(f"[dry-run] copy {src} -> {dest}")
            return True
        shutil.copy2(src, dest)
        self.log(f"  copied : {dest}")
        return True

    def move(self, src: Path, dest: Path) -> bool:
        if dest.exists():
            self.log(f"  exists : {dest}")
            return False
        if self.dry_run:
            self.log(f"[dry-run] move {src} -> {dest}")
            return True
        shutil.move(str(src), dest)
        self.log(f"  moved  : {dest}")
        return True

    def rename(self, src: Path, dest: Path) -> bool:
        if src == dest:
            return False
        if dest.exists():
            self.log(f"  skip   : destination exists {dest}")
            return False
        if self.dry_run:
            self.log(f"[dry-run] rename {src} -> {dest}")
            return True
        src.rename(dest)
        self.log(f"  renamed: {src.name} -> {dest.name}")
        return True

    def rmdir(self, path: Path) -> bool:
        if self.dry_run:
            self.log(f"[dry-run] rmdir {path}")
            return True
        path.rmdir()
        self.log(f"  removed: {path}")
        return True

    def delete(self, path: Path) -> bool:
        if self.dry_run:
            self.log(f"[dry-run] delete {path}")
            return True
        path.unlink()
        self.log(f"  deleted: {path}")
        return True

    def write_text(self, dest: Path, text: str) -> bool:
        if dest.exists():
            self.log(f"  exists : {dest}")
            return False
        if self.dry_run:
            kb = len(text.encode("utf-8")) // 1024
            self.log(f"[dry-run] write {dest} ({kb:,} KB)")
            return True
        dest.write_text(text, encoding="utf-8")
        kb = len(text.encode("utf-8")) // 1024
        self.log(f"  wrote  : {dest} ({kb:,} KB)")
        return True


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
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in self.BLOCK_TAGS:
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


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


def extract_text_from_epub(epub_path: Path) -> str:
    with zipfile.ZipFile(epub_path) as zf:
        names = set(zf.namelist())
        opf_path = None

        if "META-INF/container.xml" in names:
            container = zf.read("META-INF/container.xml").decode("utf-8", errors="replace")
            match = re.search(r'full-path="([^"]+\.opf)"', container)
            if match:
                opf_path = match.group(1)

        ordered: list[str] = []
        if opf_path and opf_path in names:
            opf_dir = str(Path(opf_path).parent).rstrip("/")
            opf = zf.read(opf_path).decode("utf-8", errors="replace")
            manifest = {
                match.group(1): match.group(2)
                for match in re.finditer(r'<item\s[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf)
            }
            spine = re.search(r"<spine[^>]*>(.*?)</spine>", opf, re.DOTALL)
            if spine:
                for match in re.finditer(r'idref="([^"]+)"', spine.group(1)):
                    href = manifest.get(match.group(1), "")
                    if not href:
                        continue
                    href = href.split("#")[0]
                    full = f"{opf_dir}/{href}" if opf_dir and opf_dir != "." else href
                    ordered.append("/".join(part for part in full.split("/") if part and part != "."))

        if not ordered:
            ordered = sorted(
                name for name in names if name.lower().endswith((".html", ".xhtml", ".htm"))
            )

        parts: list[str] = []
        seen: set[str] = set()
        for item in ordered:
            if item in seen or item not in names:
                continue
            seen.add(item)
            raw = zf.read(item).decode("utf-8", errors="replace")
            text = html_to_text(raw)
            if text:
                parts.append(text)

        return "\n\n".join(parts)


def extract_text_from_pdf(pdf_path: Path) -> str:
    if _pypdf is None:
        raise RuntimeError("pypdf is not installed")
    reader = _pypdf.PdfReader(pdf_path)
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def to_camel_case(raw: str) -> str:
    raw = raw.replace("&", " And ")
    raw = raw.replace("'", "")
    raw = re.sub(r"[^A-Za-z0-9]+", " ", raw)
    return "".join(word.capitalize() for word in raw.split())


def canonical_folder_name(name: str) -> str:
    if re.fullmatch(r"[A-Z0-9][A-Za-z0-9]*", name):
        return name
    return to_camel_case(name)


def looks_like_person_name(text: str) -> bool:
    words = [word for word in re.split(r"\s+", text.strip()) if word]
    if not 2 <= len(words) <= 3:
        return False
    return all(re.fullmatch(r"[A-Z][a-z]+(?:['-][A-Z][a-z]+)?", word) for word in words)


def title_segment_score(text: str) -> tuple[int, int]:
    words = [word for word in re.split(r"\s+", text.strip()) if word]
    lower_words = {word.lower() for word in words}
    title_words = {
        "a", "an", "and", "for", "from", "in", "of", "on", "our",
        "the", "to", "we", "what", "with", "your",
    }
    score = 0
    if len(words) >= 3:
        score += 3
    if lower_words & title_words:
        score += 3
    if any(char.isdigit() for char in text):
        score += 1
    if looks_like_person_name(text):
        score -= 4
    return score, len(text)


def choose_title_segment(left: str, right: str) -> str:
    left_score = title_segment_score(left)
    right_score = title_segment_score(right)
    return right if right_score > left_score else left


def title_from_ocean_filename(filename: str) -> str:
    name = re.sub(r"^_OceanofPDF\.com_", "", filename, flags=re.IGNORECASE)
    name = re.sub(r"\.(epub|pdf)$", "", name, flags=re.IGNORECASE)
    name = re.split(r"_-_", name, maxsplit=1)[0]
    return to_camel_case(name)


def title_from_loose_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"\s*\[[^\]]+\]$", "", stem)
    parts = re.split(r"\s+-\s+", stem, maxsplit=1)
    title = choose_title_segment(parts[0], parts[1]) if len(parts) == 2 else parts[0]
    return normalize_archive_candidate(canonical_folder_name(clean_archive_title(title)))


def looks_like_track_file(path: Path) -> bool:
    stem = Path(path.name).stem.strip()
    lower = stem.lower()
    if re.match(r"^\d+\b", stem):
        return True
    if re.search(r"\b(chapter|disc|track|part)\b", lower):
        return True
    if re.search(r"\(\d+\s+of\s+\d+\)", lower):
        return True
    if re.search(r"\b\d{2,3}\b", stem):
        return True
    return False


def looks_like_track_bucket(name: str) -> bool:
    if name in PROTECTED_TRACK_BUCKET_NAMES:
        return False
    lower = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).lower().strip()
    if re.match(r"^\d{1,3}[A-Za-z]", name):
        return True
    if re.match(r"^\d{1,3}\b", name):
        return True
    return bool(re.search(r"\b(chapter|disc|track|part)\b", lower))


def should_organize_loose_file(path: Path, archive_mode: bool) -> bool:
    if not archive_mode:
        return True
    if path.suffix.lower() in {".epub", ".pdf"}:
        return True
    return not looks_like_track_file(path)


def cleaned_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"_dup\d+$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*\[[^\]]+\]$", "", stem)
    return re.sub(r"\s+", " ", stem).strip()


def clean_archive_title(raw: str) -> str:
    raw = re.sub(r"\s*\(Unabridged\)\s*", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*\(\d+\s+of\s+\d+\)\s*$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+Audiobook\s*$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+Disc\s+\d+\s*$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+Chapter\s+\d+\s*$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+-\s+\d+\s*$", "", raw)
    raw = re.sub(r"\s+\d+\s*$", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def normalize_archive_candidate(canonical: str) -> str:
    if re.fullmatch(r"StationElevenPart\d+", canonical):
        return "Station11"
    if canonical.startswith("TheTestaments"):
        return "TheTestaments"
    if canonical in {"Au", "Aue"}:
        return "Aue"
    if canonical.endswith("Audiobook"):
        return canonical[:-9] or canonical
    return canonical


def existing_folder_map(root: Path) -> dict[str, str]:
    return {
        canonical_folder_name(path.name).lower(): path.name
        for path in root.iterdir()
        if is_book_folder(path)
    }


def archive_title_candidates(path: Path) -> list[tuple[str, bool]]:
    stem = cleaned_stem(path.name)
    candidates: list[tuple[str, bool]] = []

    def add(raw: str | None, strong: bool) -> None:
        if not raw:
            return
        canonical = normalize_archive_candidate(canonical_folder_name(clean_archive_title(raw)))
        if not canonical or canonical.isdigit():
            return
        entry = (canonical, strong)
        if entry not in candidates:
            candidates.append(entry)

    add(stem, False)

    parts = re.split(r"\s+-\s+", stem)
    if len(parts) == 2 and (looks_like_person_name(parts[0]) or looks_like_person_name(parts[1])):
        add(choose_title_segment(clean_archive_title(parts[0]), clean_archive_title(parts[1])), True)

    if match := re.search(r"(The Testaments)", stem, re.IGNORECASE):
        add(match.group(1), True)
    if match := re.match(r"^(Recursion)\s+\d+$", stem, re.IGNORECASE):
        add(match.group(1), True)
    if match := re.match(r"^(.+?)\s+\(\d+\s+of\s+\d+\)$", stem, re.IGNORECASE):
        add(match.group(1), True)
    if match := re.match(r"^(.+?)\s+-\s+\d+$", stem, re.IGNORECASE):
        add(match.group(1), True)
    if match := re.match(r"^(.+?)\s+-\s+Chapter\s+\d+$", stem, re.IGNORECASE):
        add(match.group(1), True)
    if match := re.match(r"^\d+\s+-\s+(.+?)\s+-\s+(Introduction|Chapter\s+\d+|Historical Notes.*)$", stem, re.IGNORECASE):
        add(match.group(1), True)
    if match := re.match(r"^(.+?)\s+\d+$", stem, re.IGNORECASE):
        add(match.group(1), True)

    return candidates


def infer_archive_folder(root: Path, path: Path) -> Path | None:
    folders = existing_folder_map(root)
    candidates = archive_title_candidates(path)

    lower = cleaned_stem(path.name).lower()
    if (
        re.match(r"^\d{3}\s+part\b", lower)
        or "afterword by the author" in lower
        or "valerie martin" in lower
    ) and "the testaments" not in lower:
        existing = folders.get("hmtale")
        if existing:
            return root / existing

    for candidate, strong in candidates:
        if not strong:
            continue
        existing = folders.get(candidate.lower())
        if existing:
            return root / existing

    for candidate, _strong in candidates:
        existing = folders.get(candidate.lower())
        if existing:
            return root / existing

    for candidate, strong in candidates:
        if strong:
            return root / candidate

    return None


def organize_archive_files(root: Path, actions: Actions) -> None:
    loose_files = sorted(
        path for path in root.iterdir()
        if path.is_file() and not is_ignored_name(path.name) and path.suffix.lower() in ARCHIVE_EXTENSIONS
    )
    for source in loose_files:
        destination_folder = infer_archive_folder(root, source)
        if destination_folder is None:
            continue

        actions.mkdir(destination_folder)
        keep_original_name = (
            looks_like_track_file(source)
            or looks_like_track_bucket(cleaned_stem(source.name))
            or source.suffix.lower() == ".cue"
        )
        if source.suffix.lower() == ".txt":
            destination_name = source.name if keep_original_name else f"{destination_folder.name}.txt"
        elif source.suffix.lower() in SIDECAR_EXTENSIONS and source.suffix.lower() != ".cue":
            destination_name = source.name if keep_original_name else f"{destination_folder.name}{source.suffix.lower()}"
        else:
            destination_name = source.name if keep_original_name else f"{destination_folder.name}{source.suffix.lower()}"

        destination = destination_folder / destination_name
        print(f"\nOrganizing archive file: {source.name}")
        print(f"  folder : {destination_folder.name}")
        actions.move(source, destination)


def move_contents_to_folder(source_dir: Path, destination_dir: Path, actions: Actions) -> None:
    if not source_dir.exists() or source_dir == destination_dir:
        return
    actions.mkdir(destination_dir)
    print(f"\nRepairing folder: {source_dir.name} -> {destination_dir.name}")
    for entry in sorted(source_dir.iterdir()):
        if is_ignored_name(entry.name):
            continue
        destination = destination_dir / entry.name
        if entry.is_file():
            actions.move(entry, destination)
    remaining = [entry for entry in source_dir.iterdir() if not is_ignored_name(entry.name)]
    if not remaining:
        actions.rmdir(source_dir)


def repair_archive_aliases(root: Path, actions: Actions) -> None:
    alias_map = {
        "TheTestamentsHistoricalNotes": "TheTestaments",
        "TheFeminineMystiqueUnabridged": "TheFeminineMystique",
    }
    for source_name, target_name in alias_map.items():
        source_dir = root / source_name
        if source_dir.exists():
            move_contents_to_folder(source_dir, root / target_name, actions)

    for source_dir in sorted(path for path in root.iterdir() if path.is_dir() and not is_ignored_name(path.name)):
        target_name = None
        if re.fullmatch(r"StationElevenPart\d+", source_dir.name):
            target_name = "Station11"
        elif re.fullmatch(r"\d{3}Part.*", source_dir.name):
            target_name = "HmTale"
        if target_name:
            move_contents_to_folder(source_dir, root / target_name, actions)

    for source in sorted(path for path in root.iterdir() if path.is_file() and not is_ignored_name(path.name)):
        target_name = None
        if re.fullmatch(r"StationElevenPart\d+\.mp3", source.name):
            target_name = "Station11"
        elif re.fullmatch(r"\d{3}Part.*\.(mp3|jpg|png|jpeg)$", source.name):
            target_name = "HmTale"
        elif source.name in {"047 Historical Notes (Full Cast).mp3", "048 New Afterword by the Author.mp3", "049 Essay by Valerie Martin.mp3"}:
            target_name = "HmTale"
        if target_name:
            actions.mkdir(root / target_name)
            print(f"\nRepairing file: {source.name} -> {target_name}")
            actions.move(source, root / target_name / source.name)


def same_file_contents(a: Path, b: Path) -> bool:
    try:
        if a.is_dir() or b.is_dir():
            return False
        return filecmp.cmp(a, b, shallow=False)
    except FileNotFoundError:
        return False


def place_or_delete_duplicate(source: Path, destination: Path, actions: Actions) -> None:
    if destination.exists():
        if same_file_contents(source, destination):
            actions.delete(source)
        return
    actions.move(source, destination)


def explicit_archive_bucket(root: Path, source: Path) -> Path | None:
    name = source.name
    stem = source.stem
    if re.fullmatch(r"StationElevenPart\d+\.mp3", name):
        return root / "Station11"
    if re.fullmatch(r"\d{3}Part.*\.(mp3|jpg|png|jpeg)$", name):
        return root / "HmTale"
    if name in {"048 New Afterword by the Author.mp3", "049 Essay by Valerie Martin.mp3"}:
        return root / "HmTale"
    if re.fullmatch(r"Recursion \d+\.mp3", name):
        return root / "Recursion"
    if name == "MaddAddam.mp3" or re.fullmatch(r"Margaret Attwood - MaddAddam Disc \d+\.mp3", name):
        return root / "MaddAddam"
    if name == "TheYearOfTheFlood.mp3" or re.fullmatch(r"Margaret Atwood - The Year of the Flood Disc \d+\.mp3", name):
        return root / "TheYearOfTheFlood"
    if name in {"WeDoNotPart.epub", "WeDoNotPart.txt"}:
        return root / "WeDoNotPart"
    if name == "And So I Roar.epub":
        return root / "AndSoIRoar"
    if name == "Birnam_Wood.epub":
        return root / "BirnamWood"
    if name in {"Cover.jpg", "EmbeddedCover.jpg"}:
        return root / "Cover"
    if re.fullmatch(r"\d+\s+-\s+.*\.mp3", name) or re.fullmatch(r"\d+\.txt", name):
        return root / "HarryPotterMixedChapters"
    if stem in HARRY_POTTER_CHAPTER_TITLES:
        return root / "HarryPotterMixedChapters"
    if name in {"TheDeathdayParty.mp3", "TheDursleysDeparting.mp3", "TheBurrow.mp3", "TheInvitation.mp3", "TheLettersFromNoOne.mp3", "TheWhompingWillow.mp3"}:
        return root / "HarryPotterMixedChapters"
    return None


def is_single_file_harry_potter_chapter_dir(path: Path) -> bool:
    if not path.is_dir() or path.name not in HARRY_POTTER_CHAPTER_TITLES:
        return False
    files = [child for child in path.iterdir() if child.is_file() and not is_ignored_name(child.name)]
    subdirs = [child for child in path.iterdir() if child.is_dir()]
    if len(files) != 1 or subdirs:
        return False
    only_file = files[0]
    return only_file.suffix.lower() == ".mp3" and only_file.stem == path.name


def repair_harry_potter_chapter_dirs(root: Path, actions: Actions) -> None:
    target = root / "HarryPotterMixedChapters"
    chapter_dirs = sorted(
        path for path in root.iterdir()
        if is_single_file_harry_potter_chapter_dir(path)
    )
    for chapter_dir in chapter_dirs:
        chapter_file = next(
            child for child in chapter_dir.iterdir()
            if child.is_file() and not is_ignored_name(child.name)
        )
        actions.mkdir(target)
        destination = target / chapter_file.name
        print(f"\nRepairing Harry Potter chapter folder: {chapter_dir.name}")
        place_or_delete_duplicate(chapter_file, destination, actions)
        remaining = list(chapter_dir.iterdir()) if chapter_dir.exists() else []
        if not remaining:
            actions.rmdir(chapter_dir)


def is_harry_potter_bucket_alias(path: Path, root: Path) -> bool:
    if not path.is_dir() or is_ignored_name(path.name) or path.name == "HarryPotterMixedChapters":
        return False
    files = [child for child in path.iterdir() if child.is_file() and not is_ignored_name(child.name)]
    subdirs = [child for child in path.iterdir() if child.is_dir()]
    if subdirs or len(files) < 10:
        return False
    hp_bucket = root / "HarryPotterMixedChapters"
    matched = 0
    for child in files:
        if explicit_archive_bucket(root, child) == hp_bucket:
            matched += 1
    return matched == len(files)


def repair_harry_potter_bucket_aliases(root: Path, actions: Actions) -> None:
    target = root / "HarryPotterMixedChapters"
    aliases = sorted(
        path for path in root.iterdir()
        if is_harry_potter_bucket_alias(path, root)
    )
    for alias in aliases:
        actions.mkdir(target)
        print(f"\nRepairing Harry Potter bucket alias: {alias.name}")
        files = sorted(child for child in alias.iterdir() if child.is_file() and not is_ignored_name(child.name))
        for source in files:
            destination = target / source.name
            place_or_delete_duplicate(source, destination, actions)
        remaining = list(alias.iterdir()) if alias.exists() else []
        if not remaining:
            actions.rmdir(alias)


def organize_archive_root_leftovers(root: Path, actions: Actions) -> None:
    leftovers = sorted(
        path for path in root.iterdir()
        if path.is_file() and not is_ignored_name(path.name)
    )
    for source in leftovers:
        bucket = explicit_archive_bucket(root, source)
        if bucket is None:
            continue
        actions.mkdir(bucket)
        destination = bucket / source.name
        print(f"\nBucketing leftover: {source.name}")
        print(f"  folder : {bucket.name}")
        place_or_delete_duplicate(source, destination, actions)


def flatten_track_buckets(root: Path, actions: Actions) -> None:
    buckets = sorted(
        path for path in root.iterdir()
        if path.is_dir() and not is_ignored_name(path.name) and looks_like_track_bucket(path.name)
    )
    for bucket in buckets:
        files = sorted(path for path in bucket.iterdir() if path.is_file() and not is_ignored_name(path.name))
        subdirs = [path for path in bucket.iterdir() if path.is_dir()]
        if subdirs:
            continue

        print(f"\nFlattening track bucket: {bucket.name}")
        for source in files:
            destination = root / source.name
            if destination.exists():
                destination = root / f"{bucket.name}{source.suffix.lower()}"
            actions.move(source, destination)

        remaining = list(bucket.iterdir()) if bucket.exists() else []
        if not remaining:
            actions.rmdir(bucket)


def is_ignored_name(name: str) -> bool:
    return name.startswith(".")


def is_special_dir(path: Path) -> bool:
    return path.name.lower() in SPECIAL_DIRS


def is_book_folder(path: Path) -> bool:
    return (
        path.is_dir()
        and not is_ignored_name(path.name)
        and not is_special_dir(path)
        and not looks_like_track_bucket(path.name)
    )


def import_ocean_downloads(actions: Actions) -> None:
    for source in sorted(DOWNLOADS.glob("_Ocean*.epub")) + sorted(DOWNLOADS.glob("_Ocean*.pdf")):
        title = title_from_ocean_filename(source.name)
        folder = BOOKS_DIR / title
        destination = folder / f"{title}{source.suffix.lower()}"
        txt_destination = folder / f"{title}.txt"

        print(f"\nImporting: {source.name}")
        print(f"  title  : {title}")
        actions.mkdir(folder)
        copied = actions.copy(source, destination)

        if txt_destination.exists():
            print(f"  exists : {txt_destination}")
            continue

        try:
            text_source = destination if destination.exists() else source
            text = (
                extract_text_from_epub(text_source)
                if source.suffix.lower() == ".epub"
                else extract_text_from_pdf(text_source)
            )
        except RuntimeError as exc:
            print(f"  skip   : {exc}")
            continue

        if copied or destination.exists() or actions.dry_run:
            actions.write_text(txt_destination, text)


def organize_loose_files(root: Path, actions: Actions, archive_mode: bool = False) -> None:
    loose_files = sorted(
        path for path in root.iterdir()
        if (
            path.is_file()
            and not is_ignored_name(path.name)
            and path.suffix.lower() in MEDIA_EXTENSIONS
            and should_organize_loose_file(path, archive_mode)
        )
    )
    for source in loose_files:
        title = title_from_loose_filename(source.name)
        folder = root / title
        destination = folder / f"{title}{source.suffix.lower()}"
        txt_destination = folder / f"{title}.txt"

        print(f"\nOrganizing loose file: {source.name}")
        print(f"  title  : {title}")
        actions.mkdir(folder)
        moved = actions.move(source, destination)

        if source.suffix.lower() != ".pdf" or txt_destination.exists():
            if source.suffix.lower() == ".pdf" and txt_destination.exists():
                print(f"  exists : {txt_destination}")
            continue

        try:
            text = extract_text_from_pdf(destination if destination.exists() else source)
        except RuntimeError as exc:
            print(f"  skip   : {exc}")
            continue

        if moved or actions.dry_run:
            actions.write_text(txt_destination, text)


def normalize_folder_name(folder: Path, actions: Actions) -> tuple[Path, str]:
    if folder.name in PROTECTED_TRACK_BUCKET_NAMES:
        return folder, folder.name
    canonical = infer_folder_title(folder) or canonical_folder_name(folder.name)
    if not canonical or canonical == folder.name:
        return folder, folder.name

    destination = folder.with_name(canonical)
    print(f"\nNormalizing folder: {folder.name}")
    if actions.rename(folder, destination):
        return (folder if actions.dry_run else destination), canonical
    return folder, folder.name


def infer_folder_title(folder: Path) -> str | None:
    candidates: dict[str, int] = {}
    hyphen_candidates: dict[str, int] = {}
    for path in folder.iterdir():
        if not path.is_file() or is_ignored_name(path.name):
            continue
        suffix = path.suffix.lower()
        if suffix not in MEDIA_EXTENSIONS and suffix not in SIDECAR_EXTENSIONS:
            continue

        candidate = None
        stem = Path(path.name).stem
        if " - " in stem:
            candidate = title_from_loose_filename(path.name)
            hyphen_candidates[candidate] = hyphen_candidates.get(candidate, 0) + 1
        elif suffix in MEDIA_EXTENSIONS:
            candidate = canonical_folder_name(stem)
            if suffix in {".mp3", ".m4b", ".m4a"}:
                candidate = normalize_archive_candidate(candidate)

        if not candidate:
            continue
        candidates[candidate] = candidates.get(candidate, 0) + 1

    if not candidates:
        return None

    best_candidate = max(candidates.items(), key=lambda item: (item[1], len(item[0])))[0]
    if best_candidate == folder.name:
        return best_candidate

    if normalize_archive_candidate(best_candidate) == normalize_archive_candidate(folder.name):
        return folder.name

    if re.fullmatch(r"[A-Z0-9][A-Za-z0-9]*", folder.name):
        if not hyphen_candidates:
            return None
        best_hyphen_candidate = max(hyphen_candidates.items(), key=lambda item: (item[1], len(item[0])))[0]
        return best_hyphen_candidate if best_hyphen_candidate != folder.name else folder.name

    if candidates[best_candidate] >= 2:
        return best_candidate

    return None


def rename_single_primary_file(folder: Path, extension: str, target_stem: str, actions: Actions) -> None:
    target = folder / f"{target_stem}{extension}"
    if target.exists():
        return

    candidates = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == extension]
    if len(candidates) != 1:
        return

    candidate = candidates[0]
    if candidate == target:
        return
    if canonical_folder_name(candidate.stem) != target_stem:
        return

    actions.rename(candidate, target)


def normalize_folder_contents(folder: Path, target_stem: str, actions: Actions) -> None:
    print(f"\nNormalizing contents: {folder.name}")
    for extension in sorted(PRIMARY_EXTENSIONS):
        rename_single_primary_file(folder, extension, target_stem, actions)


def normalize_existing_library(actions: Actions) -> None:
    normalize_library_root(BOOKS_DIR, actions)


def normalize_library_root(root: Path, actions: Actions) -> None:
    folders = [path for path in sorted(root.iterdir()) if is_book_folder(path)]
    rename_results = [normalize_folder_name(folder, actions) for folder in folders]
    for folder_for_contents, target_stem in rename_results:
        if folder_for_contents.exists():
            normalize_folder_contents(folder_for_contents, target_stem, actions)


def split_text(text: str, n_parts: int = SPLIT_PARTS) -> list[str]:
    base, remainder = divmod(len(text), n_parts)
    parts: list[str] = []
    start = 0
    for idx in range(n_parts):
        size = base + (1 if idx < remainder else 0)
        end = start + size
        parts.append(text[start:end])
        start = end
    return parts


def write_book_txt_and_split(book_dir: Path, actions: Actions) -> None:
    epub_path = book_dir / f"{book_dir.name}.epub"
    if not epub_path.exists():
        return

    txt_path = book_dir / f"{book_dir.name}.txt"
    source_text: str | None = None
    if not txt_path.exists():
        print(f"\nExtracting text: {book_dir.name}")
        source_text = extract_text_from_epub(epub_path)
        actions.write_text(txt_path, source_text)

    if source_text is None:
        if not txt_path.exists():
            return
        source_text = txt_path.read_text(encoding="utf-8")

    parts = split_text(source_text, SPLIT_PARTS)
    split_dir = book_dir / "split"
    actions.mkdir(split_dir)

    existing_parts = sorted(split_dir.glob("[0-9][0-9].txt")) if split_dir.exists() else []
    expected_names = {f"{idx:02d}.txt" for idx in range(1, SPLIT_PARTS + 1)}
    for old_file in existing_parts:
        if old_file.name not in expected_names:
            continue
        actions.delete(old_file)

    for idx, part in enumerate(parts, start=1):
        out_path = split_dir / f"{idx:02d}.txt"
        if actions.dry_run:
            kb = len(part.encode("utf-8")) // 1024
            actions.log(f"[dry-run] write {out_path} ({kb:,} KB)")
            continue
        out_path.write_text(part, encoding="utf-8")
        kb = len(part.encode("utf-8")) // 1024
        actions.log(f"  wrote  : {out_path} ({kb:,} KB)")


def build_mac_book_txt_and_splits(actions: Actions) -> None:
    for book_dir in sorted(path for path in BOOKS_DIR.iterdir() if is_book_folder(path)):
        write_book_txt_and_split(book_dir, actions)


def resolve_destination(destination_root: Path, name: str, is_dir: bool) -> Path:
    direct = destination_root / name
    if direct.exists():
        return direct
    if not destination_root.exists():
        return direct

    lowered = name.lower()
    for child in destination_root.iterdir():
        if child.name.lower() != lowered:
            continue
        if child.is_dir() == is_dir:
            return child
    return direct


def resolve_special_dir(root: Path, name: str) -> Path:
    return resolve_destination(root, name, True)


def sync_directory(source: Path, destination: Path, actions: Actions) -> None:
    actions.mkdir(destination)
    entries = sorted(path for path in source.iterdir() if not is_ignored_name(path.name))
    for entry in entries:
        target = resolve_destination(destination, entry.name, entry.is_dir())
        if entry.is_dir():
            sync_directory(entry, target, actions)
            continue

        if target.exists():
            continue
        actions.copy(entry, target)


def same_directory_contents(source: Path, destination: Path) -> bool:
    if not source.is_dir() or not destination.is_dir():
        return False

    source_entries = sorted(path for path in source.iterdir() if not is_ignored_name(path.name))
    destination_entries = sorted(path for path in destination.iterdir() if not is_ignored_name(path.name))
    if len(source_entries) != len(destination_entries):
        return False

    destination_map = {path.name.lower(): path for path in destination_entries}
    if len(destination_map) != len(destination_entries):
        return False

    for source_entry in source_entries:
        destination_entry = destination_map.get(source_entry.name.lower())
        if destination_entry is None or source_entry.is_dir() != destination_entry.is_dir():
            return False
        if source_entry.is_dir():
            if not same_directory_contents(source_entry, destination_entry):
                return False
        elif not same_file_contents(source_entry, destination_entry):
            return False
    return True


def remove_empty_directory(path: Path, actions: Actions) -> None:
    if not path.exists() or not path.is_dir():
        return
    remaining = [child for child in path.iterdir() if not is_ignored_name(child.name)]
    if remaining:
        return
    ignored_children = [child for child in path.iterdir()]
    for child in ignored_children:
        actions.delete(child)
    actions.rmdir(path)


def merge_directory_into(source: Path, destination: Path, actions: Actions) -> None:
    actions.mkdir(destination)
    for entry in sorted(path for path in source.iterdir() if not is_ignored_name(path.name)):
        target = resolve_destination(destination, entry.name, entry.is_dir())
        if entry.is_dir():
            merge_directory_into(entry, target, actions)
            remove_empty_directory(entry, actions)
            continue
        place_or_delete_duplicate(entry, target, actions)
    remove_empty_directory(source, actions)


def relocate_existing_entry(source: Path, destination: Path, actions: Actions) -> None:
    if not source.exists():
        return
    if source.is_dir():
        merge_directory_into(source, destination, actions)
        return
    actions.mkdir(destination.parent)
    place_or_delete_duplicate(source, destination, actions)


def sync_entry_with_archive_awareness(
    source: Path,
    preferred_root: Path,
    alternate_root: Path,
    actions: Actions,
) -> None:
    preferred_target = resolve_destination(preferred_root, source.name, source.is_dir())
    alternate_target = resolve_destination(alternate_root, source.name, source.is_dir())
    archive_preferred = preferred_root.name == ARCHIVE_DIR_NAME

    if source.is_dir():
        if preferred_target.exists() and same_directory_contents(source, preferred_target):
            return
        if alternate_target.exists() and same_directory_contents(source, alternate_target):
            if archive_preferred:
                print(f"\nArchiving on APOLLO: {source.name}")
                relocate_existing_entry(alternate_target, preferred_target, actions)
            return
        if archive_preferred and alternate_target.exists():
            print(f"\nArchiving on APOLLO: {source.name}")
            relocate_existing_entry(alternate_target, preferred_target, actions)
        sync_directory(source, preferred_target, actions)
        return

    if preferred_target.exists() and same_file_contents(source, preferred_target):
        return
    if alternate_target.exists() and same_file_contents(source, alternate_target):
        if archive_preferred:
            print(f"\nArchiving file on APOLLO: {source.name}")
            relocate_existing_entry(alternate_target, preferred_target, actions)
        return
    if archive_preferred and alternate_target.exists():
        print(f"\nArchiving file on APOLLO: {source.name}")
        relocate_existing_entry(alternate_target, preferred_target, actions)
    actions.mkdir(preferred_root)
    if not preferred_target.exists():
        actions.copy(source, preferred_target)


def sync_root_entries(source_root: Path, preferred_root: Path, alternate_root: Path, actions: Actions) -> None:
    if not source_root.exists():
        return
    entries = sorted(path for path in source_root.iterdir() if not is_ignored_name(path.name))
    for entry in entries:
        if source_root == BOOKS_DIR and entry.name == ARCHIVE_DIR_NAME:
            continue
        sync_entry_with_archive_awareness(entry, preferred_root, alternate_root, actions)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import and normalize ~/Documents/mac_books")
    parser.add_argument("--dry-run", action="store_true", help="Show intended changes without modifying files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    actions = Actions(dry_run=args.dry_run)

    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    APOLLO_DIR.mkdir(parents=True, exist_ok=True)

    import_ocean_downloads(actions)
    organize_loose_files(BOOKS_DIR, actions)
    normalize_library_root(BOOKS_DIR, actions)
    build_mac_book_txt_and_splits(actions)
    mac_archive = resolve_special_dir(BOOKS_DIR, ARCHIVE_DIR_NAME)
    if mac_archive.exists():
        repair_archive_aliases(mac_archive, actions)
        repair_harry_potter_chapter_dirs(mac_archive, actions)
        repair_harry_potter_bucket_aliases(mac_archive, actions)
        flatten_track_buckets(mac_archive, actions)
        organize_archive_files(mac_archive, actions)
        organize_archive_root_leftovers(mac_archive, actions)
        normalize_library_root(mac_archive, actions)
    organize_loose_files(APOLLO_DIR, actions)
    normalize_library_root(APOLLO_DIR, actions)
    apollo_archive = resolve_special_dir(APOLLO_DIR, ARCHIVE_DIR_NAME)
    if apollo_archive.exists():
        repair_archive_aliases(apollo_archive, actions)
        repair_harry_potter_chapter_dirs(apollo_archive, actions)
        repair_harry_potter_bucket_aliases(apollo_archive, actions)
        flatten_track_buckets(apollo_archive, actions)
        organize_archive_files(apollo_archive, actions)
        organize_archive_root_leftovers(apollo_archive, actions)
        normalize_library_root(apollo_archive, actions)
    sync_root_entries(BOOKS_DIR, APOLLO_DIR, apollo_archive, actions)
    sync_root_entries(mac_archive, apollo_archive, APOLLO_DIR, actions)

    print("\nDone.")


if __name__ == "__main__":
    main()
