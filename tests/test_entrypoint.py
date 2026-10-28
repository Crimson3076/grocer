"""entrypoint.sh: the browser view must always be password protected."""
import os
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path, password):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    data = tmp_path / "data"
    log = tmp_path / "x11vnc.log"
    for name in ("Xvfb", "fluxbox", "websockify", "pkill"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "x11vnc").write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
    (bin_dir / "uvicorn").write_text("#!/bin/sh\nsleep 1\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "DATA_DIR": str(data),
           "GROCER_ENV_FILE": str(tmp_path / "none.env")}
    if password:
        env["VNC_PASSWORD"] = password
    subprocess.run(["bash", str(ROOT / "entrypoint.sh")], env=env, check=True, timeout=30,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(50):
        if log.exists() and "-rfbauth" in log.read_text():
            break
        time.sleep(0.1)
    return data, log.read_text()


def test_generated_password_is_saved_privately_and_vnc_needs_it(tmp_path):
    data, calls = _run(tmp_path, "")
    pw_file = data / "vnc-password.txt"
    assert len(pw_file.read_text().strip()) == 8
    assert stat.S_IMODE(pw_file.stat().st_mode) == 0o600
    assert "-rfbauth" in calls
    assert "-nopw" not in calls


def test_configured_password_is_used_and_not_written_to_a_file(tmp_path):
    data, calls = _run(tmp_path, "letmein1")
    assert not (data / "vnc-password.txt").exists()
    assert "-rfbauth" in calls
    assert "-nopw" not in calls
