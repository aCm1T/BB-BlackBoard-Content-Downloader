import json
import unittest
from unittest.mock import Mock, patch

import requests

from api import BlackboardAPI
from main import get_available_courses, select_courses


class CourseFlowTests(unittest.TestCase):
    def test_checkbox_selects_multiple_courses_in_display_order(self):
        courses = [
            {"id": "1", "name": "Course One", "courseId": "ONE"},
            {"id": "2", "name": "Course Two", "courseId": "TWO"},
            {"id": "3", "name": "Course Three", "courseId": "THREE"},
        ]
        with patch("main.questionary.checkbox") as checkbox:
            checkbox.return_value.ask.return_value = [2, 0]

            selected = select_courses(courses)

        self.assertEqual(selected, [courses[0], courses[2]])
        choices = checkbox.call_args.kwargs["choices"]
        self.assertEqual([choice.value for choice in choices], [0, 1, 2])
        self.assertEqual(
            [choice.title for choice in choices],
            ["Course One", "Course Two", "Course Three"],
        )
        validator = checkbox.call_args.kwargs["validate"]
        self.assertEqual(validator([]), "请至少选择一门课程")
        self.assertTrue(validator([0]))

    def test_duplicate_course_names_show_ids_in_checkbox(self):
        courses = [
            {"id": "1", "name": "Seminar", "courseId": "SEM-1"},
            {"id": "2", "name": "Seminar", "courseId": "SEM-2"},
        ]
        with patch("main.questionary.checkbox") as checkbox:
            checkbox.return_value.ask.return_value = [1]
            select_courses(courses)

        choices = checkbox.call_args.kwargs["choices"]
        self.assertEqual(
            [choice.title for choice in choices],
            ["Seminar  (SEM-1)", "Seminar  (SEM-2)"],
        )

    def test_checkbox_cancellation_exits_without_downloading(self):
        courses = [{"id": "1", "name": "Course One", "courseId": "ONE"}]
        with patch("main.questionary.checkbox") as checkbox:
            checkbox.return_value.ask.return_value = None
            with self.assertRaises(SystemExit) as exit_context:
                select_courses(courses)
        self.assertEqual(exit_context.exception.code, 0)

    def test_course_keyword_still_selects_without_prompt(self):
        courses = [
            {"id": "1", "name": "Math", "courseId": "MATH"},
            {"id": "2", "name": "Physics", "courseId": "PHYS"},
        ]
        with patch("main.questionary.checkbox") as checkbox:
            selected = select_courses(courses, ["math"])
        self.assertEqual(selected, [courses[0]])
        checkbox.assert_not_called()

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
