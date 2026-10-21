# 小雨 · 本地 AI 陪伴

在本机浏览器里与可自定义的角色聊天。项目支持会话摘要、可编辑长期记忆、Obsidian Markdown 笔记检索，以及兼容 OpenAI 格式的聊天和向量模型接口。

本公开版只包含本机网页应用。请仅导入你有权使用的笔记，避免将隐私内容或 API 密钥提交到公开仓库。

## 快速开始

需要 Python 3.11 或更新版本。

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

macOS/Linux:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

Windows 也可双击 `start.bat`，或运行 `start.ps1`。默认访问 `http://127.0.0.1:8765/`，按 `Ctrl+C` 停止服务。如端口已占用，可在启动命令后加 `--port 8766`。

首次启动为规则演示模式，无需密钥。要使用真实模型，在“连接与设置”中填写兼容 OpenAI 的 API 基础地址、模型名和密钥，再切换为“真实模型”。请按所选模型供应商的控制台信息填写；向量检索需要另行配置支持的 embedding 模型。不开启向量检索时，笔记仍可用本机关键词检索。

人物卡在“人物设定”中编辑。默认角色是“小雨”，设定仅为虚构陪伴，不代表真实人物。聊天、设置、记忆和索引默认存放在本机 `data/`；可通过 `COMPANION_DATA_DIR` 指定其他目录。笔记同步只读扫描指定目录中的 Markdown 文件，不修改原笔记。密钥和私人笔记都不应加入 Git。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Windows 把上述 `.venv/bin/python` 改为 `.venv\Scripts\python`。

本项目按 MIT License 开源。界面图标使用 Lucide 风格与部分图形，相关许可和 Feather 派生图标声明见 [LICENSES/LUCIDE.txt](LICENSES/LUCIDE.txt)。第三方模型服务与笔记内容仍受各自的服务条款和许可约束。
