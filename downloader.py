"""
下载引擎 - 递归遍历课程内容并下载所有文件
"""

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
from auth import _mount_requests_tls_compat
from config import BB_BASE_URL, DEFAULT_DOWNLOAD_DIR, INVALID_CHARS, MAX_RETRIES
from download_index import DownloadIndex

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


class CourseDownloader:
    """课程内容下载器"""

    def __init__(
        self,
        api: BlackboardAPI,
        download_dir: str = DEFAULT_DOWNLOAD_DIR,
    ):
        self.api = api
        self.download_dir = os.path.abspath(download_dir)
        self.index = DownloadIndex(self.download_dir)
        self._lock = threading.Lock()
        self._thread_local = threading.local()
        self._worker_sessions: list[requests.Session] = []
        self._executor: ThreadPoolExecutor | None = None
        self._jobs = []
        self._futures = []
        self._progress: Progress | None = None
        self.stats = {
            "files_downloaded": 0,
            "files_updated": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "total_bytes": 0,
        }
        self._embedded_urls: set[str] = set()

    def _download_api(self) -> BlackboardAPI:
        """Give each download worker its own authenticated requests session."""
        if self._executor is None:
            return self.api
        worker_api = getattr(self._thread_local, "api", None)
        if worker_api is None:
            source = self.api.session
            session = requests.Session()
            session.headers.update(source.headers)
            session.cookies.update(source.cookies)
            session.verify = source.verify
            session.cert = source.cert
            session.trust_env = source.trust_env
            session.proxies.update(source.proxies)
            _mount_requests_tls_compat(session)
            worker_api = BlackboardAPI(session)
            self._thread_local.api = worker_api
            with self._lock:
                self._worker_sessions.append(session)
        return worker_api

    def _schedule_download(self, *args, **kwargs) -> None:
        if self._executor is None:
            source_key = kwargs.get("source_key")
            if source_key:
                self.index.reserve_existing(source_key)
            self._download_file(*args, **kwargs)
        else:
            self._jobs.append((args, kwargs))

    def _backup_file(self, path: str) -> None:
        relative = os.path.relpath(path, self.download_dir)
        backup_dir = os.path.join(
            self.download_dir, ".bb-versions", os.path.dirname(relative)
        )
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = os.path.join(backup_dir, f"{os.path.basename(path)}.{stamp}.bak")
        shutil.copy2(path, backup)

    @staticmethod
    def _hash_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as existing:
            for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

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

        self._embedded_urls.clear()
        self._jobs = []
        self._futures = []
        self._worker_sessions = []
        with ThreadPoolExecutor(max_workers=4) as executor, Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.fields[filename]}"),
            BarColumn(bar_width=30),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            self._executor = executor
            self._progress = progress
            try:
                contents = self.api.get_course_contents(course_id)
                if not contents:
                    console.print("   [yellow]该课程没有内容[/yellow]")
                    return

                for content in contents:
                    self._process_content(course_id, content, course_dir)

                self._download_announcement_files(course_id, course_dir)

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    console.print("   [yellow]⚠ 无权访问该课程内容[/yellow]")
                else:
                    console.print(f"   [red]✗ 获取课程内容失败: {e}[/red]")
            finally:
                for _, kwargs in self._jobs:
                    source_key = kwargs.get("source_key")
                    if source_key:
                        self.index.reserve_existing(source_key)
                for args, kwargs in self._jobs:
                    self._futures.append(
                        executor.submit(self._download_file, *args, **kwargs)
                    )
                for future in as_completed(self._futures):
                    try:
                        future.result()
                    except Exception as error:
                        console.print(f"   [red]✗ 下载任务失败: {error}[/red]")
                        with self._lock:
                            self.stats["files_failed"] += 1
                self._executor = None
                self._progress = None
                for session in self._worker_sessions:
                    session.close()
                try:
                    self.index.save()
                except OSError as error:
                    console.print(f"   [yellow]⚠ 无法保存下载索引: {error}[/yellow]")

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
            target = urlparse(resolved_url)
            identity = f"{parent_dir}|{target.hostname}|{target.path}"
            source_key = "embedded:" + hashlib.sha256(identity.encode()).hexdigest()
            self._schedule_download(
                file_path,
                safe_name,
                lambda headers=None, url=resolved_url: self._download_api().download_url(
                    url, **({"headers": headers} if headers else {})
                ),
                expected_size=item.get("size", -1),
                source_key=source_key,
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
            self._schedule_download(
                file_path,
                safe_name,
                lambda headers=None, attachment_id=attachment_id: self._download_api().download_attachment(
                    course_id,
                    content_id,
                    attachment_id,
                    **({"headers": headers} if headers else {}),
                ),
                expected_size=attachment.get("size", -1),
                source_key=f"attachment:{course_id}:{content_id}:{attachment_id}",
            )

    def _download_file(
        self,
        file_path: str,
        display_name: str,
        response_factory,
        expected_size: int = -1,
        source_key: str | None = None,
    ) -> None:
        """Revalidate a Blackboard resource and replace changed bytes atomically."""
        source_key = source_key or f"path:{os.path.relpath(file_path, self.download_dir)}"
        cached_path, entry, local_valid = self.index.cached(source_key)
        conditional = {}
        if local_valid and entry:
            if entry.get("etag"):
                conditional["If-None-Match"] = entry["etag"]
            if entry.get("last_modified"):
                conditional["If-Modified-Since"] = entry["last_modified"]
            if conditional:
                conditional["Cache-Control"] = "no-cache"

        for attempt in range(1, MAX_RETRIES + 1):
            resp = None
            temp_path = None
            task = None
            try:
                resp = (
                    response_factory(headers=conditional)
                    if conditional else response_factory()
                )
                if resp.status_code == 304:
                    if not local_valid or not cached_path:
                        raise IOError("server reported unchanged but local file is missing")
                    reserved = self.index.claim(
                        source_key, cached_path, cached_path, True
                    )
                    if reserved != cached_path:
                        raise IOError("local filename belongs to another attachment")
                    validators = {
                        "ETag": resp.headers.get("ETag") or entry.get("etag"),
                        "Last-Modified": (
                            resp.headers.get("Last-Modified")
                            or entry.get("last_modified")
                        ),
                    }
                    self.index.record(source_key, cached_path, validators)
                    with self._lock:
                        self.stats["files_skipped"] += 1
                    console.print(f"   [dim]⏭ 未变化: {display_name}[/dim]")
                    return

                try:
                    response_size = int(resp.headers.get("Content-Length", ""))
                    if response_size < 0:
                        response_size = None
                except (TypeError, ValueError):
                    response_size = None
                progress_size = response_size
                if progress_size is None and expected_size > 0:
                    progress_size = expected_size

                server_name = filename_from_content_disposition(
                    resp.headers.get("Content-Disposition")
                )
                if server_name:
                    display_name = sanitize_filename(server_name)
                    preferred = os.path.join(os.path.dirname(file_path), display_name)
                else:
                    preferred = file_path
                output_path = self.index.claim(
                    source_key, preferred, cached_path, local_valid
                )
                display_name = os.path.basename(output_path)

                fd, temp_path = tempfile.mkstemp(
                    prefix=".bb-download-", dir=os.path.dirname(output_path)
                )
                digest = hashlib.sha256()
                progress = getattr(self, "_progress", None)
                downloaded = 0
                with os.fdopen(fd, "wb") as output:
                    if progress is not None:
                        task = progress.add_task(
                            "下载中",
                            total=(
                                progress_size
                                if progress_size is not None and progress_size > 0
                                else None
                            ),
                            filename=display_name[:50],
                        )
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            output.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            if task is not None:
                                progress.update(task, completed=downloaded)

                if (
                    response_size is not None
                    and resp.headers.get("Content-Encoding", "identity").lower()
                    == "identity"
                    and downloaded != response_size
                ):
                    raise IOError(
                        f"incomplete download: {downloaded}/{response_size} bytes"
                    )

                with self._lock:
                    self.stats["total_bytes"] += downloaded
                if (
                    cached_path
                    and cached_path != output_path
                    and os.path.isfile(cached_path)
                    and self._hash_file(cached_path) == digest.hexdigest()
                ):
                    self.index.release(source_key, output_path)
                    self.index.record(
                        source_key, cached_path, resp.headers, digest.hexdigest()
                    )
                    with self._lock:
                        self.stats["files_skipped"] += 1
                    console.print(f"   [dim]⏭ 内容相同: {display_name}[/dim]")
                    return
                if (
                    os.path.isfile(output_path)
                    and self._hash_file(output_path) == digest.hexdigest()
                ):
                    self.index.record(
                        source_key, output_path, resp.headers, digest.hexdigest()
                    )
                    with self._lock:
                        self.stats["files_skipped"] += 1
                    console.print(f"   [dim]⏭ 内容相同: {display_name}[/dim]")
                    return

                replacing = os.path.isfile(output_path)
                if replacing:
                    self._backup_file(output_path)
                os.replace(temp_path, output_path)
                temp_path = None
                self.index.record(
                    source_key, output_path, resp.headers, digest.hexdigest()
                )
                with self._lock:
                    self.stats["files_downloaded"] += 1
                    if replacing:
                        self.stats["files_updated"] += 1
                status = "↻ 已更新" if replacing else "✓ 已下载"
                console.print(f"   [green]{status}: {display_name}[/green]")
                return

            except Exception as error:
                conditional = {}
                if attempt < MAX_RETRIES:
                    console.print(
                        f"   [yellow]⚠ 下载失败，重试 ({attempt}/{MAX_RETRIES}): "
                        f"{display_name} - {error}[/yellow]"
                    )
                    time.sleep(2 * attempt)
                else:
                    console.print(f"   [red]✗ 下载失败: {display_name} - {error}[/red]")
                    with self._lock:
                        self.stats["files_failed"] += 1
            finally:
                if task is not None:
                    progress.remove_task(task)
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
        table.add_row("🔄 其中已更新", f"{self.stats['files_updated']} 个文件")
        table.add_row("⏭ 已跳过", f"{self.stats['files_skipped']} 个文件")
        table.add_row("❌ 下载失败", f"{self.stats['files_failed']} 个文件")

        total_mb = self.stats["total_bytes"] / (1024 * 1024)
        table.add_row("💾 总下载量", f"{total_mb:.2f} MB")

        console.print()
        console.print(table)
