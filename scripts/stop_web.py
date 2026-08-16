"""停止前后端开发服务（停止计划任务 + 终止前端进程树）。"""

from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Stop-ScheduledTask -TaskName AgentflowBackend",
        ],
        capture_output=True,
        timeout=60,
    )
    print("已停止计划任务 AgentflowBackend")
    pids_file = Path(__file__).resolve().parents[1] / ".appdata" / "web.pids"
    if not pids_file.exists():
        print("未找到运行记录（可能尚未启动）")
        return
    for line in pids_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, pid = line.split("=")
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
            print(f"已停止 {key} (pid={pid})")
        except Exception as exc:  # noqa: BLE001
            print(f"{key} (pid={pid}) 停止失败：{exc}")
    pids_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
