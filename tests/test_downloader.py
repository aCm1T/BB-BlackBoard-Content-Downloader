import os
import tempfile
import unittest
from unittest.mock import Mock, patch

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


if __name__ == "__main__":
    unittest.main()
