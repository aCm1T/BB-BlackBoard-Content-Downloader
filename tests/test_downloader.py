import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from downloader import CourseDownloader


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
        response.headers = headers
        response.iter_content.return_value = [data]
        self.api.download_attachment.return_value = response
        return response

    def test_skips_existing_attachment_using_response_size(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"complete")
        response = self.response(**{"content-length": "8"})

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(os.listdir(self.directory.name), ["lecture.pdf"])
        response.iter_content.assert_not_called()
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

    def test_unknown_size_skips_nonempty_existing_file(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"complete")
        response = self.response()

        self.download()

        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        response.iter_content.assert_not_called()

    def test_changed_size_keeps_distinct_version_and_skips_it_next_run(self):
        path = os.path.join(self.directory.name, "lecture.pdf")
        with open(path, "wb") as output:
            output.write(b"old")
        self.response(**{"content-length": "8"})

        self.download()
        self.download()

        self.assertEqual(self.downloader.stats["files_downloaded"], 1)
        self.assertEqual(self.downloader.stats["files_skipped"], 1)
        self.assertEqual(
            set(os.listdir(self.directory.name)),
            {"lecture.pdf", "lecture (1).pdf"},
        )
        with open(os.path.join(self.directory.name, "lecture (1).pdf"), "rb") as saved:
            self.assertEqual(saved.read(), b"complete")

    def test_incomplete_transfer_leaves_no_file(self):
        self.response(b"short", **{"content-length": "8"})

        with patch("downloader.MAX_RETRIES", 1):
            self.download()

        self.assertEqual(self.downloader.stats["files_failed"], 1)
        self.assertEqual(os.listdir(self.directory.name), [])


if __name__ == "__main__":
    unittest.main()
