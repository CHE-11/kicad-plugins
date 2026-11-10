# Copy folder easyeda2kicad to plugins directory using the PLUGINS_DIR env variable
# Copy file LCSC Importer.py to plugins directory

import os
import shutil
from pathlib import Path

script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent

env_file = project_root / ".env"

if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

plugins_dir = os.environ.get("PLUGINS_DIR")
if not plugins_dir:
    print("Error: PLUGINS_DIR is not set. Set it in the .env file or as an environment variable.")
    raise SystemExit(1)

plugins_path = Path(plugins_dir)
if not plugins_path.exists():
    print(f"Error: PLUGINS_DIR does not exist: {plugins_path}")
    raise SystemExit(1)

easyeda_src = script_dir / "easyeda2kicad"
if not easyeda_src.is_dir():
    print(f"Error: missing package folder: {easyeda_src}")
    raise SystemExit(1)

easyeda_dst = plugins_path / "easyeda2kicad"
shutil.copytree(easyeda_src, easyeda_dst, dirs_exist_ok=True)
print(f"Copied folder {easyeda_src.name}/ -> {easyeda_dst}")

lcsc_src = script_dir / "LCSC Importer.py"
if not lcsc_src.is_file():
    print(f"Error: missing file: {lcsc_src}")
    raise SystemExit(1)

lcsc_dst = plugins_path / "LCSC Importer.py"
shutil.copy2(lcsc_src, lcsc_dst)
print(f"Copied {lcsc_src.name} -> {lcsc_dst}")
