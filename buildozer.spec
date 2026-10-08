[app]

title = Аква
package.name = aqua
package.domain = org.aqua

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,md,txt
source.include_patterns = patches/*.py

version = 2.0

requirements = python3,kivy,gtts,pyjnius,requests,urllib3,chardet,idna,certifi

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/icon.png

android.permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,RECORD_AUDIO

android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a

android.accept_sdk_license = True

android.allow_backup = True
android.logcat_filters = *:S python:D
