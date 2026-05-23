# Radio Contest Monitor

Live radio transcription, keyword / Groq contest detection, Discord alerts, ICY metadata, weather/gas parsing, and FmStream.org search.

- **Desktop**: Tk app (`gui_monitor.py`) or PyInstaller **Windows** folder build.
- **Android**: KivyMD app (`mobile/main.py`) + **Buildozer** APK (cloud transcription on device).

## Desktop (Tk)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python gui_monitor.py
```

### Transcription backend (desktop)

- **Local (default)**: `faster-whisper` — set `MODEL_SIZE`, `WHISPER_DEVICE`, etc.
- **Cloud**: set `TRANSCRIBE_BACKEND=groq` or `openai` and `GROQ_API_KEY` / `OPENAI_API_KEY` so all workers share one API transcriber (lighter RAM).

## KivyMD UI (Android + desktop)

```bash
pip install -r mobile/requirements.txt
PYTHONPATH=. python mobile/main.py
```

Material-style **Monitor** / **Stations** / **Settings** tabs: start/stop, transcript + alerts, station editor (with FmStream search), secrets (`secrets.json` under the app user data dir), Groq contest-AI toggle.

### Android APK (Buildozer)

On Linux (or WSL with USB / artifact install):

1. Install [Buildozer](https://buildozer.readthedocs.io/) dependencies (Java JDK, Android SDK/NDK are fetched automatically on first build).
2. From **this directory** (`audio-stream-monitor`):

   ```bash
   buildozer android debug
   ```

3. Install `bin/*debug*.apk` on the device.

**Important:** On Android, `faster-whisper` is not bundled. Set **TRANSCRIBE_BACKEND** to `api` (default on Android), enter **GROQ_API_KEY** or **OPENAI_API_KEY** in **Settings**, then **Save**. Chunks are sent to Groq/OpenAI speech-to-text over HTTPS (same behavior you would get from their Whisper APIs).

Optional: run the **Radio monitor Android APK** GitHub Actions workflow (long first build while SDK/NDK download).

## Windows `.exe` (PyInstaller)

See `build_windows.bat` and `radio_monitor.spec`. Distribute the whole `dist/RadioMonitor` folder. Workflow: `.github/workflows/radio-monitor-windows.yml`.

## Notes

- **FFmpeg** on desktop helps MP3 decoding with local Whisper; optional for API mode.
- **Secrets** are not committed; use env vars (desktop) or in-app Settings (mobile → `secrets.json`).
- **Architecture**: shared logic lives in `radio_engine.py` and `transcribers.py`; Tk and Kivy are thin front ends.
