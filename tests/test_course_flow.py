import json
import unittest
from unittest.mock import Mock

import requests

from api import BlackboardAPI
from main import get_available_courses


class CourseFlowTests(unittest.TestCase):
    def test_inaccessible_courses_are_not_shown_as_na_rows(self):
        api = Mock()
        api.get_course.side_effect = [
            {"id": "_1_1", "name": "Course One", "courseId": "COURSE-1"},
            {"results": []},
            {"id": "_3_1", "name": "Unavailable", "courseId": "COURSE-3",
             "availability": {"available": "No"}},
        ]

        courses = get_available_courses(api, [
            {"courseId": "_1_1"}, {"courseId": "_2_1"}, {"courseId": "_3_1"}
        ])

        self.assertEqual([course["name"] for course in courses], ["Course One"])

    def test_attachment_unsupported_400_is_an_empty_list(self):
        session = Mock()
        response = requests.Response()
        response.status_code = 400
        response._content = json.dumps({
            "status": 400,
            "message": "The Content Item does not support file attachments",
        }).encode()
        session.get.return_value = response

        attachments = BlackboardAPI(session).get_content_attachments("c", "i")

        self.assertEqual(attachments, [])

    def test_other_attachment_400_still_raises(self):
        session = Mock()
        response = requests.Response()
        response.status_code = 400
        response._content = json.dumps({"message": "Invalid request"}).encode()
        session.get.return_value = response

        with self.assertRaises(requests.HTTPError):
            BlackboardAPI(session).get_content_attachments("c", "i")


if __name__ == "__main__":
    unittest.main()
