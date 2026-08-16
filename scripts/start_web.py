"""启动前后端开发服务。

后端通过 Windows 计划任务（AgentflowBackend）托管，避免进程被环境回收；
前端用 node 直接跑 Vite（分离进程）。日志在 .appdata/。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
APPDATA = PROJECT_ROOT / ".appdata"
FRONTEND = PROJECT_ROOT / "frontend"

FLAGS = (
    subprocess.CREATE_NO_WINDOW
    | subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NEW_PROCESS_GROUP
)


def launch(args: list[str], cwd: Path, log_name: str) -> int:
    APPDATA.mkdir(parents=True, exist_ok=True)
    log = open(APPDATA / log_name, "w", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=FLAGS,
        close_fds=True,
    )
    return proc.pid


def main() -> None:
    # 注册并启动后端计划任务（系统托管，抗回收）
    action = (
        f'New-ScheduledTaskAction -Execute "{PYTHON}" '
        f'-Argument "\\"{PROJECT_ROOT / "scripts" / "run_backend.py"}\\"" '
        f'-WorkingDirectory "{PROJECT_ROOT}"'
    )
    trigger = "New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)"
    register = (
        f"Register-ScheduledTask -TaskName AgentflowBackend "
        f"-Action ({action}) -Trigger ({trigger}) -Force"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", register],
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Start-ScheduledTask -TaskName AgentflowBackend",
        ],
        capture_output=True,
        timeout=60,
    )
    # 分离模式下 npm.cmd 不可靠，直接用 node 跑 Vite CLI
    frontend_pid = launch(
        ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1"],
        FRONTEND,
        "frontend.log",
    )
    (APPDATA / "web.pids").write_text(
        f"backend=task:AgentflowBackend\nfrontend={frontend_pid}\n", encoding="utf-8"
    )
    print("backend=计划任务 AgentflowBackend")
    print(f"frontend_pid={frontend_pid}")
    print("前端    : http://localhost:5173  （默认账号 admin / admin）")
    print("后端 API: http://localhost:8000")
    print("接口文档: http://localhost:8000/docs")


if __name__ == "__main__":
    main()
