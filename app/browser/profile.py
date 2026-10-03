"""The embedded browser's profile: one per app user, kept on disk.

Logins to jobright, hiring.cafe and ChatGPT live in this profile, so they
survive restarts. Nothing here ever touches the user's own Chrome.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEngineProfile, QWebEngineSettings

from app.config import user_data_dir

#: ChatGPT only writes its reply into the page while the page is painted; these
#: stop Chromium throttling a tab that is covered or in the background.
CHROMIUM_FLAGS = (
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
)


def apply_chromium_flags() -> None:
    """Must run before QApplication is created."""
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    missing = [flag for flag in CHROMIUM_FLAGS if flag not in current]
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join([current, *missing]).strip()


def chrome_user_agent(profile: QWebEngineProfile) -> str:
    """The engine's own user agent with the QtWebEngine token removed.

    The real Chromium version stays, so sites see an ordinary desktop Chrome
    rather than an embedded browser they may refuse.
    """
    parts = [part for part in profile.httpUserAgent().split(" ") if not part.startswith("QtWebEngine/")]
    return " ".join(parts)


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    counter = 1
    while target.exists():
        counter += 1
        target = folder / f"{Path(name).stem} ({counter}){Path(name).suffix}"
    return target


def make_profile(user_id: str, parent: QObject, downloads_dir: Path | None = None) -> QWebEngineProfile:
    """A persistent profile under the user's data folder."""
    storage = user_data_dir(user_id) / "browser"
    storage.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile(f"resume-ai-{user_id[:8] or 'user'}", parent)
    profile.setPersistentStoragePath(str(storage / "storage"))
    profile.setCachePath(str(storage / "cache"))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpUserAgent(chrome_user_agent(profile))
    profile.setHttpAcceptLanguage("en-US,en;q=0.9")
    if hasattr(profile, "setPersistentPermissionsPolicy"):
        # Never remember a permission: every request is answered (refused) by the app.
        profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime)

    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanPaste, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, False)

    def on_download(request: QWebEngineDownloadRequest) -> None:
        folder = downloads_dir or Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        target = unique_path(folder, request.downloadFileName() or "download")
        request.setDownloadDirectory(str(target.parent))
        request.setDownloadFileName(target.name)
        request.accept()

    profile.downloadRequested.connect(on_download)
    return profile
