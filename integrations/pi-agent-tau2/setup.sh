#!/usr/bin/env bash
# pi-agent × tau2-bench 调优底座环境准备。
# 前置：node >= 20、uv、网络。全部产物落在本目录（.vendor/ 与 node_modules/ 已 gitignore）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TAU2_SHA="${TAU2_SHA:-2174a603f6d014ef94473ffa95957f6ce27100db}"

cd "$ROOT"

if [ ! -d .vendor/tau2-bench ]; then
  mkdir -p .vendor
  git clone https://github.com/sierra-research/tau2-bench .vendor/tau2-bench
fi

cd .vendor/tau2-bench
CURRENT_SHA="$(git rev-parse HEAD)"
if [ "$CURRENT_SHA" != "$TAU2_SHA" ]; then
  echo "tau2-bench HEAD $CURRENT_SHA != 固定 $TAU2_SHA；如需更新请同时更新 casebook 版本与固定 SHA。" >&2
fi
uv sync
# 文本评测只需要 websockets 可选依赖；voice 组很重，不安装。
uv pip install --python .venv/bin/python websockets
uv pip install --python .venv/bin/python -e "$ROOT/../.."

cd "$ROOT/agent-node"
if [ ! -d node_modules ] || ! grep -q pi-ai package-lock.json 2>/dev/null; then
  npm install
fi

echo "环境就绪。下一步："
echo "  (node agent-node/src/server.mjs &)"
echo "  .vendor/tau2-bench/.venv/bin/python python/run_task.py --domain mock --task-ids create_task_1 --prompt-file candidates/exact-title-v1.txt"
