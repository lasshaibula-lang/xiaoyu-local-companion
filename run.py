from __future__ import annotations

import argparse
import socket
import threading
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="栖语 · 本地陪伴")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"端口 {args.port} 已被占用。已有服务可直接访问 http://127.0.0.1:{args.port}；或使用 --port 8766。")
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{args.port}")
            return
    url = f"http://127.0.0.1:{args.port}"
    print(f"栖语启动中：{url}\n按 Ctrl+C 停止。当前只接受本机访问。", flush=True)
    if not args.no_browser:
        timer = threading.Timer(1.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run("companion.api:app", host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
