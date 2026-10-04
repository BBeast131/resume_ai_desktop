# PyInstaller spec for Resume AI (Windows).   pyinstaller resume_ai.spec
# See docs/PACKAGING.md.
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ("app/automation/js/*.js", "app/automation/js"),
    ("app/data/alembic", "app/data/alembic"),
    ("app/assets/icon.ico", "app/assets"),
    ("app/assets/icon.png", "app/assets"),
    (".env.example", "."),
]
datas += collect_data_files("alembic")

a = Analysis(
    ["app/main.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=["keyring.backends.Windows", "win32timezone", "sqlalchemy.dialects.sqlite", "logging.config"],
    excludes=["tests", "spike", "tools"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ResumeAI", console=False, icon="app/assets/icon.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="ResumeAI")
