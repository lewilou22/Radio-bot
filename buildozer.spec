[app]

title = Radio Monitor
package.name = radiomonitor
package.domain = org.radiomonitor
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.main = main.py
version = 0.1.0

requirements = python3,kivy==2.3.1,kivymd==1.2.0,filetype,attrs,fuzzysearch,requests,urllib3,certifi,charset_normalizer,idna,pillow,pyjnius,android,tzdata

orientation = portrait
fullscreen = 0

osx.python_version = 3
osx.kivy_version = 1.9.1

[buildozer]
log_level = 2
warn_on_root = 1

[android]
# Required for `adb shell run-as …` and pulling private debug files.
android.debuggable = 1
# Target SDK 34 reduces "built for older Android" warnings on current devices.
android.api = 34
android.minapi = 24
android.ndk = 25b
android.accept_sdk_license = True
# INTERNET only unless you add notifications / a real foreground service.
android.permissions = INTERNET,WAKE_LOCK,FOREGROUND_SERVICE,POST_NOTIFICATIONS
android.archs = arm64-v8a
android.enable_androidx = True

# Copy default config if missing (optional — app creates data dirs at runtime)
# android.add_src = 
