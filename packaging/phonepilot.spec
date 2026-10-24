# PyInstaller spec: one folder (dist/PhonePilot) with two executables sharing the same runtime.
#   PhonePilot.exe       windowed GUI (no console)
#   phonepilot-cli.exe   console: mcp-stdio relay for Claude, init, doctor, setup-claude
# Build: python -m PyInstaller packaging/phonepilot.spec --noconfirm   (scripts/build_release.ps1 does this)

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "src" / "phonepilot" / "gui" / "resources" / "app.ico")

datas = [
    (str(ROOT / "vendor"), "vendor"),  # adb + scrcpy-server (scripts/fetch_vendor.py must have run)
    (str(ROOT / "skills"), "skills"),
    (str(ROOT / "policy"), "policy"),
    (str(ROOT / ".env.example"), "."),
    (str(ROOT / "secrets.meta.example.yaml"), "."),
    (str(ROOT / "config.example.yaml"), "."),
    (str(ROOT / "src" / "phonepilot" / "gui" / "resources"), "phonepilot/gui/resources"),
]
# imported lazily or by string (uvicorn loaders, MCP transports, CLI subcommands)
hidden = [
    *collect_submodules("phonepilot"),
    *collect_submodules("uvicorn"),
    *collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli")),  # mcp.cli needs typer; unused
    "mcp_types",
]
# Qt modules the app never imports; WebEngine alone is ~400 MB
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
    "tkinter", "pytest", "hypothesis",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(icon=ICON, version=str(ROOT / "build" / "version_info.txt"), upx=False, exclude_binaries=True)
gui = EXE(pyz, a.scripts, [], name="PhonePilot", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="phonepilot-cli", console=True, **common)

coll = COLLECT(gui, cli, a.binaries, a.datas, name="PhonePilot", upx=False)
