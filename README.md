# 中文语音助手

一个基于命令行的中文语音助手：本地录音 → 本地语音识别 → 调用大模型（可联网搜索）→ 本地语音合成播放。

整个流程跑在一个 `script.py` 里，适合在 SSH 终端或本地直接运行。

## 功能

- 按键控制录音（`s` 开始 / `e` 结束 / `q` 退出）
- 使用 [Vosk](https://alphacephei.com/vosk/) 在本地完成中文语音识别，无需联网
- 调用 DeepSeek 大模型生成回答，遇到天气、新闻、价格、赛事等实时信息会自动调用 [Tavily](https://tavily.com/) 联网搜索
- 使用 [Piper](https://github.com/rhasspy/piper) 在本地合成中文语音并实时播放
- 录音、识别、问答、播放各跑一个线程，通过队列串起来，互不阻塞

## 环境要求

- Python 3
- macOS / Linux，需要可用的麦克风和扬声器
- PyAudio 依赖 PortAudio，macOS 上可用 `brew install portaudio` 安装
- 两个 API Key：
  - `DEEPSEEK_API_KEY` — DeepSeek 平台申请
  - `TAVILY_API_KEY` — Tavily 平台申请

## 安装

```bash
# 创建并激活虚拟环境（如果 .venv 还没建）
python3.14 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

仓库根目录放置运行需要的模型文件：

例如：

- `vosk-model-small-cn-0.22/` — Vosk 中文识别模型
- `zh_CN-huayan-medium.onnx` + `.json` — Piper 中文 TTS 音色（当前使用）

如果是从 git 重新拉的、模型文件不在，可以分别从 [Vosk 模型库](https://alphacephei.com/vosk/models) 和 [Piper 音色库](https://github.com/rhasspy/piper/blob/master/VOICES.md) 下载，放回原路径即可。

## 运行

```bash
export DEEPSEEK_API_KEY=你的key
export TAVILY_API_KEY=你的key

.venv/bin/python script.py
```

启动后会看到提示：

```
=====================================
  SSH 环境 录音工具
  s + 回车 = 开始录音
  e + 回车 = 停止录音
  q + 回车 = 退出程序
=====================================
```

正常一次交互的流程：

1. 输入 `s` 回车，开始录音，对着麦克风说话
2. 输入 `e` 回车，结束录音
3. 终端会依次打印：识别结果 → AI 回答 → 开始播放
4. 想再问一句，重复 1–3
5. 输入 `q` 回车退出

## 工作原理

```
键盘线程 ──s──▶ 录音线程 ──frames──▶ record_queue
                                       │
                                       ▼
                                   识别线程 ──text──▶ question_queue
                                                        │
                                                        ▼
                                                    AI 线程 ──answer──▶ answer_queue
                                                                            │
                                                                            ▼
                                                                        播放线程
```

- 录音线程每次按 `s` 临时启动，按 `e` 退出，把整段音频丢进队列
- 识别线程拿到音频后用 Vosk 跑一遍，得到中文文本
- AI 线程把文本发给 DeepSeek。系统提示里会注入当前时间和地点（广东佛山），并要求回答不要包含 Markdown 和 emoji，方便 TTS 朗读
- 如果 DeepSeek 判断需要查最新信息（天气、新闻、股价等），会触发 `web_search` 工具调用，由 Tavily 实际去搜索，再把结果回喂给模型；最多循环 5 轮直到出最终答案
- 播放线程用 Piper 把回答流式合成成 PCM，边合成边写到 PyAudio 输出流播放

## 配置项

要改行为时直接编辑 `script.py` 里这些位置：

| 改什么 | 在哪里改 |
| --- | --- |
| 录音参数（采样率、声道、块大小） | 文件顶部的 `CHUNK / FORMAT / CHANNELS / RATE` |
| 队列容量（默认每个 3） | `record_queue / question_queue / answer_queue` 的初始化 |
| 大模型与推理参数 | `ask_ai()` 里的 `client.chat.completions.create(...)` |
| 系统提示词（人设、地点、格式约束） | `ask_ai()` 里的 `prompt` 字符串 |
| 联网搜索行为 | `run_web_search()`，`max_results` 与 `search_depth` |
| 切换 TTS 音色 | 顶部的 `voice = PiperVoice.load(...)`，换成 `zh_CN-xiao_ya-medium.onnx` 即可 |

## 常见问题

**没声音 / 录不到**：检查系统输入输出设备是否被其他应用占用；macOS 第一次运行需要在「系统设置 → 隐私与安全性 → 麦克风」里给终端授权。

**`OSError: PortAudio not found`**：`brew install portaudio` 后重装 PyAudio。

**识别结果是空的**：录音太短或没说话。`recognize_voice` 会打印 `⚠️ 未识别到有效语音，已跳过`。

**AI 回答里出现「（搜索轮次超出限制）」**：tool-call 循环跑了 5 轮还没收敛，可以在 `ask_ai()` 里把 `range(5)` 调大，或者简化提问。

**回答里夹着 Markdown / emoji 被念出来**：模型偶尔会忽略约束。可以加强系统提示，或在播放前做一层正则清洗。

## 依赖

见 `requirements.txt`：

- `openai` — 调用 DeepSeek（DeepSeek 兼容 OpenAI SDK）
- `piper-tts` — 本地中文 TTS
- `PyAudio` — 麦克风录音 / 扬声器播放
- `tavily-python` — 联网搜索
- `vosk` — 本地中文 ASR