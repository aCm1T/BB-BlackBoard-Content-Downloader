"""
Blackboard Content Downloader
自动下载 Blackboard Learn 课程的全部文件

CLI 入口
"""

import argparse
import sys
from collections import Counter

import questionary
from rich.console import Console
from rich.panel import Panel

from auth import get_session
from api import BlackboardAPI
from downloader import CourseDownloader
from config import BB_BASE_URL, DEFAULT_DOWNLOAD_DIR

console = Console()

BANNER = r"""
[bold cyan]
  ____  ____    ____                    _                 _
 | __ )| __ )  |  _ \  _____      ____| | ___   __ _  __| | ___ _ __
 |  _ \|  _ \  | | | |/ _ \ \ /\ / / _` |/ _ \ / _` |/ _` |/ _ \ '__|
 | |_) | |_) | | |_| | (_) \ V  V / (_| | (_) | (_| | (_| |  __/ |
 |____/|____/  |____/ \___/ \_/\_/ \__,_|\___/ \__,_|\__,_|\___|_|
[/bold cyan]
[dim]Blackboard Content Downloader[/dim]
"""


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Blackboard 课程内容自动下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_DOWNLOAD_DIR,
        help=f"下载保存目录 (默认: {DEFAULT_DOWNLOAD_DIR})",
    )
    parser.add_argument(
        "-c", "--course",
        nargs="*",
        help="只下载指定课程（输入课程名称关键词，支持多个）",
    )
    parser.add_argument(
        "--relogin",
        action="store_true",
        help="强制重新登录（忽略缓存的 Cookie）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="下载所有课程（不询问选择）",
    )
    return parser.parse_args()


def select_courses(courses: list[dict], keywords: list[str] | None = None) -> list[dict]:
    """
    通过关键词或交互式复选框选择课程

    Args:
        courses: 全部课程列表
        keywords: 过滤关键词

    Returns:
        选中的课程列表
    """
    if keywords:
        filtered = []
        for course in courses:
            name = course.get("name", "").lower()
            course_id_str = course.get("courseId", "").lower()
            if any(kw.lower() in name or kw.lower() in course_id_str for kw in keywords):
                filtered.append(course)
        if filtered:
            console.print(f"[cyan]🔍 匹配到 {len(filtered)} 个课程:[/cyan]")
            return filtered
        else:
            console.print("[yellow]⚠ 没有匹配的课程，显示全部课程[/yellow]")

    name_counts = Counter(course["name"] for course in courses)
    choices = [
        questionary.Choice(
            title=(
                course["name"]
                if name_counts[course["name"]] == 1
                else f"{course['name']}  ({course['courseId']})"
            ),
            value=index,
        )
        for index, course in enumerate(courses)
    ]
    try:
        selected = questionary.checkbox(
            "请选择要下载的课程",
            choices=choices,
            instruction="↑↓ 移动，空格勾选，回车确认；Ctrl+C 退出",
            validate=lambda values: bool(values) or "请至少选择一门课程",
        ).ask()
    except KeyboardInterrupt:
        selected = None

    if selected is None:
        console.print("[dim]👋 已退出[/dim]")
        sys.exit(0)

    selected_indices = set(selected)
    return [course for index, course in enumerate(courses) if index in selected_indices]


def get_available_courses(api: BlackboardAPI, memberships: list[dict]) -> list[dict]:
    """Return accessible courses with the fields needed for selection and download."""
    courses = []
    for membership in memberships:
        course_id = membership.get("courseId", "")
        if not course_id:
            continue
        try:
            course = api.get_course(course_id)
        except Exception:
            continue
        if not isinstance(course, dict) or not all(
            course.get(field) for field in ("id", "name", "courseId")
        ):
            continue
        if (course.get("availability") or {}).get("available") == "No":
            continue
        courses.append(course)
    return courses


def main() -> None:
    """主函数"""
    args = parse_args()

    console.print(BANNER)
    console.print(
        Panel(
            f"[bold]Blackboard URL:[/bold] {BB_BASE_URL}\n"
            f"[bold]下载目录:[/bold] {args.output_dir}",
            title="⚙️  配置",
            border_style="blue",
        )
    )

    # 1. 登录认证
    console.print("\n[bold]📌 第一步: 登录认证[/bold]")
    try:
        session = get_session(force_relogin=args.relogin)
    except Exception as e:
        console.print(f"[bold red]✗ 登录失败: {e}[/bold red]")
        sys.exit(1)

    # 2. 获取用户信息
    console.print("\n[bold]📌 第二步: 获取课程列表[/bold]")
    api = BlackboardAPI(session)

    try:
        user = api.get_current_user()
        user_name = user.get("name", {})
        display_name = user_name.get("given", "") + " " + user_name.get("family", "")
        user_id = user.get("id", "")
        console.print(f"[green]✓ 欢迎, {display_name.strip()}![/green]")
    except Exception as e:
        console.print(f"[bold red]✗ 获取用户信息失败: {e}[/bold red]")
        sys.exit(1)

    # 3. 获取课程列表
    try:
        memberships = api.get_user_courses(user_id)
    except Exception as e:
        console.print(f"[bold red]✗ 获取课程列表失败: {e}[/bold red]")
        sys.exit(1)

    if not memberships:
        console.print("[yellow]⚠ 没有找到任何已注册的课程[/yellow]")
        sys.exit(0)

    # 获取每个课程的详细信息
    courses = get_available_courses(api, memberships)

    if not courses:
        console.print("[yellow]⚠ 没有可用的课程[/yellow]")
        sys.exit(0)

    console.print(f"[green]✓ 找到 {len(courses)} 个可用课程[/green]")

    # 4. 选择课程
    if args.all:
        selected_courses = courses
    else:
        selected_courses = select_courses(courses, args.course)

    console.print(
        f"\n[bold green]✓ 将下载 {len(selected_courses)} 个课程的内容[/bold green]"
    )

    # 5. 开始下载
    console.print("\n[bold]📌 第三步: 下载文件[/bold]")
    dl = CourseDownloader(api, download_dir=args.output_dir)

    for course in selected_courses:
        course_id = course.get("id", "")
        course_name = course.get("name", "Unknown Course")
        dl.download_course(course_id, course_name)

    # 6. 显示统计
    dl.print_stats()
    console.print("\n[bold green]🎉 全部完成！[/bold green]")
    console.print(f"[dim]文件保存在: {args.output_dir}[/dim]")


if __name__ == "__main__":
    main()
