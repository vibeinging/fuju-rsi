"""把独立 RSI 控制台产物同步进 Python wheel。"""
from pathlib import Path
import shutil


root = Path(__file__).resolve().parents[1]
source = root / "console" / "dist"
target = root / "src" / "fuju_rsi" / "console_dist"
if not (source / "index.html").is_file():
    raise SystemExit("先运行 cd console && npm ci && npm run build")
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
print(target)
