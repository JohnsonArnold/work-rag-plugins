"""Install or update the work-rag Antigravity plugin into ~/.gemini/config/plugins/work-rag."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    default_python = repo_root / ".venv-win" / "Scripts" / "python.exe"
    if not default_python.is_file():
        default_python = Path(sys.executable)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=r"D:\work-rag\.work-rag-data", help="Shared work-rag library directory")
    parser.add_argument(
        "--plugin-dir",
        default=str(Path.home() / ".gemini" / "config" / "plugins" / "work-rag"),
        help="Target Antigravity plugin directory",
    )
    parser.add_argument("--python", dest="python_exe", default=str(default_python), help="Absolute Python executable")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        raise SystemExit(f"Data directory must be absolute: {data_dir}")
    python_exe = Path(args.python_exe).expanduser().resolve()
    if not python_exe.is_file():
        raise SystemExit(f"Python executable not found: {python_exe}")

    panel_html = repo_root / "work_rag" / "assets" / "panel.html"
    if not panel_html.is_file():
        raise SystemExit(f"Built panel HTML missing: {panel_html}. Build extensions/antigravity-app first.")

    # Initialize or migrate the shared library cleanly.
    init = subprocess.run(
        [str(python_exe), "-m", "work_rag", "--data-dir", str(data_dir), "init"],
        capture_output=True,
        encoding="utf-8",
        cwd=repo_root,
    )
    if init.returncode != 0:
        raise SystemExit(f"Failed to initialize library: {init.stderr or init.stdout}")

    source_plugin = repo_root / "extensions" / "antigravity-plugin"
    plugin_dir = Path(args.plugin_dir).expanduser().resolve()
    plugin_dir.mkdir(parents=True, exist_ok=True)

    for folder in ("assets", "skills", "rules"):
        src = source_plugin / folder
        dst = plugin_dir / folder
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            shutil.copytree(src, dst)

    for filename in ("plugin.json", "README.md"):
        shutil.copy2(source_plugin / filename, plugin_dir / filename)

    mcp_config = {
        "mcpServers": {
            "work-rag": {
                "command": str(python_exe),
                "args": [
                    "-m",
                    "work_rag",
                    "--data-dir",
                    str(data_dir),
                    "mcp",
                    "--enable-app",
                ],
                "env": {},
            }
        }
    }
    (plugin_dir / "mcp_config.json").write_text(
        json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Installed Antigravity plugin: {plugin_dir}")
    print(f"Shared data directory:        {data_dir}")
    print(f"Python executable:            {python_exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
