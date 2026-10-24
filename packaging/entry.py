"""PyInstaller entry point for both executables (PhonePilot.exe = windowed GUI, phonepilot-cli.exe = console)."""

from phonepilot.__main__ import main

raise SystemExit(main())
