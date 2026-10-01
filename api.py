"""
Blackboard REST API 客户端
"""

import requests
from rich.console import Console

from config import API_BASE_URL, API_TIMEOUT, DOWNLOAD_TIMEOUT, PAGE_SIZE

console = Console()


class BlackboardAPI:
    """Blackboard Learn REST API 封装"""

    def __init__(self, session: requests.Session):
        self.session = session
        self.base_url = API_BASE_URL

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """发送 GET 请求并返回 JSON"""
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 401:
                console.print("[bold red]✗ 认证已过期，请重新登录[/bold red]")
                raise
            elif status == 403:
                console.print(f"[yellow]⚠ 无权限访问: {endpoint}[/yellow]")
                return {"results": []}
            elif status == 404:
                return {"results": []}
            elif status == 400 and endpoint.endswith("/attachments"):
                try:
                    error = e.response.json()
                except ValueError:
                    error = {}
                message = error.get("message", "") if isinstance(error, dict) else ""
                if isinstance(message, str) and (
                    "does not support file attachments" in message.lower()
                ):
                    return {"results": []}
                console.print(f"[red]✗ API 错误 {status}: {endpoint}[/red]")
                raise
            else:
                console.print(f"[red]✗ API 错误 {status}: {endpoint}[/red]")
                raise
        except requests.exceptions.RequestException as e:
            console.print(f"[red]✗ 请求失败: {e}[/red]")
            raise

    def _get_paginated(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """处理分页，返回所有结果"""
        all_results = []
        if params is None:
            params = {}
        params["limit"] = PAGE_SIZE
        params["offset"] = 0

        while True:
            data = self._get(endpoint, params)
            results = data.get("results", [])
            all_results.extend(results)

            # 检查是否有下一页
            paging = data.get("paging", {})
            next_page = paging.get("nextPage")
            if not next_page or not results:
                break

            # 解析 nextPage URL 中的 offset
            params["offset"] = params["offset"] + len(results)

        return all_results

    def get_current_user(self) -> dict:
        """获取当前登录用户信息"""
        return self._get("/users/me")

    def get_user_courses(self, user_id: str) -> list[dict]:
        """
        获取用户已注册的课程列表

        Args:
            user_id: 用户 ID

        Returns:
            课程成员资格列表
        """
        memberships = self._get_paginated(f"/users/{user_id}/courses")
        return memberships

    def get_course(self, course_id: str) -> dict:
        """获取课程详细信息"""
        return self._get(f"/courses/{course_id}")

    def get_course_contents(self, course_id: str) -> list[dict]:
        """
        获取课程的顶级内容列表

        Args:
            course_id: 课程 ID

        Returns:
            内容项列表
        """
        return self._get_paginated(f"/courses/{course_id}/contents")

    def get_course_announcements(self, course_id: str) -> list[dict]:
        """Get all announcements for a course."""
        return self._get_paginated(f"/courses/{course_id}/announcements")

    def get_content_children(self, course_id: str, content_id: str) -> list[dict]:
        """
        获取内容项的子内容

        Args:
            course_id: 课程 ID
            content_id: 父内容 ID

        Returns:
            子内容项列表
        """
        return self._get_paginated(
            f"/courses/{course_id}/contents/{content_id}/children"
        )

    def get_content_attachments(self, course_id: str, content_id: str) -> list[dict]:
        """
        获取内容项的附件列表

        Args:
            course_id: 课程 ID
            content_id: 内容 ID

        Returns:
            附件列表
        """
        return self._get_paginated(
            f"/courses/{course_id}/contents/{content_id}/attachments"
        )

    def download_attachment(
        self,
        course_id: str,
        content_id: str,
        attachment_id: str,
    ) -> requests.Response:
        """
        下载附件，返回 Response 对象（流式）

        Args:
            course_id: 课程 ID
            content_id: 内容 ID
            attachment_id: 附件 ID

        Returns:
            流式 Response 对象
        """
        url = (
            f"{self.base_url}/courses/{course_id}/contents/{content_id}"
            f"/attachments/{attachment_id}/download"
        )
        resp = self.session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp

    def download_url(self, url: str) -> requests.Response:
        """Download a file from an authenticated Blackboard URL."""
        resp = self.session.get(
            url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp
