import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from requests.structures import CaseInsensitiveDict

from downloader import (
    CourseDownloader,
    extract_embedded_files,
    filename_from_content_disposition,
)


class EmbeddedFileTests(unittest.TestCase):
    def test_extracts_data_bbfile_links(self):
        html = (
            '<a href="https://blackboard.cuhk.edu.hk/bbcswebdav/xid-1" '
            'data-bbtype="attachment" '
            'data-bbfile="{&quot;displayName&quot;:&quot;HW1_Chapter3.pdf&quot;,'
            '&quot;mimeType&quot;:&quot;application/pdf&quot;,'
            '&quot;fileSize&quot;:49521,'
            '&quot;resourceUrl&quot;:&quot;https://blackboard.cuhk.edu.hk/'
            'bbcswebdav/xid-1?token=a&amp;b=c&quot;}">HW1</a>'
        )

        files = extract_embedded_files(html)

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "HW1_Chapter3.pdf")
        self.assertEqual(files[0]["mime_type"], "application/pdf")
        self.assertEqual(files[0]["size"], 49521)
        self.assertEqual(
            files[0]["url"],
            "https://blackboard.cuhk.edu.hk/bbcswebdav/xid-1?token=a&b=c",
        )

    def test_deduplicates_embedded_links(self):
        html = (
            '<a href="/bbcswebdav/xid-1">One</a>'
            '<a href="/bbcswebdav/xid-1">One again</a>'
        )

        self.assertEqual(len(extract_embedded_files(html)), 1)

    def test_parses_rfc5987_content_disposition(self):
        value = "attachment; filename*=UTF-8''%E8%AF%BE%E4%BB%B6.pdf"

        self.assertEqual(filename_from_content_disposition(value), "课件.pdf")

    def test_extracts_embedded_media_links(self):
        html = (
            '<img src="@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-2" '
            'alt="diagram.png">'
            '<video src="https://blackboard.example.edu/bbcswebdav/xid-3"></video>'
        )

        files = extract_embedded_files(html)

        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["name"], "diagram.png")
        self.assertEqual(
            files[0]["url"],
            "@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-2",
        )
        self.assertEqual(
            files[1]["url"], "https://blackboard.example.edu/bbcswebdav/xid-3"
        )


class CourseDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.api = Mock()
        self.api.get_content_attachments.return_value = []
        self.api.get_course_announcements.return_value = []
        self.base_url_patcher = patch(
            "downloader.BB_BASE_URL", "https://blackboard.example.edu"
        )
        self.base_url_patcher.start()
        self.addCleanup(self.base_url_patcher.stop)
        self.downloader = CourseDownloader(self.api, self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_assessment_instructions_are_scanned_for_embedded_files(self):
        content = {
            "id": "_7422240_1",
            "title": "HW1",
            "contentHandler": {
                "id": "resource/x-bb-asmt-test-link",
                "instructions": (
                    '<a href="https://blackboard.example.edu/bbcswebdav/xid-1" '
                    'data-bbfile="{&quot;displayName&quot;:&quot;HW1.pdf&quot;,'
                    '&quot;fileSize&quot;:123,'
                    '&quot;resourceUrl&quot;:&quot;https://blackboard.example.edu/'
                    'bbcswebdav/xid-1&quot;}">HW1.pdf</a>'
                ),
            },
        }

        with patch.object(self.downloader, "_download_file") as download_file:
            self.downloader._process_content("_course_1", content, self.temp_dir.name)

        download_file.assert_called_once()
        args, kwargs = download_file.call_args
        self.assertEqual(args[0], os.path.join(self.temp_dir.name, "HW1.pdf"))
        self.assertEqual(args[1], "HW1.pdf")
        self.assertEqual(kwargs["expected_size"], 123)

        response_factory = args[2]
        response_factory()
        self.api.download_url.assert_called_once_with(
            "https://blackboard.example.edu/bbcswebdav/xid-1"
        )

    def test_resolves_embedded_file_url_stub(self):
        self.assertEqual(
            self.downloader._resolve_embedded_url(
                "@X@EmbeddedFile.requestUrlStub@X@bbcswebdav/xid-2"
            ),
            "https://blackboard.example.edu/bbcswebdav/xid-2",
        )

    def test_external_embedded_urls_are_not_downloaded(self):
        content = {
            "body": (
                '<a href="https://example.com/course.pdf" '
                'data-bbtype="attachment">course.pdf</a>'
            ),
            "contentHandler": {"id": "resource/x-bb-document"},
        }

        with patch.object(self.downloader, "_download_file") as download_file:
            self.downloader._process_content("_course_1", content, self.temp_dir.name)

        download_file.assert_not_called()
        self.api.download_url.assert_not_called()

    def test_announcement_embedded_files_are_downloaded(self):
        self.api.get_course_announcements.return_value = [{
            "title": "Week 1 Notice",
            "body": (
                '<a href="https://blackboard.example.edu/bbcswebdav/xid-3" '
                'data-bbfile="{&quot;displayName&quot;:&quot;notice.pdf&quot;,'
                '&quot;fileSize&quot;:456}">notice.pdf</a>'
            ),
        }]

        with patch.object(self.downloader, "_download_file") as download_file:
            self.downloader._download_announcement_files(
                "_course_1", self.temp_dir.name
            )

        download_file.assert_called_once()
        args, kwargs = download_file.call_args
        self.assertEqual(
            args[0],
            os.path.join(
                self.temp_dir.name, "Announcements", "Week 1 Notice", "notice.pdf"
            ),
        )
        self.assertEqual(args[1], "notice.pdf")
        self.assertEqual(kwargs["expected_size"], 456)


class IncrementalDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.api = Mock()
        self.downloader = CourseDownloader(self.api, self.directory.name)

    def download(self, filename="lecture.pdf"):
        self.api.get_content_attachments.return_value = [
            {"id": "attachment-1", "fileName": filename}
        ]
        self.downloader._download_attachments(
            "course-1", "content-1", "Lecture", self.directory.name
        )

    def response(self, data=b"complete", **headers):
        response = Mock()
        response.headers = CaseInsensitiveDict(headers)
        response.status_code = 200
        response.iter_content.return_value = [data]
        self.api.download_attachment.return_value = response
        return response

    def test_first_run_verifies_existing_attachment_bytes(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"complete")
        response = self.response(**{"content-length": "8"})

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(os.listdir(self.directory.name), ["lecture.pdf"])
        response.iter_content.assert_called_once()
        response.close.assert_called_once()

    def test_checks_name_from_content_disposition_before_skipping(self):
        path = os.path.join(self.directory.name, "actual.pdf")
        with open(path, "wb") as output:
            output.write(b"complete")
        self.response(**{
            "content-length": "8",
            "content-disposition": 'attachment; filename="actual.pdf"',
        })

        self.download("api-name.pdf")

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(os.listdir(self.directory.name), ["actual.pdf"])

    def test_unknown_size_compares_existing_file_content(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"complete")
        response = self.response()

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        response.iter_content.assert_called_once()

    def test_etag_revalidation_skips_body_on_later_runs(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"complete")
        self.response(b"complete", ETag='"version-1"')
        self.download()
        response = self.response(b"ignored", ETag='"version-1"')
        response.status_code = 304

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 2)
        self.assertEqual(
            self.api.download_attachment.call_args.kwargs["headers"],
            {"If-None-Match": '"version-1"', "Cache-Control": "no-cache"},
        )
        response.iter_content.assert_not_called()

    def test_etag_is_reused_after_restarting_downloader(self):
        self.response(b"complete", ETag='"version-1"')
        self.download()
        self.downloader.index.save()
        restarted = CourseDownloader(self.api, self.directory.name)
        response = self.response(b"ignored", ETag='"version-1"')
        response.status_code = 304

        restarted._download_attachments(
            "course-1", "content-1", "Lecture", self.directory.name
        )

        self.assertEqual(restarted.stats["files_skipped"], 1)
        response.iter_content.assert_not_called()

    def test_touching_unchanged_local_file_does_not_make_duplicate(self):
        self.response(b"complete", ETag='"version-1"')
        self.download()
        path = os.path.join(self.directory.name, "lecture.pdf")
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        response = self.response(b"ignored", ETag='"version-1"')
        response.status_code = 304

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(
            set(os.listdir(self.directory.name)), {"lecture.pdf"}
        )
        response.iter_content.assert_not_called()

    def test_same_size_remote_change_replaces_local_file(self):
        self.response(b"original", ETag='"version-1"')
        self.download()
        self.response(b"revised!", ETag='"version-2"')

        self.download()

        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "rb") as saved:
            self.assertEqual(saved.read(), b"revised!")
        self.assertEqual(self.downloader.stats["files_updated"], 1)
        backups = os.listdir(os.path.join(self.directory.name, ".bb-versions"))
        self.assertEqual(len(backups), 1)

    def test_local_edit_is_preserved_when_remote_changes(self):
        self.response(b"original", ETag='"version-1"')
        self.download()
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as edited:
            edited.write(b"my local edit")
        self.response(b"revised!", ETag='"version-2"')

        self.download()

        with open(path, "rb") as saved:
            self.assertEqual(saved.read(), b"my local edit")
        with open(os.path.join(self.directory.name, "lecture (1).pdf"), "rb") as saved:
            self.assertEqual(saved.read(), b"revised!")
        self.assertNotIn("headers", self.api.download_attachment.call_args.kwargs)

    def test_local_edit_matching_remote_does_not_create_numbered_copy(self):
        self.response(b"original", ETag='"version-1"')
        self.download()
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as edited:
            edited.write(b"revised!")
        self.response(b"revised!", ETag='"version-2"')

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(set(os.listdir(self.directory.name)), {"lecture.pdf"})
        self.assertEqual(
            self.downloader.index.entries[
                "attachment:course-1:content-1:attachment-1"
            ]["etag"],
            '"version-2"',
        )

    def test_distinct_attachments_with_same_name_keep_both_files(self):
        self.api.get_content_attachments.return_value = [
            {"id": "first", "fileName": "shared.pdf"},
            {"id": "second", "fileName": "shared.pdf"},
        ]

        def respond(course_id, content_id, attachment_id):
            response = Mock()
            response.status_code = 200
            response.headers = CaseInsensitiveDict()
            response.iter_content.return_value = [attachment_id.encode()]
            return response

        self.api.download_attachment.side_effect = respond

        self.downloader._download_attachments(
            "course-1", "content-1", "Lecture", self.directory.name
        )

        with open(os.path.join(self.directory.name, "shared.pdf"), "rb") as saved:
            self.assertEqual(saved.read(), b"first")
        with open(os.path.join(self.directory.name, "shared (1).pdf"), "rb") as saved:
            self.assertEqual(saved.read(), b"second")

    def test_changed_size_updates_file_and_preserves_old_version(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"old")
        self.response(**{"content-length": "8"})

        self.download()
        self.download()

        self.assertEqual(self.downloader.stats["files_downloaded"], 1)
        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(self.downloader.stats["files_updated"], 1)
        self.assertIn(".bb-versions", os.listdir(self.directory.name))
        with open(path, "rb") as saved:
            self.assertEqual(saved.read(), b"complete")

    def test_incomplete_transfer_leaves_no_file(self):
        self.response(b"short", **{"content-length": "8"})

        with patch("downloader.MAX_RETRIES", 1):
            self.download()

        self.assertEqual(self.downloader.stats["files_failed"], 1)
        self.assertEqual(os.listdir(self.directory.name), [])


