"""VK Archive: Attachments Downloader.

A small desktop utility for processing exported VK chat archives. The app finds
media links inside `messages*.html`, downloads selected attachment types, and
rewrites the exported HTML so the saved media can be opened offline.

The program is intentionally local-first: it does not upload archive data
anywhere and only makes direct requests to media URLs already present in the VK
archive.
"""

from __future__ import annotations

import ctypes
import hashlib
import html as html_lib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Literal
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# =============================================================================
# Application metadata
# =============================================================================

APP_NAME = "VK Archive: Attachments Downloader"
AUTHOR_NAME = "Robert Musalimov"
AUTHOR_LINK = "https://app.notion.com/p/musalimov/27bdd5f3244a8068ad5cd9cfe274df96"

# =============================================================================
# Regular expressions and constants
# =============================================================================

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
WEBP_GIF_EXTENSIONS = (".webp", ".gif")
AUDIO_EXTENSIONS = (".ogg", ".mp3", ".m4a", ".aac", ".wav")
KNOWN_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS + WEBP_GIF_EXTENSIONS + AUDIO_EXTENSIONS

REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT_SECONDS = 40
DOWNLOAD_CHUNK_SIZE = 1024 * 256
MANIFEST_SAVE_INTERVAL = 100

URL_RE = re.compile(r'https?://[^\s"\'<>]+')
BACKGROUND_URL_RE = re.compile(r'url\(["\']?(https?://[^)"\']+)["\']?\)')
MESSAGE_NUMBER_RE = re.compile(r"messages(\d+)")

INVALID_WINDOWS_FILENAME_CHARS = '<>:"/\\|?*'

MediaType = Literal["image", "audio"]
LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]
ManifestItem = dict[str, object]
Manifest = dict[str, ManifestItem]


# =============================================================================
# Platform helpers
# =============================================================================


def enable_dpi_awareness() -> None:
    """Make the Tkinter window sharper on Windows high-DPI displays."""
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            # DPI awareness is a visual improvement only; the app can continue.
            pass



def get_dpi_factor(root: tk.Tk) -> float:
    """Return the current Tk scaling factor compared with Windows 100% scale."""
    try:
        tk_scaling = float(root.tk.call("tk", "scaling"))
        return max(1.0, tk_scaling / (96 / 72))
    except Exception:
        return 1.0



