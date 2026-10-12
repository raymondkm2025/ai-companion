"""
ANIMA Memory Service —— 自檢腳本
=========================================================================
不開 socket，直接驅動 MemoryService，覆蓋全部端點的行為。
沒有網絡、沒有第三方依賴，任何裝了 Python 3.9+ 的機器都能跑：

    python companion/selftest.py

退出碼 0 = 全部通過；1 = 有失敗項。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台預設 cp1252，直接 print 中文會 UnicodeEncodeError。
# 在腳本內自行切 UTF-8，不依賴外部環境變數。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from server import MemoryService  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anima-selftest-"))
    data = tmp / "memory.jsonl"
    svc = MemoryService(data)

    print("== 1. health ==")
    h = svc.health()
    check("health 回報 ok", h["status"] == "ok", str(h))
    check("初始記憶庫為空", h["active_memories"] == 0, str(h["active_memories"]))

    print("== 2. ingest：顯著性門檻 ==")
    res = svc.ingest({"items": [
        {
            "level": "episodic",
            "content": "使用者說項目被臨時改需求，加班到十點錯過孩子睡前故事",
            "verbatim": "算了，反正也沒人在意",
            "emotion": {"valence": -0.7, "arousal": 0.85, "label": "frustration"},
            "self_disclosure": 0.8,
            "relational": True,
            "open_loop": {"is_open": True, "desc": "項目週五驗收"},
        },
        {
            "level": "episodic",
            "content": "使用者說今天天氣不錯",
            "emotion": {"valence": 0.1, "arousal": 0.1},
        },
        {
            "level": "semantic",
            "content": "使用者對花生過敏",
            "user_pinned": True,
        },
    ]})
    admitted = {a["id"] for a in res["admitted"]}
    check("高顯著性情節入庫", len(admitted) >= 1, str(res["admitted"]))
    check("低顯著性閒聊被拒", any(r.get("reason") == "below_threshold" for r in res["rejected"]),
          str(res["rejected"]))
    check("明示項無論分數一律入庫", len(admitted) == 2, f"admitted={len(admitted)}")

    ep_id = res["admitted"][0]["id"]
    sem_id = res["admitted"][1]["id"]

    print("== 3. search：喚醒檢索 ==")
    s = svc.search({
        "query": "今天又是加班到很晚",
        "affect": {"valence": -0.6, "arousal": 0.8, "label": "sadness"},
        "top_k": 2,
    })
    check("有召回結果", len(s["recalled"]) >= 1, str(s))
    check("召回生效（recall_count 增加）", all(r["recall_count"] >= 1 for r in s["recalled"]),
          str(s["recalled"]))
    check("有候選排序清單", len(s["candidates"]) >= 1, str(s["candidates"]))
    check("維持度不超過 1.0（地板非加成）",
          all(r["retention"] <= 1.0 for r in s["recalled"]),
          str([r["retention"] for r in s["recalled"]]))

    # 語義閘門：與當下情境無關的記憶，其他項再高都不該被想起
    un = svc.search({"query": "週末想去海邊走走",
                     "affect": {"valence": 0.3, "arousal": 0.3}, "top_k": 5})
    check("無關查詢不召回加班記憶",
          all("加班" not in x["content"] for x in un["recalled"]),
          str([x["content"] for x in un["recalled"]]))

    print("== 4. context/compile：預算制編譯 ==")
    now = time.time()
    out = svc.compile_context({
        "budget_tokens": 1200,
        "persona": "我說話簡短，不濫用安慰句式。",
        "relational": "認識 8 個月，稱呼已演化為「老莫」。",
        "state": "現在：深夜；我的狀態：安靜。",
        "turns": [
            {"speaker": "user", "text": "算了，反正也沒人在意",
             "ts": now - 3600, "emotion": {"valence": -0.7, "arousal": 0.9}},
            {"speaker": "ai", "text": "……", "ts": now - 3500},
            {"speaker": "user", "text": "今天也是加班到十點",
             "ts": now - 60, "emotion": {"valence": -0.6, "arousal": 0.7}},
        ],
    })
    check("回傳五個區塊", set(out["sections"]) ==
          {"persona", "relational", "recalled", "dialogue", "state"}, str(out["sections"].keys()))
    check("逐字錨點被保留", "反正也沒人在意" in out["sections"]["dialogue"],
          out["sections"]["dialogue"][:200])
    check("不可淘汰區塊未被裁掉", "老莫" in out["sections"]["relational"])
    check("總 token 在預算量級內", out["total_tokens"] <= 1200 * 1.2,
          f"total={out['total_tokens']}")
    check("回報召回 id", isinstance(out["recalled_ids"], list))

    print("== 5. revise：版本化不覆蓋 ==")
    r = svc.revise(ep_id, "使用者說項目臨時改需求，但真正在意的是沒人發現他加了班")
    check("內容已更新", "真正在意" in r["content"], str(r))
    check("舊版本被保留", r["previous_value"] != "", str(r))

    print("== 6. suppress：使用者要求別再提 ==")
    svc.suppress(sem_id)
    s2 = svc.search({"query": "花生 過敏", "affect": {"arousal": 0.5}, "top_k": 5})
    check("已抑制的記憶不再被召回",
          all(x["id"] != sem_id for x in s2["recalled"]),
          str([x["id"] for x in s2["recalled"]]))
    check("抑制不等於刪除（仍在庫中）",
          svc.get_memory(sem_id) is not None)

    print("== 7. persist：落盤與重載 ==")
    svc.store.save()
    check("資料檔已寫入", data.exists() and data.stat().st_size > 0, str(data))
    svc2 = MemoryService(data)
    check("重啟後記憶條數一致", svc2.loaded == len(svc.store.items),
          f"{svc2.loaded} vs {len(svc.store.items)}")
    check("抑制狀態跨重啟保留",
          svc2.get_memory(sem_id)["suppressed"] is True)
    check("修訂歷史跨重啟保留",
          svc2.get_memory(ep_id)["previous_value"] != "")

    print("== 8. consolidate：鞏固與主動提起鉤子 ==")
    rep = svc.consolidate({"turns": [{"speaker": "user", "text": "週五要驗收"}]})
    check("回傳鉤子欄位", "hooks" in rep, str(rep))
    check("未完成事項（episodic 層）也成為主動提起鉤子",
          len(rep["hooks"]) >= 1, str(rep))
    check("回傳 active 計數", isinstance(rep.get("active"), int), str(rep))

    print("== 9. 錯誤處理 ==")
    try:
        svc.ingest({"items": []})
        check("空 items 應報錯", False)
    except ValueError:
        check("空 items 正確拋 ValueError", True)
    try:
        svc.revise("mem_not_exist", "x")
        check("不存在 id 應報錯", False)
    except KeyError:
        check("不存在 id 正確拋 KeyError", True)

    print()
    if FAILURES:
        print(f"結果：{len(FAILURES)} 項失敗 → {FAILURES}")
        return 1
    print("結果：全部通過 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
