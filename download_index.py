"""Local HTTP validators and file ownership for incremental downloads."""

import hashlib
import json
import os
import re
import tempfile
import threading


class DownloadIndex:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, ".bb-download-index.json")
        self.lock = threading.Lock()
        self.claims: dict[str, str] = {}
        self.entries: dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8") as source:
                data = json.load(source)
            if data.get("version") == 1 and isinstance(data.get("files"), dict):
                self.entries = data["files"]
        except (OSError, ValueError, AttributeError):
            pass

    def _stored_path(self, entry: dict) -> str | None:
        relative = entry.get("path")
        if not isinstance(relative, str) or os.path.isabs(relative):
            return None
        path = os.path.normpath(os.path.join(self.root, relative))
        if os.path.commonpath((self.root, path)) != self.root:
            return None
        return path

    def cached(self, key: str) -> tuple[str | None, dict | None, bool]:
        """Return stored path, entry, and whether the local file is unchanged."""
        with self.lock:
            entry = self.entries.get(key)
            if isinstance(entry, dict):
                entry = dict(entry)
        if not isinstance(entry, dict):
            return None, None, False
        path = self._stored_path(entry)
        if not path or not os.path.isfile(path):
            return path, entry, False
        stat = os.stat(path)
        valid = stat.st_size == entry.get("size") and stat.st_mtime_ns == entry.get("mtime_ns")
        if not valid and entry.get("sha256"):
            digest = hashlib.sha256()
            with open(path, "rb") as local_file:
                for chunk in iter(lambda: local_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() == entry["sha256"]:
                valid = True
                entry["size"] = stat.st_size
                entry["mtime_ns"] = stat.st_mtime_ns
                with self.lock:
                    self.entries[key] = entry
        return path, entry, valid

    def reserve_existing(self, key: str) -> None:
        path, _, _ = self.cached(key)
        if path and os.path.isfile(path):
            with self.lock:
                self.claims.setdefault(path, key)

    def claim(self, key: str, preferred: str, cached_path: str | None, valid: bool) -> str:
        """Reserve one output path per Blackboard resource in this run."""
        with self.lock:
            if valid and cached_path:
                candidate = cached_path
            else:
                candidate = preferred
                if cached_path == candidate and os.path.exists(candidate):
                    candidate = self._next_name(candidate)
            while candidate in self.claims and self.claims[candidate] != key:
                candidate = self._next_name(candidate)
            self.claims[candidate] = key
            return candidate

    def release(self, key: str, path: str) -> None:
        with self.lock:
            if self.claims.get(path) == key:
                del self.claims[path]

    def _next_name(self, path: str) -> str:
        base, ext = os.path.splitext(path)
        base = re.sub(r" \([1-9][0-9]*\)$", "", base)
        number = 1
        while True:
            candidate = f"{base} ({number}){ext}"
            if not os.path.exists(candidate) and candidate not in self.claims:
                return candidate
            number += 1

    def record(self, key: str, path: str, headers, digest: str | None = None) -> None:
        stat = os.stat(path)
        with self.lock:
            previous = self.entries.get(key)
            previous_digest = (
                previous.get("sha256") if isinstance(previous, dict) else None
            )
        entry = {
            "path": os.path.relpath(path, self.root),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
            "sha256": digest or previous_digest,
        }
        with self.lock:
            self.entries[key] = entry

    def save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        with self.lock:
            data = {"version": 1, "files": dict(self.entries)}
        fd, temporary = tempfile.mkstemp(prefix=".bb-index-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
