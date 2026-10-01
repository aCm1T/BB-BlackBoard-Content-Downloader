# BB BlackBoard Content Downloader

Automatically download **all** course files from [Blackboard Learn](https://www.blackboard.com/) with one command — browser login (SSO/CAS supported), REST API traversal, and incremental file sync.

---

## Table of contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [CLI options](#cli-options)
- [Output](#output)
- [What is not pushed to GitHub](#what-is-not-pushed-to-github)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)
- [License](#license)
- [中文说明](#中文说明)

---

## Features

| Feature | Description |
|---------|-------------|
| **Browser login** | Opens Chrome or Edge for campus SSO — passwords are not stored by this tool |
| **Cookie cache** | Saves `cookies.json` locally; skip the browser on the next run if still valid |
| **`.env` loading** | Reads `BB_BASE_URL` (and options) from a local `.env` via `python-dotenv` |
| **Campus TLS** | Optional compatibility adapter for OpenSSL 3 / strict campus TLS; opt out with `BB_TLS_STRICT` |
| **Course selection** | Lists enrollments; filter by keywords or download everything with `--all` |
| **Folder layout** | Mirrors Blackboard content folders on disk |
| **Incremental sync** | Checks the final response filename and size before streaming; when the server omits size, skips an existing nonempty file |
| **Progress & retries** | Rich progress bars; failed downloads retry with backoff |

---

## Prerequisites

- **Python** ≥ 3.10  
- **Google Chrome** or **Microsoft Edge**  
- A Blackboard Learn account at your institution  

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/<your-account>/BB-BlackBoard-Content-Downloader.git
cd BB-BlackBoard-Content-Downloader
pip install -r requirements.txt
```

Use a virtual environment if you prefer (`python -m venv .venv` then activate it).

### 2. Configure (single template file)

The repo ships **`env.template`** only. Copy it to **`.env`** and edit the URL (do **not** commit `.env`).

**Linux / macOS**

```bash
cp env.template .env
```

**Windows (PowerShell)**

```powershell
Copy-Item env.template .env
```

**Windows (CMD)**

```cmd
copy env.template .env
```

Edit `.env`:

```ini
BB_BASE_URL=https://bb.your-university.edu
# Optional:
# BB_TLS_STRICT=1
```

You can instead export `BB_BASE_URL` (and `BB_TLS_STRICT`) in the shell; the app loads `.env` automatically when present.

### 3. Run

```bash
python main.py
```

- **First time:** a browser window opens — complete your usual campus login. The tool waits until you land on Blackboard, then validates the Public REST API and continues.  
- **Later runs:** if `cookies.json` is still valid, **no browser** opens. Use `python main.py --relogin` or delete `cookies.json` to sign in again.

### 4. Default output

By default files go to **`./downloads/`** (configurable with `-o`). That folder is **gitignored** so course files are not uploaded to GitHub.

---

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `BB_BASE_URL` | Yes* | Root URL of Blackboard, e.g. `https://bb.example.edu` — **no** path after the host. |
| `BB_TLS_STRICT` | No | If `1`, `true`, or `yes`, disables the TLS compatibility adapter for `requests`. |

\*If unset, a placeholder is used and requests will fail until you configure a real base URL.

---

## CLI options

```text
usage: main.py [-h] [-o OUTPUT_DIR] [-c [COURSE ...]] [--relogin] [--all]
```

| Flag | Description | Example |
|------|-------------|---------|
| `-o`, `--output-dir` | Output directory | `python main.py -o D:\BlackboardFiles` |
| `-c`, `--course` | Keywords to filter course names | `python main.py -c "Math" "Physics"` |
| `--all` | Download all courses (no prompt) | `python main.py --all` |
| `--relogin` | Ignore cache; open browser again | `python main.py --relogin` |

**Examples**

```bash
python main.py --all
python main.py -o D:\BlackboardFiles
python main.py -c CSC
python main.py --relogin --all
```

---

## Output

Default tree (simplified):

```text
downloads/
├── Course A/
│   ├── Week 1/
│   │   ├── lecture1.pdf
│   │   └── homework1.docx
│   └── Week 2/
│       └── lecture2.pptx
└── Course B/
    └── ...
```

---

## What is not pushed to GitHub

These paths are listed in **`.gitignore`** so they stay on your machine only:

| Path | Reason |
|------|--------|
| **`downloads/`** | Downloaded teaching materials — large and personal |
| **`.env`** | Your institution URL / flags (copy from `env.template`) |
| **`cookies.json`** | Session cookies — sensitive |
| **`.venv/`**, **`__pycache__/`** | Local environment and bytecode |

**Never** force-add `.env`, `cookies.json`, or `downloads/` to a public repository.

---

## Architecture

```text
main.py          CLI — args, course selection, orchestration
auth.py          Selenium login → cookies → requests session + TLS compat + /users/me check
api.py           Blackboard Learn Public API v1 (pagination, errors)
downloader.py    Recursive contents + streamed attachment downloads
config.py        load_dotenv(".env"), URLs, timeouts, retries
```

**Flow**

```text
Browser login (or valid cookies.json)
  → requests + TLS compat
  → GET /learn/api/public/v1/users/me
  → /users/{id}/courses
  → /courses/{id}/contents (recursive)
  → /attachments/{id}/download
```

---

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| **`SSLV3_ALERT_HANDSHAKE_FAILURE`** with `requests` but Chrome works | Leave default TLS compat on; only set `BB_TLS_STRICT=1` if you know you need strict OpenSSL defaults. |
| Browser login OK, CLI says API / auth failed | Read the printed **HTTP status** and body snippet. **403** on `/learn/api/public/v1` often means the school disabled or restricted the **Public REST API** for your account. |
| No browser window | Cached `cookies.json` is still valid — run `--relogin` or delete `cookies.json`. Check the taskbar; the window may be minimized. |
| Wrong or empty courses | Confirm `BB_BASE_URL` is exactly the Blackboard **site root** (same host you use in the browser). |
| Dependencies / drivers | Install `requirements.txt`; ensure Chrome or Edge is installed. `webdriver-manager` is used when possible. |

---

## Contributing

Issues and pull requests are welcome. For larger changes, please open an issue first to agree on direction.

---

## Disclaimer

This project is for **personal, educational use** — backing up **your own** course materials. Respect your institution’s terms of service and copyright.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## 中文说明

- **环境变量**：仓库里只提供 **`env.template`**。复制为 **`.env`** 后填写 `BB_BASE_URL`；**不要**把 `.env` 推到 GitHub。  
- **下载目录**：默认 **`downloads/`** 已在 `.gitignore` 中，**不会**随仓库上传。  
- **会话**：**`cookies.json`** 含登录态，已忽略，请勿提交。  
- **浏览器**：首次需手动登录；若已缓存有效 Cookie，再次运行可能不弹窗，可用 `python main.py --relogin`。  
- **许可**：见仓库根目录 **[LICENSE](LICENSE)**（MIT）。
