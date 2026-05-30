import json
import os
import queue
import sys
import threading
import time

import pyaudio
from openai import OpenAI
from piper import PiperVoice
from tavily import TavilyClient
from vosk import Model, KaldiRecognizer

# -------- 录音配置 --------
CHUNK = 1096
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000

#
# # -------- 全局状态 --------
recording_event = threading.Event()

record_queue = queue.Queue(3)
question_queue = queue.Queue(3)
answer_queue = queue.Queue(3)

client = OpenAI(
    api_key=os.environ.get('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com")

tavily = TavilyClient(api_key=os.environ.get('TAVILY_API_KEY'))

SEARCH_TOOL = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索实时/最新信息（天气、新闻、股价、赛事、地点、最近事件等任何需要查证的内容）。模型自身不掌握或不确定时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，中英文皆可"},
            },
            "required": ["query"],
        },
    },
}]


def run_web_search(query: str) -> str:
    print(f"🔎 联网搜索: {query}")
    try:
        res = tavily.search(query=query, max_results=5, search_depth="basic")
        items = [
            f"- {r.get('title')}: {r.get('content')}" for r in res.get("results", [])
        ]
        return "\n".join(items) if items else "（无结果）"
    except Exception as e:
        return f"搜索失败: {e}"


model = Model("vosk-model-small-cn-0.22")
recognizer = KaldiRecognizer(model, RATE)
voice = PiperVoice.load("zh_CN-huayan-medium.onnx")
p = pyaudio.PyAudio()


# # -------- 录音线程 --------
def record_thread():
    print("开始录音")
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)
    frames = []
    print("🎙️ 开始录音，按 E 停止")
    while recording_event.is_set():
        data = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)

    stream.stop_stream()
    stream.close()
    print("录音长度：" + str(len(frames)))
    record_queue.put(frames)
    print("✅ 录音已完成")


def recognize_voice():
    print("\n识别线程启动")
    while True:
        frames = record_queue.get()
        data = b"".join(frames)
        print("录音字节数：" + str(len(data)))
        # 把整段录音喂给识别器，再用 FinalResult 强制刷新（清空内部状态）
        recognizer.AcceptWaveform(data)
        result = json.loads(recognizer.FinalResult())
        text = result.get("text", "").strip().replace(" ", "")
        if text:
            print(f"You said: {text}")
            question_queue.put(text)
        else:
            print("⚠️ 未识别到有效语音，已跳过")
        record_queue.task_done()


def ask_ai():
    print("\nAI 线程启动")
    from datetime import datetime

    while True:
        question = question_queue.get()
        t = datetime.now()
        prompt = (
                "你是一个语音助手的处理问题的一部分，答案尽量清晰而口语化，回答也要有逻辑，但需要避免重复和长篇大论, "
                "为考虑播放效果，不允许产生任何Markdown格式符号，不允许产生emoji等不可读的元素，"
                "现在的时间是: "
                + t.strftime("%Y-%m-%d %H:%M:%S")
                + " 现在的位置是：广东佛山。"
                  "对于天气、新闻、价格、赛事、地点等实时或可能过时的信息，请调用 web_search 工具联网查询，再据此回答。"
        )
        print(f"prompt: {prompt}")

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ]

        # tool-call loop：模型可能多轮调用搜索后才给最终答案
        for _ in range(5):
            response = client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=messages,
                tools=SEARCH_TOOL,
                stream=False,
                reasoning_effort="high",
                extra_body={"thinking": {"type": "enabled"}},
            )
            msg = response.choices[0].message
            if not msg.tool_calls:
                answer = msg.content
                break

            # 把 assistant 的 tool_call 消息也加回去
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = run_web_search(args.get("query", ""))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        else:
            answer = "（搜索轮次超出限制）"

        print(f"ai回答: {answer}")
        answer_queue.put(answer)
        question_queue.task_done()


def speak_answer():
    print("\n播放线程启动")

    while True:
        answer = answer_queue.get()
        stream = None  # 播放流，一开始不打开！
        try:
            # 开始流式合成
            for chunk in voice.synthesize(answer):
                audio_data = chunk.audio_int16_bytes

                if not stream and len(audio_data) > 0:
                    print("▶️ 开始播放")
                    stream = p.open(
                        format=FORMAT,
                        channels=CHANNELS,
                        rate=voice.config.sample_rate,
                        output=True  # 输出=播放
                    )

                # 有流就写数据播放
                if stream and len(audio_data) > 0:
                    stream.write(audio_data)

            print("✅ TTS 合成&播放完成")

        except Exception as e:
            print(f"❌ TTS 播放失败: {e}")

        finally:
            if stream:
                stream.stop_stream()
                stream.close()
                print("🔌 播放设备已安全关闭")
            answer_queue.task_done()


# def _drain(q):
#     """清空 queue 里的所有积压项"""
#     dropped = 0
#     while True:
#         try:
#             q.get_nowait()
#             q.task_done()
#             dropped += 1
#         except queue.Empty:
#             return dropped


_record_thread_ref = None  # 跟踪当前录音线程，避免重复启动


def keyboard_control_thread():
    global _record_thread_ref
    while True:
        # SSH 只能用这种方式读键盘
        cmd = input("\n输入命令 (s=开始, e=停止, q=退出): ").strip().lower()

        if cmd == 's':
            # 防止旧录音线程未完全退出时重复启动
            if _record_thread_ref is not None and _record_thread_ref.is_alive():
                print("⚠️ 上一次录音还在收尾，请稍等")
                continue
            if not recording_event.is_set():
                # 新一次开口 → 丢弃排队的旧问题/旧答案（避免 AI 阻塞时积压乱序）
                # d1 = _drain(question_queue)
                # d2 = _drain(answer_queue)
                # if d1 or d2:
                #     print(f"🧹 已丢弃 {d1} 个待处理问题, {d2} 个待播放答复")
                print("▶️ 开始录音...")
                recording_event.set()
                _record_thread_ref = threading.Thread(target=record_thread, daemon=True)
                _record_thread_ref.start()

        elif cmd == 'e':
            if recording_event.is_set():
                recording_event.clear()
                print(" 停止录音...")

        elif cmd == 'q':
            print("退出程序...")
            recording_event.clear()
            time.sleep(0.5)
            sys.exit(0)


if __name__ == "__main__":
    print("=====================================")
    print("  SSH 环境 录音工具")
    print("  s + 回车 = 开始录音")
    print("  e + 回车 = 停止录音")
    print("  q + 回车 = 退出程序")
    print("=====================================")

    recording_event.clear()
    threading.Thread(target=recognize_voice).start()
    threading.Thread(target=ask_ai).start()
    threading.Thread(target=speak_answer).start()
    keyboard_control_thread()  # 保持程序运行
