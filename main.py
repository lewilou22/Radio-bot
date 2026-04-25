"""
Buildozer / python-for-android require this exact filename at the project root.
Delegates to the KivyMD app in mobile/.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback

# Must match buildozer.spec package.domain + "." + package.name
_ANDROID_PACKAGE = "org.radiomonitor.radiomonitor"


def _app_files_path(name: str) -> str:
    return os.path.join(f"/data/data/{_ANDROID_PACKAGE}/files", name)


def _write_app_file(name: str, text: str) -> None:
    """Best-effort; works when this process is the app UID (debug APK)."""
    try:
        p = _app_files_path(name)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _install_crash_hooks() -> None:
    def _from_excepthook(exc_type, exc, tb) -> None:
        try:
            raw = "".join(traceback.format_exception(exc_type, exc, tb))
            _log_crash_to_logcat(raw)
            _try_write_crash_file(raw)
        except Exception:
            pass

    sys.excepthook = _from_excepthook

    hook = getattr(threading, "excepthook", None)
    if hook is not None:

        def _thread_excepthook(args) -> None:  # type: ignore[no-untyped-def]
            try:
                raw = "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                )
                _log_crash_to_logcat(raw)
                _try_write_crash_file(raw)
            except Exception:
                pass
            hook(args)

        threading.excepthook = _thread_excepthook  # type: ignore[assignment]


def _log_crash_to_logcat(text: str) -> None:
    try:
        from jnius import autoclass

        Log = autoclass("android.util.Log")
        tag = "RadioMonitor"
        for line in text.splitlines():
            if line.strip():
                Log.e(tag, line[:4000])
    except Exception:
        pass


def _try_write_crash_file(text: str) -> None:
    """When logcat is empty (OEM restriction), pull with:
    adb shell run-as org.radiomonitor.radiomonitor cat files/last_crash.txt
    """
    paths: list[str] = []
    try:
        from jnius import autoclass

        act = autoclass("org.kivy.android.PythonActivity").mActivity
        if act is not None:
            fd = act.getFilesDir()
            if fd is not None:
                paths.append(
                    os.path.join(str(fd.getAbsolutePath()), "last_crash.txt")
                )
    except Exception:
        pass
    paths.append(f"/data/data/{_ANDROID_PACKAGE}/files/last_crash.txt")
    for p in paths:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return
        except OSError:
            continue


def main() -> None:
    _install_crash_hooks()
    # If this file never appears after launch, Python main() is not the entry (wrong/old APK).
    _write_app_file("entered_main.txt", "ok\n")

    try:
        from mobile.main import main as run_kivy

        _write_app_file("import_mobile_ok.txt", "ok\n")
        run_kivy()
    except BaseException:
        tb = traceback.format_exc()
        _log_crash_to_logcat(tb)
        _try_write_crash_file(tb)
        print(tb)
        raise


# If this file never appears, main.py was not loaded on device (wrong entrypoint/APK).
try:
    import android  # noqa: F401 — present only in python-for-android builds
except ImportError:
    pass
else:
    _write_app_file("p4a_main_py_loaded.txt", "ok\n")


if __name__ == "__main__":
    main()
