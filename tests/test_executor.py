from agentflow.agents.executor import strip_code_fence
from agentflow.core.executor import LocalBackend, static_scan


def test_strip_code_fence():
    code = '```python\nprint("hello")\n```'
    assert strip_code_fence(code) == 'print("hello")'
    assert strip_code_fence("print('x')") == "print('x')"


def test_static_scan_detects_dangerous_patterns():
    assert static_scan("import os; os.system('whoami')")
    assert static_scan("print('hello')") == []


def test_execute_simple_script(tmp_path):
    backend = LocalBackend()
    out = backend.execute("print('hello')", work_dir=tmp_path / "task1")
    assert out.success
    assert "hello" in out.stdout


def test_execute_timeout_terminates(tmp_path):
    backend = LocalBackend()
    code = "import time; time.sleep(10)"
    out = backend.execute(code, work_dir=tmp_path / "task2", timeout=1)
    assert not out.success
    assert out.timed_out


def test_env_whitelist_excludes_secrets(tmp_path):
    backend = LocalBackend()
    code = "import os; print('DATA_PATH' in os.environ, 'OPENAI_API_KEY' in os.environ)"
    out = backend.execute(
        code, work_dir=tmp_path / "task3", env={"DATA_PATH": "data.csv"}
    )
    assert out.success
    assert "True False" in out.stdout
