# ============================================================
# FILE: process_utils.py
# Same process-tree killing logic already proven in run_pipeline.py.
# Shared here so topic_queue.py's stoppable stages use it too.
# ============================================================

import os
import signal
import subprocess


def kill_process_tree(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def popen_for_stage(command, log_file, cwd=None, env=None):
    """Starts a subprocess in its own process group so
    kill_process_tree can actually terminate it (and any children)."""
    popen_kwargs = {"stdout": log_file, "stderr": subprocess.STDOUT}
    if cwd:
        popen_kwargs["cwd"] = cwd
    if env is not None:
        popen_kwargs["env"] = env
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(command, **popen_kwargs)