"""
下载引擎 - 递归遍历课程内容并下载所有文件
"""

import json
import mimetypes
import os
import re
import tempfile
import time
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

import requests
from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TextColumn,
    SpinnerColumn,
)

from api import BlackboardAPI
from config import BB_BASE_URL, DEFAULT_DOWNLOAD_DIR, INVALID_CHARS, MAX_RETRIES

console = Console()

# 可以包含附件的内容类型
CONTENT_TYPES_WITH_ATTACHMENTS = [
    "resource/x-bb-document",
    "resource/x-bb-file",
    "resource/x-bb-assignment",
    "resource/x-bb-asn-assignment",
]

# 文件夹类型
FOLDER_TYPES = [
    "resource/x-bb-folder",
    "resource/x-bb-lesson",
    "resource/x-bb-coursemodule",
]

EMBEDDED_FILE_STUB = "@X@EmbeddedFile.requestUrlStub@X@"


class _EmbeddedFileLinkParser(HTMLParser):
    """Extract downloadable BbML links from body/instructions HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value for key, value in attrs}
        raw_metadata = attributes.get("data-bbfile")
        metadata: dict = {}
        if raw_metadata:
            try:
                metadata = json.loads(raw_metadata)
            except (TypeError, ValueError):
                metadata = {}

        url = (
            metadata.get("resourceUrl")
            or metadata.get("viewerUrl")
            or attributes.get("href")
            or attributes.get("src")
            or attributes.get("data-src")
        )
        if not url:
            return

        file_size = metadata.get("fileSize", -1)
        try:
            file_size = int(file_size)
        except (TypeError, ValueError):
            file_size = -1

        self.links.append({
            "url": url,
            "name": (
                metadata.get("displayName")
                or metadata.get("fileName")
                or metadata.get("linkName")
                or attributes.get("alt")
                or ""
            ),
            "mime_type": metadata.get("mimeType", ""),
            "size": file_size,
        })


def extract_embedded_files(*html_fragments: str | None) -> list[dict]:
    """Return unique embedded file links found in BbML/HTML fragments."""
    parser = _EmbeddedFileLinkParser()
    for fragment in html_fragments:
        if fragment:
            parser.feed(str(fragment))

    files = []
    seen = set()
    for item in parser.links:
        key = (item["url"], item["name"])
        if key in seen:
            continue
        seen.add(key)
        files.append(item)
    return files


def filename_from_content_disposition(value: str | None) -> str | None:
    """Parse both regular and RFC 5987 Content-Disposition filenames."""
    if not value:
        return None

    message = Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    return unquote(filename) if filename else None


def sanitize_filename(name: str) -> str:
    """
    清理文件名，移除不允许的字符

    Args:
        name: 原始文件名

    Returns:
        清理后的文件名
    """
    if not name:
        return "untitled"

    for char, replacement in INVALID_CHARS.items():
        name = name.replace(char, replacement)

    # 去除首尾空格和点
    name = name.strip().strip(".")

    # 限制长度
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[:200 - len(ext)] + ext

    return name if name else "untitled"


def ensure_unique_path(path: str) -> str:
    """
    确保路径唯一（处理同名文件）

    Args:
        path: 目标文件路径

    Returns:
        唯一的文件路径
    """
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base} ({counter}){ext}"):
        counter += 1
    return f"{base} ({counter}){ext}"


def find_existing_path(path: str, size: int | None) -> str | None:
    """Find an existing base or numbered copy matching the download size."""
    parent = os.path.dirname(path)
    base, ext = os.path.splitext(os.path.basename(path))
    numbered = re.compile(rf"{re.escape(base)} \([1-9][0-9]*\){re.escape(ext)}")
    candidates = [path]
    candidates.extend(
        os.path.join(parent, name)
        for name in os.listdir(parent)
        if numbered.fullmatch(name)
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            existing_size = os.path.getsize(candidate)
            if existing_size == size or (size is None and existing_size > 0):
                return candidate
    return None


class CourseDownloader:
    """课程内容下载器"""

    def __init__(
        self,
        api: BlackboardAPI,
        download_dir: str = DEFAULT_DOWNLOAD_DIR,
    ):
        self.api = api
        self.download_dir = download_dir
        self.stats = {
            "files_downloaded": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "total_bytes": 0,
        }
        self._embedded_urls: set[str] = set()

    def download_course(self, course_id: str, course_name: str) -> None:
        """
        下载单个课程的全部内容

        Args:
            course_id: 课程 ID
            course_name: 课程名称（用于创建目录）
        """
        safe_name = sanitize_filename(course_name)
        course_dir = os.path.join(self.download_dir, safe_name)
        os.makedirs(course_dir, exist_ok=True)

        console.print(f"\n[bold cyan]📚 正在处理课程: {course_name}[/bold cyan]")
        console.print(f"[dim]   保存到: {course_dir}[/dim]")

        try:
            contents = self.api.get_course_contents(course_id)
            if not contents:
                console.print("   [yellow]该课程没有内容[/yellow]")
                return

            for content in contents:
                self._process_content(course_id, content, course_dir)

            self._download_announcement_files(course_id, course_dir)

        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 403:
                console.print(f"   [yellow]⚠ 无权访问该课程内容[/yellow]")
            else:
                console.print(f"   [red]✗ 获取课程内容失败: {e}[/red]")

    def _process_content(
        self,
        course_id: str,
        content: dict,
        parent_dir: str,
    ) -> None:
        """
        递归处理单个内容项

        Args:
            course_id: 课程 ID
            content: 内容项数据
            parent_dir: 父目录路径
        """
        title = content.get("title", "Untitled")
        content_id = content.get("id", "")
        handler = content.get("contentHandler", {})
        handler_id = handler.get("id", "")

        # 如果是文件夹类型，递归处理子内容
        if handler_id in FOLDER_TYPES:
            folder_name = sanitize_filename(title)
            folder_path = os.path.join(parent_dir, folder_name)
            os.makedirs(folder_path, exist_ok=True)

            try:
                children = self.api.get_content_children(course_id, content_id)
                for child in children:
                    self._process_content(course_id, child, folder_path)
            except Exception as e:
                console.print(f"   [yellow]⚠ 无法获取 '{title}' 的子内容: {e}[/yellow]")

        self._download_embedded_files(content, parent_dir)

        # 如果可能包含附件，尝试下载
        if handler_id in CONTENT_TYPES_WITH_ATTACHMENTS or not handler_id:
            self._download_attachments(course_id, content_id, title, parent_dir)

    def _resolve_embedded_url(self, raw_url: str) -> str | None:
        """Resolve a BbML file URL and keep requests on the Blackboard host."""
        normalized = raw_url.replace(EMBEDDED_FILE_STUB, "").strip()
        resolved = urljoin(f"{BB_BASE_URL.rstrip('/')}/", normalized)
        target = urlparse(resolved)
        base = urlparse(BB_BASE_URL)

        if target.scheme not in ("http", "https") or not target.hostname:
            return None
        if target.hostname != base.hostname and not target.hostname.endswith(
            f".{base.hostname}"
        ):
            return None
        if "bbcswebdav" not in target.path.lower():
            return None
        return resolved

    def _download_embedded_files(self, content: dict, parent_dir: str) -> None:
        """Download files embedded in content bodies and assessment instructions."""
        handler = content.get("contentHandler") or {}
        embedded_files = extract_embedded_files(
            content.get("body"),
            handler.get("instructions"),
        )

        for item in embedded_files:
            resolved_url = self._resolve_embedded_url(item.get("url", ""))
            if not resolved_url or resolved_url in self._embedded_urls:
                continue

            self._embedded_urls.add(resolved_url)
            file_name = item.get("name") or unquote(
                os.path.basename(urlparse(resolved_url).path)
            )
            if not file_name:
                file_name = "embedded_file"

            if not os.path.splitext(file_name)[1] and item.get("mime_type"):
                extension = mimetypes.guess_extension(item["mime_type"])
                if extension:
                    file_name += extension

            safe_name = sanitize_filename(file_name)
            file_path = os.path.join(parent_dir, safe_name)
            self._download_file(
                file_path,
                safe_name,
                lambda url=resolved_url: self.api.download_url(url),
                expected_size=item.get("size", -1),
            )

    def _download_announcement_files(
        self,
        course_id: str,
        course_dir: str,
    ) -> None:
        """Download files embedded in course announcements."""
        try:
            announcements = self.api.get_course_announcements(course_id)
        except Exception:
            return

        for announcement in announcements:
            body = announcement.get("body")
            if not extract_embedded_files(body):
                continue

            title = sanitize_filename(announcement.get("title") or "Announcement")
            announcement_dir = os.path.join(course_dir, "Announcements", title)
            os.makedirs(announcement_dir, exist_ok=True)
            self._download_embedded_files({"body": body}, announcement_dir)

    def _download_attachments(
        self,
        course_id: str,
        content_id: str,
        content_title: str,
        parent_dir: str,
    ) -> None:
        """
        下载内容项的所有附件

        Args:
            course_id: 课程 ID
            content_id: 内容 ID
            content_title: 内容标题
            parent_dir: 保存目录
        """
        try:
            attachments = self.api.get_content_attachments(course_id, content_id)
        except Exception:
            return

        for attachment in attachments:
            attachment_id = attachment.get("id", "")
            file_name = attachment.get("fileName", "")

            if not file_name:
                file_name = f"attachment_{attachment_id}"

            safe_name = sanitize_filename(file_name)
            file_path = os.path.join(parent_dir, safe_name)

            # 下载文件
            self._download_file(
                file_path,
                safe_name,
                lambda: self.api.download_attachment(
                    course_id, content_id, attachment_id
                ),
                expected_size=attachment.get("size", -1),
            )

    def _download_file(
        self,
        file_path: str,
        display_name: str,
        response_factory,
        expected_size: int = -1,
    ) -> None:
        """
        下载单个文件（带重试和进度条）

        Args:
            file_path: 保存路径
            display_name: 显示名称
            response_factory: 返回流式响应的无参数函数
            expected_size: 预期文件大小，用于跳过已下载文件
        """
        for attempt in range(1, MAX_RETRIES + 1):
            resp = None
            temp_path = None
            try:
                resp = response_factory()

                # Resolve the final filename and response size before reading the body.
                try:
                    total_size = int(resp.headers.get("content-length", ""))
                    if total_size < 0:
                        total_size = None
                except (TypeError, ValueError):
                    total_size = None
                if total_size is None and expected_size > 0:
                    total_size = expected_size

                # 如果响应头有文件名，优先使用
                server_name = filename_from_content_disposition(
                    resp.headers.get("content-disposition")
                )
                if server_name:
                    safe_server_name = sanitize_filename(server_name)
                    if safe_server_name != display_name:
                        file_path = os.path.join(
                            os.path.dirname(file_path), safe_server_name
                        )
                        display_name = safe_server_name

                if find_existing_path(file_path, total_size) is not None:
                    self.stats["files_skipped"] += 1
                    console.print(f"   [dim]⏭ 已存在: {display_name}[/dim]")
                    return

                # Keep incomplete downloads out of the visible file set.
                fd, temp_path = tempfile.mkstemp(
                    prefix=".bb-download-", dir=os.path.dirname(file_path)
                )

                # 使用 rich 进度条下载
                with os.fdopen(fd, "wb") as f, Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.fields[filename]}"),
                    BarColumn(bar_width=30),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                    transient=True,
                ) as progress:
                    task = progress.add_task(
                        "下载中",
                        total=(
                            total_size
                            if total_size is not None and total_size > 0
                            else None
                        ),
                        filename=display_name[:50],
                    )

                    downloaded = 0
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress.update(task, completed=downloaded)

                if (
                    total_size is not None
                    and resp.headers.get("content-encoding", "identity") == "identity"
                    and downloaded != total_size
                ):
                    raise IOError(f"incomplete download: {downloaded}/{total_size} bytes")

                file_path = ensure_unique_path(file_path)
                os.replace(temp_path, file_path)
                temp_path = None

                self.stats["files_downloaded"] += 1
                self.stats["total_bytes"] += os.path.getsize(file_path)
                console.print(f"   [green]✓ {display_name}[/green]")
                return

            except Exception as e:
                if attempt < MAX_RETRIES:
                    console.print(
                        f"   [yellow]⚠ 下载失败，重试 ({attempt}/{MAX_RETRIES}): "
                        f"{display_name}[/yellow]"
                    )
                    time.sleep(2 * attempt)  # 指数退避
                else:
                    console.print(
                        f"   [red]✗ 下载失败: {display_name} - {e}[/red]"
                    )
                    self.stats["files_failed"] += 1
            finally:
                if temp_path is not None and os.path.exists(temp_path):
                    os.unlink(temp_path)
                if resp is not None:
                    resp.close()

    def print_stats(self) -> None:
        """打印下载统计"""
        from rich.table import Table

        table = Table(title="📊 下载统计", show_header=False, border_style="cyan")
        table.add_column("指标", style="bold")
        table.add_column("值", justify="right")

        table.add_row("✅ 成功下载", f"{self.stats['files_downloaded']} 个文件")
        table.add_row("⏭ 已跳过", f"{self.stats['files_skipped']} 个文件")
        table.add_row("❌ 下载失败", f"{self.stats['files_failed']} 个文件")

        total_mb = self.stats["total_bytes"] / (1024 * 1024)
        table.add_row("💾 总下载量", f"{total_mb:.2f} MB")

        console.print()
        console.print(table)