def open_folder(path: Path) -> None:
    """Open a folder in the system file manager."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])



def sanitize_folder_name(name: str) -> str:
    """Return a filesystem-safe folder name for the output directory."""
    cleaned = "".join("_" if char in INVALID_WINDOWS_FILENAME_CHARS else char for char in name)
    cleaned = cleaned.strip().strip(".")
    return cleaned or "VK_Archive_Output"


# =============================================================================
# Processor configuration
# =============================================================================


@dataclass(frozen=True)
class ProcessorConfig:
    """User-selected settings for one processing run."""

    archive_root: Path
    dialog_id: str
    output_root: Path
    output_name: str
    download_images: bool = True
    download_webp_gif: bool = True
    download_audio: bool = True
    inline_media: bool = True
    delay_seconds: float = 0.15

    @property
    def output_dir(self) -> Path:
        return self.output_root / sanitize_folder_name(self.output_name)

    @property
    def chat_folder(self) -> Path:
        return self.archive_root / "messages" / self.dialog_id

    @property
    def style_css(self) -> Path:
        return self.archive_root / "style.css"

    @property
    def allowed_media_types(self) -> set[MediaType]:
        allowed: set[MediaType] = set()

        if self.download_images or self.download_webp_gif:
            allowed.add("image")
        if self.download_audio:
            allowed.add("audio")

        return allowed


# =============================================================================
# VK archive processing logic
# =============================================================================


class VKArchiveProcessor:
    """Process a single VK chat archive folder."""

    def __init__(
        self,
        config: ProcessorConfig,
        log_callback: LogCallback,
        progress_callback: ProgressCallback,
    ) -> None:
        self.config = config
        self.log = log_callback
        self.progress = progress_callback
        self.existing_files_index: dict[str, ManifestItem] = {}

    # ---------------------------------------------------------------------
    # Small utilities
    # ---------------------------------------------------------------------

    @staticmethod
    def read_html(path: Path) -> str:
        """Read a VK archive HTML file using UTF-8 with CP1251 fallback."""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="cp1251", errors="replace")

    @staticmethod
    def sha1(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    @staticmethod
    def natural_message_sort_key(path: Path) -> int:
        """Sort `messages.html`, `messages1.html`, `messages2.html`, ... naturally."""
        match = MESSAGE_NUMBER_RE.search(path.stem)
        return int(match.group(1)) if match else 0

    @staticmethod
    def clean_url(url: str) -> str:
        return html_lib.unescape(url).strip().rstrip(").,;")

    @staticmethod
    def save_json(path: Path, data: object) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_manifest(self, manifest: Manifest) -> None:
        self.save_json(self.config.output_dir / "manifest.json", manifest)

    # ---------------------------------------------------------------------
    # CSS
    # ---------------------------------------------------------------------

    def copy_style_css(self) -> None:
        """Copy VK's original stylesheet if it exists in the archive root."""
        if not self.config.style_css.exists():
            self.log(f"WARNING: style.css не найден: {self.config.style_css}")
            return

        destination = self.config.output_dir / "style.css"
        shutil.copy2(self.config.style_css, destination)
        self.log(f"CSS скопирован: {destination}")

    @staticmethod
    def ensure_css_link(soup: BeautifulSoup) -> None:
        """Ensure that a rewritten HTML page links to the copied VK stylesheet."""
        head = soup.find("head")

        if head is None:
            html_tag = soup.find("html")
            head = soup.new_tag("head")

            if html_tag is not None:
                html_tag.insert(0, head)
            else:
                soup.insert(0, head)

        if soup.find("link", href="../style.css") is None:
            css_link = soup.new_tag("link", rel="stylesheet", href="../style.css")
            head.append(css_link)

    # ---------------------------------------------------------------------
    # Manifest
    # ---------------------------------------------------------------------

    def load_manifest(self) -> Manifest:
        """Load the previous manifest so repeated runs do not redownload files."""
        manifest_path = self.config.output_dir / "manifest.json"

        if not manifest_path.exists():
            return {}

        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as error:
            self.log(f"WARNING: не удалось прочитать manifest.json: {error}")
            return {}

        if not isinstance(raw_manifest, dict):
            self.log("WARNING: manifest.json имеет неожиданный формат")
            return {}

        manifest: Manifest = {}
        allowed_media_types = self.config.allowed_media_types

        for url, item in raw_manifest.items():
            if not isinstance(url, str) or not isinstance(item, dict):
                continue

            if item.get("media_type") not in allowed_media_types:
                continue

            local_path = Path(str(item.get("local_path", "")))

            if local_path.exists() and local_path.stat().st_size > 0:
                manifest[url] = item

        self.log(f"Загружен старый manifest: {len(manifest)} полезных записей")
        return manifest

    # ---------------------------------------------------------------------
    # URL extraction and filtering
    # ---------------------------------------------------------------------

    def url_matches_enabled_types(self, url: str) -> bool:
        """Return True when a URL has an enabled media extension."""
        lower_url = url.lower()

        if self.config.download_images and any(ext in lower_url for ext in IMAGE_EXTENSIONS):
            return True

        if self.config.download_webp_gif and any(ext in lower_url for ext in WEBP_GIF_EXTENSIONS):
            return True

        if self.config.download_audio and any(ext in lower_url for ext in AUDIO_EXTENSIONS):
            return True

        return False

    def is_interesting_url(self, url: str) -> bool:
        """Return True for media URLs that should be considered for download.

        The app intentionally ignores common non-target links such as VK document
        pages, video pages, and unknown binary files. Final validation is still
        done after the HTTP response by checking Content-Type and extension.
        """
        if not url or not url.startswith("http"):
            return False

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        lower_url = url.lower()

        if "userapi.com" in host and self.config.allowed_media_types:
            return True

        if self.config.download_webp_gif and (
            "sticker" in lower_url or "vk.com/sticker" in lower_url
        ):
            return True

        return self.url_matches_enabled_types(url)

    def extract_urls_from_html(self, html_path: Path) -> set[str]:
        """Extract potentially downloadable media URLs from one HTML page."""
        html = self.read_html(html_path)
        soup = BeautifulSoup(html, "html.parser")
        urls: set[str] = set()

        for tag in soup.find_all(["a", "img", "audio", "source"]):
            for attr in ("href", "src", "data-src"):
                value = tag.get(attr)

                if not value:
                    continue

                url = self.clean_url(value)

                if self.is_interesting_url(url):
                    urls.add(url)

        for tag in soup.find_all(style=True):
            style_value = tag.get("style", "")

            for value in BACKGROUND_URL_RE.findall(style_value):
                url = self.clean_url(value)

                if self.is_interesting_url(url):
                    urls.add(url)

        for value in URL_RE.findall(html):
            url = self.clean_url(value)

            if self.is_interesting_url(url):
                urls.add(url)

        return urls

    # ---------------------------------------------------------------------
    # Media type and extension detection
    # ---------------------------------------------------------------------

    def guess_media_type(self, content_type: str, url: str) -> MediaType | None:
        """Determine the output media type or return None if it is disabled/unknown."""
        content_type = content_type.split(";")[0].strip().lower()
        lower_url = url.lower()

        if content_type.startswith("image/"):
            if content_type in {"image/jpeg", "image/png"} and self.config.download_images:
                return "image"
            if content_type in {"image/webp", "image/gif"} and self.config.download_webp_gif:
                return "image"
            return None

        if content_type.startswith("audio/"):
            return "audio" if self.config.download_audio else None

        if self.config.download_images and any(ext in lower_url for ext in IMAGE_EXTENSIONS):
            return "image"

        if self.config.download_webp_gif and any(ext in lower_url for ext in WEBP_GIF_EXTENSIONS):
            return "image"

        if self.config.download_audio and any(ext in lower_url for ext in AUDIO_EXTENSIONS):
            return "audio"

        return None

    @staticmethod
    def guess_extension(content_type: str, url: str) -> str | None:
        """Return the most likely local file extension."""
        content_type = content_type.split(";")[0].strip().lower()

        content_type_to_ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "audio/ogg": ".ogg",
            "application/ogg": ".ogg",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/aac": ".aac",
            "audio/wav": ".wav",
        }

        if content_type in content_type_to_ext:
            return content_type_to_ext[content_type]

        url_ext = Path(urlparse(url).path).suffix.lower()

        if url_ext in KNOWN_MEDIA_EXTENSIONS:
            return url_ext

        return None

    # ---------------------------------------------------------------------
    # Downloading
    # ---------------------------------------------------------------------

    def build_existing_files_index(self, chat_id: str) -> None:
        """Index already downloaded files to support fast resume behavior."""
        self.existing_files_index = {}
        base_dir = self.config.output_dir / "media" / chat_id

        search_dirs: tuple[tuple[MediaType, Path], ...] = (
            ("image", base_dir / "images"),
            ("audio", base_dir / "audio"),
        )

        for media_type, folder in search_dirs:
            if not folder.exists():
                continue

            for file_path in folder.iterdir():
                if not file_path.is_file() or file_path.stat().st_size <= 0:
                    continue

                file_hash = file_path.stem
                self.existing_files_index[file_hash] = {
                    "local_path": str(file_path),
                    "content_type": "",
                    "media_type": media_type,
                    "size_bytes": file_path.stat().st_size,
                    "resumed_from_existing_file": True,
                }

        self.log(f"Индекс уже скачанных файлов: {len(self.existing_files_index)}")

    def find_existing_downloaded_file(self, url: str) -> ManifestItem | None:
        return self.existing_files_index.get(self.sha1(url))

    @staticmethod
    def write_response_to_file(response: requests.Response, file_path: Path) -> int:
        """Stream a `requests` response into a file and return the byte count."""
        size_bytes = 0

        with file_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue

                file.write(chunk)
                size_bytes += len(chunk)

        return size_bytes

    def download_media(
        self,
        session: requests.Session,
        url: str,
        chat_id: str,
        manifest: Manifest,
        failed: list[dict[str, str]],
    ) -> ManifestItem | None:
        """Download one media URL or reuse a previous manifest/index entry."""
        if url in manifest:
            local_path = Path(str(manifest[url].get("local_path", "")))

            if local_path.exists() and local_path.stat().st_size > 0:
                return manifest[url]

        existing_item = self.find_existing_downloaded_file(url)

        if existing_item is not None:
            manifest[url] = existing_item
            return existing_item

        try:
            response = session.get(
                url,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
                stream=True,
            )
            response.raise_for_status()
        except Exception as error:
            failed.append({"url": url, "error": str(error)})
            self.log(f"FAILED: {url}")
            return None

        content_type = response.headers.get("Content-Type", "")
        media_type = self.guess_media_type(content_type, url)

        if media_type is None:
            failed.append(
                {
                    "url": url,
                    "error": f"Skipped disabled/unknown file. Content-Type: {content_type}",
                }
            )
            self.log(f"SKIP other: {url}")
            return None

        extension = self.guess_extension(content_type, url)

        if extension is None:
            failed.append(
                {
                    "url": url,
                    "error": f"Skipped unknown extension. Content-Type: {content_type}",
                }
            )
            self.log(f"SKIP unknown extension: {url}")
            return None

        media_subdir = self.config.output_dir / "media" / chat_id / (
            "images" if media_type == "image" else "audio"
        )
        media_subdir.mkdir(parents=True, exist_ok=True)

        file_path = media_subdir / f"{self.sha1(url)}{extension}"
        size_bytes = self.write_response_to_file(response, file_path)

        if size_bytes <= 0:
            file_path.unlink(missing_ok=True)
            failed.append({"url": url, "error": "Downloaded file is empty"})
            self.log(f"FAILED empty: {url}")
            return None

        item: ManifestItem = {
            "local_path": str(file_path),
            "content_type": content_type,
            "media_type": media_type,
            "size_bytes": size_bytes,
        }
        manifest[url] = item

        time.sleep(self.config.delay_seconds)
        return item

    # ---------------------------------------------------------------------
    # HTML rewriting
    # ---------------------------------------------------------------------

    @staticmethod
    def relpath_for_html(file_path: Path, html_output_path: Path) -> str:
        rel_path = os.path.relpath(file_path, start=html_output_path.parent)
        return rel_path.replace("\\", "/")

    @staticmethod
    def replace_link_with_image(
        soup: BeautifulSoup,
        tag,
        relative_link: str,
        original_url: str,
    ) -> None:
        new_link = soup.new_tag("a", href=relative_link)
        new_link["target"] = "_blank"
        new_link["data-original-url"] = original_url

        image = soup.new_tag("img", src=relative_link)
        image["loading"] = "lazy"
        image["style"] = (
            "max-width: 420px; max-height: 420px; display: block; "
            "border-radius: 8px; margin: 6px 0;"
        )

        new_link.append(image)
        tag.replace_with(new_link)

    @staticmethod
    def replace_link_with_audio(
        soup: BeautifulSoup,
        tag,
        relative_link: str,
        original_url: str,
    ) -> None:
        audio = soup.new_tag("audio", src=relative_link)
        audio["controls"] = ""
        audio["preload"] = "metadata"
        audio["data-original-url"] = original_url
        tag.replace_with(audio)

    def rewrite_html_file(
        self,
        input_html: Path,
        output_html: Path,
        manifest: Manifest,
    ) -> int:
        """Rewrite links in one HTML file so they point to local downloaded files."""
        html = self.read_html(input_html)
        soup = BeautifulSoup(html, "html.parser")
        changed_count = 0
        allowed_media_types = self.config.allowed_media_types

        for tag in soup.find_all(["a", "img", "audio", "source"]):
            for attr in ("href", "src"):
                old_url = tag.get(attr)

                if not old_url:
                    continue

                old_url = self.clean_url(old_url)

                if old_url not in manifest:
                    continue

                item = manifest[old_url]
                media_type = item.get("media_type")

                if media_type not in allowed_media_types:
                    continue

                local_file = Path(str(item.get("local_path", "")))

                if not local_file.exists():
                    continue

                relative_link = self.relpath_for_html(local_file, output_html)

                if self.config.inline_media and tag.name == "a":
                    if media_type == "image":
                        self.replace_link_with_image(soup, tag, relative_link, old_url)
                    elif media_type == "audio":
                        self.replace_link_with_audio(soup, tag, relative_link, old_url)
                    changed_count += 1
                    break

                tag[attr] = relative_link
                tag["data-original-url"] = old_url
                changed_count += 1

        output_html.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_css_link(soup)
        output_html.write_text(str(soup), encoding="utf-8")

        return changed_count

    # ---------------------------------------------------------------------
    # Output index page
    # ---------------------------------------------------------------------

    @staticmethod
    def create_index_html(output_chat_dir: Path, html_files: list[Path]) -> None:
        """Create a simple index page for all rewritten message pages."""
        index_html = output_chat_dir / "index.html"
        links = "\n".join(
            f'                <li><a href="{html_file.name}">{html_file.name}</a></li>'
            for html_file in html_files
        )

        index_html.write_text(
            f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>VK chat archive</title>
    <link rel="stylesheet" href="../style.css">
    <style>
        :root {{
            --page-bg: #edeef0;
            --card-bg: #ffffff;
            --vk-blue: #4a76a8;
            --link-blue: #2a5885;
            --muted-text: #818c99;
            --border: #e7e8ec;
        }}

        body {{
            background: var(--page-bg);
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        }}

        .archive_top {{
            height: 48px;
            background: var(--vk-blue);
            display: flex;
            align-items: center;
        }}

        .archive_top_inner {{
            width: 960px;
            margin: 0 auto;
            color: white;
            font-size: 24px;
            font-weight: 700;
        }}

        .archive_wrap {{
            width: 760px;
            margin: 48px auto;
            background: var(--card-bg);
            border-radius: 6px;
            box-shadow: 0 1px 0 0 #dce1e6;
            overflow: hidden;
        }}

        .archive_header {{
            padding: 22px 26px;
            border-bottom: 1px solid var(--border);
            font-size: 20px;
            font-weight: 500;
        }}

        .archive_content {{
            padding: 22px 26px 28px;
        }}

        .archive_hint {{
            color: var(--muted-text);
            margin-bottom: 16px;
            font-size: 14px;
        }}

        .archive_list {{
            list-style: none;
            margin: 0;
            padding: 0;
        }}

        .archive_list li {{
            margin-bottom: 8px;
        }}

        .archive_list a {{
            display: block;
            padding: 11px 14px;
            background: #f5f6f8;
            border-radius: 6px;
            color: var(--link-blue);
            text-decoration: none;
            font-size: 14px;
        }}

        .archive_list a:hover {{
            background: #e9edf2;
        }}
    </style>
</head>
<body>
    <div class="archive_top">
        <div class="archive_top_inner">VK</div>
    </div>

    <main class="archive_wrap">
        <div class="archive_header">Архив чата</div>

        <div class="archive_content">
            <div class="archive_hint">Страницы сообщений этого чата:</div>

            <ul class="archive_list">
{links}
            </ul>
        </div>
    </main>
</body>
</html>
""",
            encoding="utf-8",
        )

    # ---------------------------------------------------------------------
    # Main processing pipeline
    # ---------------------------------------------------------------------

    def validate_archive_structure(self) -> None:
        """Validate that the selected folder looks like a VK export archive."""
        if not self.config.archive_root.exists():
            raise FileNotFoundError(f"Папка архива не найдена: {self.config.archive_root}")

        messages_dir = self.config.archive_root / "messages"
        if not messages_dir.exists():
            raise FileNotFoundError(
                f"В папке архива не найдена папка messages: {self.config.archive_root}"
            )

        if not self.config.chat_folder.exists():
            raise FileNotFoundError(f"Папка чата не найдена: {self.config.chat_folder}")

        if not self.config.allowed_media_types:
            raise ValueError("Выберите хотя бы один тип вложений для скачивания.")

    def find_message_pages(self) -> list[Path]:
        html_files = sorted(
            self.config.chat_folder.glob("messages*.html"),
            key=self.natural_message_sort_key,
        )

        if not html_files:
            raise FileNotFoundError(
                f"Не нашёл messages*.html в папке: {self.config.chat_folder}"
            )

        return html_files

    def extract_all_urls(self, html_files: list[Path]) -> list[str]:
        all_urls: set[str] = set()

        for html_file in html_files:
            all_urls.update(self.extract_urls_from_html(html_file))

        return sorted(all_urls)

    def download_all_media(
        self,
        urls: list[str],
        manifest: Manifest,
        failed: list[dict[str, str]],
    ) -> None:
        total = len(urls)

        with requests.Session() as session:
            for index, url in enumerate(urls, start=1):
                if index == 1 or index == total or index % MANIFEST_SAVE_INTERVAL == 0:
                    self.log(f"Обработано ссылок: {index}/{total}")

                self.download_media(session, url, self.config.dialog_id, manifest, failed)

                if total:
                    self.progress(index, total)

                if index % MANIFEST_SAVE_INTERVAL == 0:
                    self.save_manifest(manifest)
                    self.log(f"Manifest saved after {index} files")

    def rewrite_all_html(self, html_files: list[Path], manifest: Manifest) -> int:
        output_chat_dir = self.config.output_dir / f"chat_{self.config.dialog_id}"
        total_changed = 0

        for html_file in html_files:
            output_html = output_chat_dir / html_file.name
            total_changed += self.rewrite_html_file(html_file, output_html, manifest)

        self.create_index_html(output_chat_dir, html_files)
        return total_changed

    def write_failed_urls(self, failed: list[dict[str, str]]) -> Path:
        failed_path = self.config.output_dir / "failed_urls.txt"

        if failed:
            failed_text = "\n\n".join(
                f"URL: {item['url']}\nERROR: {item['error']}" for item in failed
            )
        else:
            failed_text = "Ошибок скачивания нет."

        failed_path.write_text(failed_text, encoding="utf-8")
        return failed_path

    def run(self) -> None:
        """Run the full archive processing pipeline."""
        self.validate_archive_structure()

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.copy_style_css()

        html_files = self.find_message_pages()

        self.log("")
        self.log(f"=== Обрабатываю чат: {self.config.dialog_id} ===")
        self.log(f"Найдено HTML-страниц: {len(html_files)}")

        manifest = self.load_manifest()
        failed: list[dict[str, str]] = []

        self.build_existing_files_index(self.config.dialog_id)

        urls = self.extract_all_urls(html_files)
        self.log(f"Найдено потенциальных media-ссылок: {len(urls)}")

        self.download_all_media(urls, manifest, failed)
        total_changed = self.rewrite_all_html(html_files, manifest)

        self.save_manifest(manifest)
        failed_path = self.write_failed_urls(failed)
        output_chat_dir = self.config.output_dir / f"chat_{self.config.dialog_id}"

        self.log("")
        self.log("=== ВСЁ ГОТОВО ===")
        self.log(f"Заменено ссылок в HTML: {total_changed}")
        self.log(f"Итоговая папка: {self.config.output_dir}")
        self.log(f"Открывай: {output_chat_dir / 'index.html'}")
        self.log(f"Manifest: {self.config.output_dir / 'manifest.json'}")
        self.log(f"Ошибки: {failed_path}")


# =============================================================================
# Tkinter GUI
# =============================================================================


class VKArchiveGUI(tk.Tk):
    """Main desktop window."""

    def __init__(self) -> None:
        super().__init__()

        self.title(APP_NAME)
        self.configure_window_size()

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self.archive_root_var = tk.StringVar()
        self.dialog_id_var = tk.StringVar()
        self.output_root_var = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.output_name_var = tk.StringVar(value="VK Archive Output")

        self.download_images_var = tk.BooleanVar(value=True)
        self.download_webp_gif_var = tk.BooleanVar(value=True)
        self.download_audio_var = tk.BooleanVar(value=True)
        self.inline_media_var = tk.BooleanVar(value=True)

        self.create_widgets()
        self.after(100, self.process_log_queue)

    def configure_window_size(self) -> None:
        dpi_factor = get_dpi_factor(self)

        window_w = int(880 * dpi_factor)
        window_h = int(680 * dpi_factor)
        min_w = int(780 * dpi_factor)
        min_h = int(600 * dpi_factor)

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        window_w = min(window_w, screen_w - 80)
        window_h = min(window_h, screen_h - 80)

        pos_x = max(20, (screen_w - window_w) // 2)
        pos_y = max(20, (screen_h - window_h) // 2)

        self.geometry(f"{window_w}x{window_h}+{pos_x}+{pos_y}")
        self.minsize(min(min_w, screen_w - 80), min(min_h, screen_h - 80))

    # ---------------------------------------------------------------------
    # UI construction
    # ---------------------------------------------------------------------

    def create_widgets(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        self.create_header(root)
        self.create_settings_section(root)
        self.create_options_section(root)
        self.create_controls_section(root)
        self.create_log_section(root)

    def create_header(self, parent: ttk.Frame) -> None:
        title = ttk.Label(
            parent,
            text=APP_NAME,
            font=("Segoe UI", 18, "bold"),
        )
        title.pack(anchor="w", pady=(0, 2))

        subtitle = ttk.Label(
            parent,
            text=f"by {AUTHOR_NAME}",
            font=("Segoe UI", 8, "underline"),
            cursor="hand2",
        )
        subtitle.pack(anchor="w", pady=(0, 12))
        subtitle.bind("<Button-1>", lambda _event: webbrowser.open_new_tab(AUTHOR_LINK))

    def create_settings_section(self, parent: ttk.Frame) -> None:
        settings = ttk.LabelFrame(parent, text="Настройки", padding=12)
        settings.pack(fill="x")

        self.add_path_row(
            settings,
            row=0,
            label="Папка VK-архива:",
            variable=self.archive_root_var,
            button_text="Выбрать...",
            command=self.choose_archive_root,
        )

        ttk.Label(settings, text="ID собеседника:").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(settings, textvariable=self.dialog_id_var).grid(
            row=1,
            column=1,
            sticky="ew",
            pady=6,
            padx=(8, 8),
        )

        hint = ttk.Label(
            settings,
            text="Например: 123456789. Программа будет искать папку archive/messages/123456789",
            foreground="#666666",
        )
        hint.grid(row=2, column=1, sticky="w", pady=(0, 6), padx=(8, 8))

        self.add_path_row(
            settings,
            row=3,
            label="Куда сохранить:",
            variable=self.output_root_var,
            button_text="Выбрать...",
            command=self.choose_output_root,
        )

        ttk.Label(settings, text="Имя новой папки:").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(settings, textvariable=self.output_name_var).grid(
            row=4,
            column=1,
            sticky="ew",
            pady=6,
            padx=(8, 8),
        )

        settings.columnconfigure(1, weight=1)

    def create_options_section(self, parent: ttk.Frame) -> None:
        options = ttk.LabelFrame(parent, text="Что скачивать", padding=12)
        options.pack(fill="x", pady=(12, 0))

        ttk.Checkbutton(
            options,
            text="Фото / картинки JPG, JPEG, PNG",
            variable=self.download_images_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            options,
            text="WEBP / GIF / стикеры, если в архиве есть прямая ссылка",
            variable=self.download_webp_gif_var,
        ).grid(row=0, column=1, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            options,
            text="Голосовые / аудио",
            variable=self.download_audio_var,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 20))

        ttk.Checkbutton(
            options,
            text="Показывать вложения прямо в HTML",
            variable=self.inline_media_var,
        ).grid(row=1, column=1, sticky="w", pady=(8, 0), padx=(0, 20))

    def create_controls_section(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(12, 8))

        self.start_button = ttk.Button(
            controls,
            text="Начать обработку",
            command=self.start_processing,
        )
        self.start_button.pack(side="left")

        self.open_output_button = ttk.Button(
            controls,
            text="Открыть папку результата",
            command=self.open_output_folder,
        )
        self.open_output_button.pack(side="left", padx=(8, 0))

        self.progress_bar = ttk.Progressbar(controls, mode="determinate")
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(12, 0))

        self.status_var = tk.StringVar(value="Готово к запуску")
        ttk.Label(parent, textvariable=self.status_var).pack(anchor="w", pady=(0, 6))

    def create_log_section(self, parent: ttk.Frame) -> None:
        log_frame = ttk.LabelFrame(parent, text="Лог", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, wrap="word", height=18)
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    @staticmethod
    def add_path_row(
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        button_text: str,
        command: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)

        ttk.Entry(parent, textvariable=variable).grid(
            row=row,
            column=1,
            sticky="ew",
            pady=6,
            padx=(8, 8),
        )

        ttk.Button(parent, text=button_text, command=command).grid(
            row=row,
            column=2,
            sticky="e",
            pady=6,
        )

    # ---------------------------------------------------------------------
    # UI actions
    # ---------------------------------------------------------------------

    def choose_archive_root(self) -> None:
        folder = filedialog.askdirectory(title="Выберите корневую папку VK-архива")

        if folder:
            self.archive_root_var.set(folder)

    def choose_output_root(self) -> None:
        folder = filedialog.askdirectory(title="Выберите папку для сохранения результата")

        if folder:
            self.output_root_var.set(folder)

    def open_output_folder(self) -> None:
        output_name = sanitize_folder_name(self.output_name_var.get().strip())
        output_dir = Path(self.output_root_var.get().strip()) / output_name

        if output_dir.exists():
            open_folder(output_dir)
        else:
            messagebox.showwarning("Папка не найдена", f"Папка ещё не создана:\n{output_dir}")

    def append_log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    # ---------------------------------------------------------------------
    # Thread communication
    # ---------------------------------------------------------------------

    def thread_log(self, text: str) -> None:
        self.log_queue.put(text)

    def thread_progress(self, current: int, total: int) -> None:
        percent = int((current / total) * 100) if total else 0
        self.log_queue.put(f"__PROGRESS__:{percent}:{current}:{total}")

    def process_log_queue(self) -> None:
        max_messages_per_tick = 50
        processed = 0

        try:
            while processed < max_messages_per_tick:
                message = self.log_queue.get_nowait()
                processed += 1
                self.handle_worker_message(message)
        except queue.Empty:
            pass

        self.after(50, self.process_log_queue)

    def handle_worker_message(self, message: str) -> None:
        if message.startswith("__PROGRESS__:"):
            _, percent, current, total = message.split(":")
            self.progress_bar["value"] = int(percent)
            self.status_var.set(f"Скачивание: {current}/{total} ({percent}%)")
            return

        if message == "__DONE__":
            self.start_button.configure(state="normal")
            self.status_var.set("Готово")
            messagebox.showinfo("Готово", "Обработка завершена.")
            return

        if message.startswith("__ERROR__:"):
            error_text = message.replace("__ERROR__:", "", 1)
            self.start_button.configure(state="normal")
            self.status_var.set("Ошибка")
            messagebox.showerror("Ошибка", error_text)
            return

        self.append_log(message)

    # ---------------------------------------------------------------------
    # Validation and processing start
    # ---------------------------------------------------------------------

    def validate_inputs(self) -> ProcessorConfig | None:
        archive_root_text = self.archive_root_var.get().strip()
        dialog_id = self.dialog_id_var.get().strip()
        output_root_text = self.output_root_var.get().strip()
        output_name = self.output_name_var.get().strip()

        if not archive_root_text:
            messagebox.showwarning("Не хватает данных", "Укажите папку VK-архива.")
            return None

        archive_root = Path(archive_root_text)
        if not archive_root.exists():
            messagebox.showwarning("Папка не найдена", f"Папка VK-архива не найдена:\n{archive_root}")
            return None

        if not dialog_id:
            messagebox.showwarning("Не хватает данных", "Укажите ID собеседника.")
            return None

        if not output_root_text:
            messagebox.showwarning("Не хватает данных", "Укажите папку для сохранения.")
            return None

        output_root = Path(output_root_text)
        if not output_root.exists():
            messagebox.showwarning("Папка не найдена", f"Папка для сохранения не найдена:\n{output_root}")
            return None

        if not output_name:
            messagebox.showwarning("Не хватает данных", "Укажите имя новой папки.")
            return None

        if not self.is_any_media_type_selected():
            messagebox.showwarning(
                "Не выбран тип файлов",
                "Выберите хотя бы один тип вложений для скачивания.",
            )
            return None

        return ProcessorConfig(
            archive_root=archive_root,
            dialog_id=dialog_id,
            output_root=output_root,
            output_name=output_name,
            download_images=self.download_images_var.get(),
            download_webp_gif=self.download_webp_gif_var.get(),
            download_audio=self.download_audio_var.get(),
            inline_media=self.inline_media_var.get(),
        )

    def is_any_media_type_selected(self) -> bool:
        return any(
            (
                self.download_images_var.get(),
                self.download_webp_gif_var.get(),
                self.download_audio_var.get(),
            )
        )

    def start_processing(self) -> None:
        config = self.validate_inputs()

        if config is None:
            return

        self.progress_bar["value"] = 0
        self.log_text.delete("1.0", "end")
        self.status_var.set("Запуск...")
        self.start_button.configure(state="disabled")

        processor = VKArchiveProcessor(
            config=config,
            log_callback=self.thread_log,
            progress_callback=self.thread_progress,
        )

        def worker() -> None:
            try:
                processor.run()
                self.log_queue.put("__DONE__")
            except Exception as error:
                self.log_queue.put(f"__ERROR__:{error}")

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()


# =============================================================================
# Entrypoint
# =============================================================================


def main() -> None:
    enable_dpi_awareness()
    app = VKArchiveGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
