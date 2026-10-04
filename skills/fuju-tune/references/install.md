# 安装到 Codex 或 Claude Code

编码 Agent 可以从本地 Fuju RSI checkout 执行一次安装，不需要用户手动复制每份资源：

```bash
python3 skills/fuju-tune/scripts/install.py --host both --project /path/to/business-project
```

`--host codex`、`--host claude` 或 `--host both` 必须明确指定。省略 `--project` 时安装到当前用户目录。项目目录必须已经存在；安装不改业务源码、不安装 Python SDK、不调用模型，也不下载远程脚本。

| 宿主 | 用户目录 | 项目目录 |
| --- | --- | --- |
| Codex | `~/.agents/skills/fuju-tune` | `.agents/skills/fuju-tune` |
| Claude Code | `~/.claude/skills/fuju-tune` | `.claude/skills/fuju-tune` |

路径按当前官方说明：[Codex 本地 Skill 目录](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skill 目录](https://code.claude.com/docs/en/skills)。Codex 使用 `.agents/skills`；本脚本不修改旧的 `.codex/skills` 安装。直接安装的 Claude Skill 使用 `/fuju-tune`，插件打包时的命令还会带插件名称。

目录已复制到其他位置时仍可执行其中的 `scripts/install.py`；源目录始终是脚本所在的 Skill，不依赖开发机路径。重复安装相同资源会返回 `unchanged`；已有任何不同文件或额外资源时拒绝覆盖，用户可选择其他位置自行比较。Python 的 `__pycache__`、`.pyc`、`.pyo` 是运行缓存，不复制、不参与版本比较。

测试或手动放到非标准位置时可使用 `--destination /path/to/skill-parent`。单宿主写入 `skill-parent/fuju-tune`；`--host both` 分别写入 `skill-parent/codex/fuju-tune` 和 `skill-parent/claude/fuju-tune`。这个选项仅复制资源，并不让宿主自动发现自定义位置。

安装器拒绝源资源、源路径和目标路径中的符号链接，也拒绝源和目标互相包含。请传真实目录；在 macOS 上，系统提供的临时路径可能经 `/var` 链接，应先使用真实临时目录。安装期间不要由另一进程改动这些目录。

安装摘要会指出目标、是否已存在和下一步：在开发或测试虚拟环境安装已确认来源的 Fuju RSI 源码或 wheel，然后进入业务项目调用 `$fuju-tune`（Codex）或 `/fuju-tune`（Claude Code）。安装检查与真实项目首次评测是两层验证；目录复制成功不代表宿主已经调用 Skill，也不代表业务结果已经正确。
