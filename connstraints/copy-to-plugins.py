# Copy this file to the plugins directory using the PLUGINS_DIR env variable
# If the file already exists, overwrite it
# Replace the PLACEHOLDER_FOR_OPENAI_API_KEY and PLACEHOLDER_FOR_OPENAI_MODEL with the actual values

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

source = script_dir / "connstraints.py"
destination = plugins_path / "connstraints.py"

api_key = os.environ.get("OPENAI_API_KEY", "")
model = os.environ.get("OPENAI_MODEL", "gpt-5.4")

if not api_key:
    print("Warning: OPENAI_API_KEY is not set. Set it in the .env file or as an environment variable.")

content = source.read_text(encoding="utf-8")
content = content.replace("PLACEHOLDER_FOR_OPENAI_API_KEY", f'"{api_key}"')
content = content.replace("PLACEHOLDER_FOR_OPENAI_MODEL", f'"{model}"')
destination.write_text(content, encoding="utf-8")

print(f"Copied {source.name} -> {destination}")


icon_source = script_dir / "connstraints-icon.png"
if icon_source.exists():
    icon_dest = plugins_path / "connstraints-icon.png"
    shutil.copy2(icon_source, icon_dest)
    print(f"Copied {icon_source.name} -> {icon_dest}")