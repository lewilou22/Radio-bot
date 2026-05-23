"""Android path setup before importing config-backed modules."""

from __future__ import annotations


def configure_android_paths() -> bool:
    """Point app storage at the Android private files directory."""
    try:
        from jnius import autoclass

        from config import ensure_app_initialized, reload_paths_after_mobile_base, set_mobile_base_dir

        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        if activity is None:
            return False
        files_dir = activity.getFilesDir()
        if files_dir is None:
            return False
        set_mobile_base_dir(str(files_dir.getAbsolutePath()))
        reload_paths_after_mobile_base()
        ensure_app_initialized()
        return True
    except Exception:
        return False


def init_app_environment() -> None:
    configure_android_paths()
    from config import apply_secrets_to_environ, ensure_app_initialized, refresh_env_keys

    ensure_app_initialized()
    apply_secrets_to_environ()
    refresh_env_keys()
