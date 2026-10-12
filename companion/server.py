"""
ANIMA Memory Service —— memory_engine 的 HTTP 封裝
=========================================================================
設計約束：
  · **純標準庫**（http.server），無第三方依賴，不需要 pip install。
  · 寫操作全部落盤（原子替換），重啟不丟記憶。
  · 全部端點以流程鎖序列化 —— 記憶庫是有狀態共享資源，不能並發寫。
  · 服務只負責「記憶與上下文」；LLM 生成不在本服務內（由呼叫方負責），
    這樣才能讓陪伴產品的對話模型自由替換。

啟動：
    python companion/server.py --port 8731
環境變數：
    ANIMA_PORT   預設 8731
    ANIMA_HOST   預設 127.0.0.1
    ANIMA_DATA   記憶檔路徑，預設 <本目錄>/data/memory.jsonl
    ANIMA_TOKEN  若設定，所有 /api/* 需帶 Authorization: Bearer <token>

端點一覽：
    GET    /api/health
    GET    /api/memories?level=episodic
    GET    /api/memory/{id}
    POST   /api/memory/ingest          寫入候選記憶（自動評分，過閾值才入庫）
    POST   /api/memory/search          喚醒檢索（非向量 top-k）
    POST   /api/memory/{id}/revise     記憶修訂（版本化，不覆蓋）
    POST   /api/memory/{id}/suppress   使用者要求「別再提」→ 永久排除召回
    POST   /api/context/compile        編譯帶預算的上下文
    POST   /api/consolidate            離線鞏固（衰減 + 生成主動提起鉤子）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台預設 cp1252，print 中文會 UnicodeEncodeError → 自行切 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from memory_engine import (  # noqa: E402
    BUDGET,
    THRESHOLD,
    AwakeningRetriever,
    Consolidator,
    ContextCompiler,
    Emotion,
    HashingBigramEmbedder,
    MemoryItem,
    MemoryStore,
    OpenLoop,
    SalienceScorer,
    Turn,
    estimate_tokens,
)

DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "memory.jsonl"
UI_PATH = Path(__file__).resolve().parent / "ui.html"


# =========================================================================
# 服務核心（與 HTTP 解耦，方便單元測試）
# =========================================================================

class MemoryService:
    def __init__(self, data_path: Path) -> None:
        self.lock = threading.RLock()
        self.embedder = HashingBigramEmbedder()
        self.store = MemoryStore(data_path)
        self.loaded = self.store.load()
        self.scorer = SalienceScorer(self.embedder)
        self.retriever = AwakeningRetriever(self.embedder, self.store)
        self.compiler = ContextCompiler()
        self.consolidator = Consolidator(self.scorer, self.store, self.retriever)

    # ---- 內部 ----
    @staticmethod
    def _emotion(d: dict | None) -> Emotion:
        d = d or {}
        return Emotion(
            valence=float(d.get("valence", 0.0)),
            arousal=float(d.get("arousal", 0.0)),
            label=str(d.get("label", "neutral")),
        )

    @staticmethod
    def _due(raw) -> float | None:
        """due 接受 epoch 秒（數字）或 ISO 日期字串；無法解析則視為無期限。"""
        if raw is None or raw == "":
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            from datetime import datetime, timezone
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None

    @staticmethod
    def _turns(raw: list) -> list[Turn]:
        out = []
        for t in raw or []:
            out.append(Turn(
                speaker=str(t.get("speaker", "user")),
                text=str(t.get("text", "")),
                ts=float(t.get("ts") or time.time()),
                emotion=MemoryService._emotion(t.get("emotion")),
            ))
        return out

    def _persist(self) -> None:
        self.store.save()

    # ---- health ----
    def health(self) -> dict:
        return {
            "status": "ok",
            "loaded_memories": self.loaded,
            "active_memories": len(self.store.items),
            "budget_ratios": BUDGET,
            "salience_threshold": THRESHOLD,
            "data_path": str(self.store.path) if self.store.path else None,
        }

    # ---- ingest ----
    def ingest(self, payload: dict) -> dict:
        raw_items = payload.get("items")
        if raw_items is None:
            raw_items = [payload] if payload.get("content") else []
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("需要 items 陣列，或單一條目（含 content）")

        admitted, rejected = [], []
        with self.lock:
            for r in raw_items:
                if not isinstance(r, dict) or not r.get("content"):
                    rejected.append({"reason": "missing_content"})
                    continue
                item = MemoryItem(
                    id=f"mem_{uuid.uuid4().hex[:16]}",
                    level=str(r.get("level", "episodic")),
                    content=str(r["content"]),
                    verbatim=str(r.get("verbatim", "")),
                    emotion=self._emotion(r.get("emotion")),
                    self_disclosure=float(r.get("self_disclosure", 0.0)),
                    relational=bool(r.get("relational", False)),
                    open_loop=OpenLoop(
                        is_open=bool((r.get("open_loop") or {}).get("is_open", False)),
                        desc=str((r.get("open_loop") or {}).get("desc", "")),
                        due=self._due((r.get("open_loop") or {}).get("due")),
                    ),
                    user_pinned=bool(r.get("user_pinned", False)),
                    subtype=str(r.get("subtype", "")),
                    status=str(r.get("status", "")),
                )
                score = self.scorer.score(item, self.store.all())
                if item.user_pinned or item.level in ("semantic", "persona") or score >= THRESHOLD:
                    item.salience = score
                    self.store.add(item)
                    admitted.append({"id": item.id, "salience": score,
                                     "flashbulb": item.is_flashbulb})
                else:
                    rejected.append({"content": item.content, "salience": score,
                                     "reason": "below_threshold"})
            self._persist()
        return {"admitted": admitted, "rejected": rejected,
                "active_memories": len(self.store.items)}

    # ---- search（喚醒檢索）----
    def search(self, payload: dict) -> dict:
        query = str(payload.get("query") or payload.get("q") or "")
        affect = self._emotion(payload.get("affect"))
        top_k = int(payload.get("top_k", 2))
        with self.lock:
            scored = self.retriever.score_all(query, affect, time.time())
            picked = self.retriever.wake(query, affect, top_k=top_k)
            self._persist()   # recall_count / last_recalled_at 已變更 → 落盤
        return {
            "query": query,
            "recalled": [
                {"id": m.id, "level": m.level, "content": m.content,
                 "verbatim": m.verbatim, "retention": round(m.retention(time.time()), 4),
                 "recall_count": m.recall_count}
                for m in picked
            ],
            "candidates": [
                {"id": m.id, "wake_score": w, "content": m.content}
                for m, w in scored[:10]
            ],
        }

    # ---- list / get ----
    def list_memories(self, level: str | None) -> dict:
        with self.lock:
            items = self.store.all(level if level else None)
        now = time.time()
        return {"count": len(items), "memories": [
            {"id": m.id, "level": m.level, "content": m.content,
             "salience": m.salience, "retention": round(m.retention(now), 4),
             "suppressed": m.suppressed, "user_pinned": m.user_pinned}
            for m in sorted(items, key=lambda x: -x.retention(now))
        ]}

    def get_memory(self, mem_id: str) -> dict | None:
        with self.lock:
            m = self.store.items.get(mem_id)
        if not m:
            return None
        # 用 to_json 做轉換：__dict__ 內含 Emotion/OpenLoop 物件，無法直接 json 序列化
        return json.loads(m.to_json()) | {"retention": round(m.retention(time.time()), 4)}

    # ---- revise / suppress ----
    def revise(self, mem_id: str, content: str) -> dict:
        with self.lock:
            if mem_id not in self.store.items:
                raise KeyError(mem_id)
            self.store.revise(mem_id, content)
            self._persist()
            m = self.store.items[mem_id]
        return {"id": m.id, "content": m.content, "previous_value": m.previous_value}

    def suppress(self, mem_id: str) -> dict:
        """使用者說『別再提這個』→ 永久排除召回，但不刪除（DESIGN 5.2-3）。"""
        with self.lock:
            if mem_id not in self.store.items:
                raise KeyError(mem_id)
            self.store.mark_suppressed(mem_id)
            self._persist()
        return {"id": mem_id, "suppressed": True}

    # ---- context compile ----
    def compile_context(self, payload: dict) -> dict:
        turns = self._turns(payload.get("turns"))
        query = str(payload.get("query") or (turns[-1].text if turns else ""))
        affect = self._emotion(payload.get("affect") or (
            turns[-1].emotion.__dict__ if turns else None))
        top_k = int(payload.get("recall_k", 3))
        budget = int(payload.get("budget_tokens", 2000))
        with self.lock:
            recalled = self.retriever.wake(query, affect, top_k=top_k)
            self._persist()
            result = self.compiler.compile(
                total_budget_tokens=budget,
                persona_core=str(payload.get("persona", "")),
                relational_baseline=str(payload.get("relational", "")),
                dynamic_state=str(payload.get("state", "")),
                turns=turns,
                recalled=recalled,
            )
        result["recalled_ids"] = [m.id for m in recalled]
        result["total_tokens"] = sum(result["usage"].values())
        return result

    # ---- consolidate ----
    def consolidate(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        turns = self._turns(payload.get("turns"))
        with self.lock:
            report = self.consolidator.run(turns)
            self._persist()
        return report


# =========================================================================
# HTTP 層
# =========================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "ANIMA/0.1"
    service: MemoryService          # 由 make_server 注入
    token: str | None = None

    # ---- 基礎工具 ----
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code: int, message: str) -> None:
        self._send(code, {"error": message})

    def _auth_ok(self) -> bool:
        if not self.token:
            return True
        got = self.headers.get("Authorization", "")
        if got == f"Bearer {self.token}":
            return True
        self._fail(401, "unauthorized")
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid json: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("body 必須是 JSON 物件")
        return data

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        sys.stderr.write("[anima] %s - %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

    # ---- 路由 ----
    def _dispatch(self, method: str) -> None:
        if not self._auth_ok():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        qs = parse_qs(urlparse(self.path).query)
        parts = [p for p in path.split("/") if p]
        svc = self.service

        try:
            # GET /api/health
            if method == "GET" and path == "/api/health":
                return self._send(200, svc.health())

            # GET /api/memories
            if method == "GET" and path == "/api/memories":
                level = (qs.get("level") or [None])[0]
                return self._send(200, svc.list_memories(level))

            # /api/memory/{id}...
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "memory":
                mem_id = parts[2]

                if method == "GET" and len(parts) == 3:
                    m = svc.get_memory(mem_id)
                    return self._send(200, m) if m else self._fail(404, "not found")

                if method == "DELETE" and len(parts) == 3:
                    return self._send(200, svc.suppress(mem_id))

                if method == "POST" and len(parts) == 4 and parts[3] == "revise":
                    body = self._body()
                    return self._send(200, svc.revise(mem_id, str(body.get("content", ""))))

                if method == "POST" and len(parts) == 4 and parts[3] == "suppress":
                    return self._send(200, svc.suppress(mem_id))

            if method == "POST":
                body = self._body()
                # POST /api/memory/ingest
                if path == "/api/memory/ingest":
                    return self._send(200, svc.ingest(body))
                # POST /api/memory/search
                if path == "/api/memory/search":
                    return self._send(200, svc.search(body))
                # POST /api/context/compile
                if path == "/api/context/compile":
                    return self._send(200, svc.compile_context(body))
                # POST /api/consolidate
                if path == "/api/consolidate":
                    return self._send(200, svc.consolidate(body))

            # ---- Web UI ----
            if method == "GET" and path in ("/", "/ui"):
                if UI_PATH.exists():
                    return self._send_html(UI_PATH.read_text(encoding="utf-8"))
                return self._fail(404, f"ui.html not found at {UI_PATH}")

            # ---- 端點清單（API 入口）----
            if method == "GET" and path == "/api":
                return self._send(200, {
                    "service": "ANIMA Memory Service",
                    "ui": "/ui",
                    "endpoints": [
                        "GET  /api/health", "GET  /api/memories?level=",
                        "GET  /api/memory/{id}",
                        "POST /api/memory/ingest", "POST /api/memory/search",
                        "POST /api/memory/{id}/revise", "POST /api/memory/{id}/suppress",
                        "POST /api/context/compile", "POST /api/consolidate",
                    ],
                })

            self._fail(404, f"no route: {method} {path}")

        except ValueError as exc:
            self._fail(400, str(exc))
        except KeyError as exc:
            self._fail(404, f"memory not found: {exc}")
        except Exception as exc:                          # noqa: BLE001
            self._fail(500, f"{type(exc).__name__}: {exc}")

    def do_GET(self) -> None:        # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:       # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:     # noqa: N802
        self._dispatch("DELETE")


def make_server(host: str, port: int, data_path: Path, token: str | None) -> ThreadingHTTPServer:
    service = MemoryService(data_path)
    handler = type("BoundHandler", (Handler,), {"service": service, "token": token})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main() -> int:
    ap = argparse.ArgumentParser(description="ANIMA Memory Service")
    ap.add_argument("--host", default=os.environ.get("ANIMA_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ANIMA_PORT", "8731")))
    ap.add_argument("--data", default=os.environ.get("ANIMA_DATA", str(DEFAULT_DATA)))
    ap.add_argument("--token", default=os.environ.get("ANIMA_TOKEN"))
    args = ap.parse_args()

    srv = make_server(args.host, args.port, Path(args.data), args.token)
    svc = srv.RequestHandlerClass.service
    print(f"[anima] memory service on http://{args.host}:{args.port}", flush=True)
    print(f"[anima] data file: {args.data} （已載入 {svc.loaded} 條）", flush=True)
    print(f"[anima] auth: {'Bearer token' if args.token else 'disabled'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[anima] shutting down", flush=True)
    finally:
        with svc.lock:
            svc.store.save()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