class ParallelDownloadTests(unittest.TestCase):
    def test_new_same_name_attachment_cannot_replace_existing_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Mock()
            api.get_course_contents.return_value = [{
                "id": "content-1",
                "title": "Files",
                "contentHandler": {"id": "resource/x-bb-file"},
            }]
            api.get_course_announcements.return_value = []
            api.get_content_attachments.return_value = [
                {"id": "old", "fileName": "shared.pdf"}
            ]

            def download(course_id, content_id, attachment_id, headers=None):
                response = Mock()
                response.headers = CaseInsensitiveDict({"ETag": attachment_id})
                if attachment_id == "old" and headers:
                    response.status_code = 304
                else:
                    response.status_code = 200
                    response.iter_content.return_value = [attachment_id.encode()]
                return response

            api.download_attachment.side_effect = download
            first = CourseDownloader(api, directory)
            with patch.object(first, "_download_api", return_value=api):
                first.download_course("course-1", "Course")

            api.get_content_attachments.return_value = [
                {"id": "new", "fileName": "shared.pdf"},
                {"id": "old", "fileName": "shared.pdf"},
            ]
            second = CourseDownloader(api, directory)
            with patch.object(second, "_download_api", return_value=api):
                second.download_course("course-1", "Course")

            folder = os.path.join(directory, "Course")
            with open(os.path.join(folder, "shared.pdf"), "rb") as saved:
                self.assertEqual(saved.read(), b"old")
            with open(os.path.join(folder, "shared (1).pdf"), "rb") as saved:
                self.assertEqual(saved.read(), b"new")

    def test_course_checks_multiple_files_in_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            api = Mock()
            api.get_course_contents.return_value = [
                {
                    "id": f"content-{number}",
                    "title": f"File {number}",
                    "contentHandler": {"id": "resource/x-bb-file"},
                }
                for number in range(4)
            ]
            api.get_content_attachments.side_effect = (
                lambda course_id, content_id: [
                    {"id": content_id, "fileName": f"{content_id}.txt"}
                ]
            )
            api.get_course_announcements.return_value = []
            lock = threading.Lock()
            overlap = threading.Event()
            active = 0
            maximum = 0

            def download(course_id, content_id, attachment_id, headers=None):
                nonlocal active, maximum
                if headers:
                    response = Mock()
                    response.status_code = 304
                    response.headers = CaseInsensitiveDict({"ETag": attachment_id})
                    return response
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active >= 2:
                        overlap.set()
                overlap.wait(timeout=1)
                time.sleep(0.01)
                with lock:
                    active -= 1
                response = Mock()
                response.status_code = 200
                response.headers = CaseInsensitiveDict({"ETag": attachment_id})
                response.iter_content.return_value = [attachment_id.encode()]
                return response

            api.download_attachment.side_effect = download
            downloader = CourseDownloader(api, directory)
            with patch.object(downloader, "_download_api", return_value=api):
                downloader.download_course("course-1", "Course")

            self.assertGreaterEqual(maximum, 2)
            self.assertEqual(downloader.stats["files_downloaded"], 4)

            restarted = CourseDownloader(api, directory)
            with patch.object(restarted, "_download_api", return_value=api):
                restarted.download_course("course-1", "Course")
            self.assertEqual(restarted.stats["files_skipped"], 4)


if __name__ == "__main__":
    unittest.main()
