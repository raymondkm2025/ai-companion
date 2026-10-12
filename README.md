# ANIMA Memory Service

`memory_engine.py` 的 HTTP 封裝。把「寫入記憶 / 喚醒檢索 / 上下文編譯 / 離線鞏固」四件事開成 REST 端點，讓任何前端或對話模型接得上。

**零第三方依賴**（純 Python 標準庫），唔需要 `pip install`。

---

## 快速開始

```bash
# 方式一：雙擊或執行
start-server.cmd

# 方式二：直接跑
python companion/server.py --port 8731

# 方式三：環境變數
set ANIMA_PORT=8731
set ANIMA_DATA=C:\path\to\memory.jsonl
python companion/server.py
```

啟動輸出：

```
[anima] memory service on http://127.0.0.1:8731
[anima] data file: .../companion/data/memory.jsonl （已載入 0 條）
[anima] auth: disabled
```

**埠口選擇**：預設 `8731`。本機 `8730` 已被 rag-system 佔用，兩者不衝突，可同時常駐。

**自檢**（唔開 socket，不需要網絡）：

```bash
python companion/selftest.py
```

---

## Web UI

服務內建單頁介面，**同一個埠**，零額外依賴：

```
http://127.0.0.1:8731/          （或 /ui，兩者同一份）
```

四個分頁對應四件核心工作：

| 分頁 | 功能 |
|---|---|
| **記憶庫** | 按層過濾、檢視維持度／顯著性、修訂、標記「別再提」、手動觸發鞏固 |
| **寫入記憶** | 表單式寫入；即時顯示**入庫或被拒**（含顯著性分數與門檻對比） |
| **喚醒檢索** | 顯示已喚醒的記憶，同時列出前 10 名**喚醒分排序**，方便檢查閘門與排序是否合理 |
| **上下文編譯** | 輸入 persona／關係基線／狀態／對話，看到五區塊輸出與**逐區塊 token 預算條** |

UI 直接讀寫同一份記憶庫，改動即時落盤；沒有前端構建步驟，`ui.html` 一個檔案就是全部。


---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `ANIMA_HOST` | `127.0.0.1` | 綁定位址。對外開放才需改 |
| `ANIMA_PORT` | `8731` | 埠口 |
| `ANIMA_DATA` | `companion/data/memory.jsonl` | 記憶檔；每次寫操作原子落盤 |
| `ANIMA_TOKEN` | 空 | 若設定，所有 `/api/*` 需帶 `Authorization: Bearer <token>` |

---

## 端點

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康檢查與當前狀態 |
| GET | `/api/memories?level=` | 列出記憶（可按層過濾） |
| GET | `/api/memory/{id}` | 取單條（含 retention） |
| POST | `/api/memory/ingest` | **寫入候選記憶**（自動評分，過閾值才入庫） |
| POST | `/api/memory/search` | **喚醒檢索**（非向量 top-k） |
| POST | `/api/memory/{id}/revise` | 記憶修訂（版本化，保留舊值） |
| POST | `/api/memory/{id}/suppress` | 使用者要求「別再提」→ 永久排除召回 |
| DELETE | `/api/memory/{id}` | 同上（語義化用法） |
| POST | `/api/context/compile` | **編譯帶預算的上下文** |
| POST | `/api/consolidate` | 離線鞏固（衰減 + 生成主動提起鉤子） |

---

## 用法示例

### 寫入記憶

```bash
curl -X POST http://127.0.0.1:8731/api/memory/ingest -H "Content-Type: application/json" -d "{\"items\":[{\"level\":\"episodic\",\"content\":\"使用者說項目被臨時改需求，加班到十點錯過孩子睡前故事\",\"verbatim\":\"算了，反正也沒人在意\",\"emotion\":{\"valence\":-0.7,\"arousal\":0.85,\"label\":\"frustration\"},\"self_disclosure\":0.8,\"relational\":true,\"open_loop\":{\"is_open\":true,\"desc\":\"項目週五驗收\",\"due\":\"2026-10-05\"}}]}"
```

回應（被拒的條目也會回報分數，方便調參）：

```json
{
  "admitted": [{ "id": "mem_9f3c...", "salience": 0.7525, "flashbulb": true }],
  "rejected": [{ "content": "今天天氣不錯", "salience": 0.13, "reason": "below_threshold" }],
  "active_memories": 1
}
```

**入庫規則**：`salience >= 0.35` 才入庫；但以下兩種**無條件入庫**——
`user_pinned: true`（使用者明示「記住這個」），以及 `level` 為 `semantic` / `persona`（穩定事實本身就不該被當輪稀釋）。

### 喚醒檢索

```json
POST /api/memory/search
{
  "query": "今天又是加班到很晚",
  "affect": { "valence": -0.6, "arousal": 0.8, "label": "sadness" },
  "top_k": 2
}
```

回傳 `recalled`（最終入選，已過 MMR 多樣性與 30 分鐘冷卻窗）與 `candidates`（前 10 名原始喚醒分，方便 debug 排序是否合理）。

> **召回 ≠ 說出來。** 這個端點只負責「想起什麼」；要不要講、怎麼講，由呼叫方的對話模型決定。這是「記憶恰當率」能被獨立觀測的前提。

### 編譯上下文

```json
POST /api/context/compile
{
  "budget_tokens": 2000,
  "persona": "我說話簡短，不濫用安慰句式。",
  "relational": "認識 8 個月，稱呼已演化為「老莫」。進行中約定：一年咖啡。",
  "state": "現在：深夜 01:20；距上次對話：1 小時；狀態：安靜、投入。",
  "query": "今天也是加班到十點",
  "recall_k": 3,
  "turns": [
    { "speaker": "user", "text": "算了，反正也沒人在意", "emotion": { "valence": -0.7, "arousal": 0.9 } },
    { "speaker": "ai", "text": "……" },
    { "speaker": "user", "text": "今天也是加班到十點", "emotion": { "valence": -0.6, "arousal": 0.7 } }
  ]
}
```

回傳 `sections`（五個區塊）、`prompt`（可直接餵給模型）、`budget` / `usage` / `over_budget`（預算診斷）、`recalled_ids`。

預算比例（`persona` 12% / `relational` 18% / `state` 10% 為**不可淘汰**，合計 40%）：

```json
"budget_ratios": { "persona": 0.12, "relational": 0.18, "recalled": 0.25, "dialogue": 0.35, "state": 0.10 }
```

### 記憶修訂與抑制

```bash
# 修訂：不覆蓋，舊值進 previous_value（「使用者改變了想法」本身是關係資訊）
curl -X POST http://127.0.0.1:8731/api/memory/mem_9f3c.../revise -H "Content-Type: application/json" -d "{\"content\":\"使用者說項目臨時改需求，但真正在意的是沒人發現他加了班\"}"

# 抑制：使用者說「別再提這個」→ 永久排除召回，但不刪除
curl -X POST http://127.0.0.1:8731/api/memory/mem_9f3c.../suppress
```

---

## 接 LLM 的完整流程

```
使用者輸入
  │
  ├─▶ POST /api/context/compile   ← 帶 persona / relational / state / turns / query
  │        └─▶ 回傳 prompt（五區塊已編排好、預算已控住）
  │
  ├─▶ 你自己的 LLM 生成回應（本服務不含模型，方便自由替換）
  │
  ├─▶ 把這一輪（user + ai）寫回 turns
  │
  └─▶ POST /api/memory/ingest     ← 由抽取器產出候選記憶（可離線/異步）

會話結束後（或空閒時）
  └─▶ POST /api/consolidate       ← 衰減全庫 + 生成「主動提起鉤子」
```

**為什麼 LLM 不放在服務內**：一來服務保持零依賴、好部署；二來陪伴產品的對話模型會頻繁替換（換模型、換人格、A/B 測試），把它鎖進記憶服務是最貴的耦合。

---

## 設計決策

| 決策 | 理由 |
|---|---|
| 純標準庫 `http.server` | 本機沙盒環境下 `pip install` 不可靠；零依賴才能保證「一定跑得起」 |
| 全流程 `RLock` 序列化 | 記憶庫是有狀態共享資源，並發寫會讓 `recall_count` / 衰減競爭寫壞；陪伴場景 QPS 極低，鎖成本可忽略 |
| 寫操作同步全量落盤（原子替換） | 記憶不能丟。`tmp` + `replace` 保證不會寫出半截檔案。規模大時改增量 append |
| 壞行跳過而非整庫失敗 | 單行 JSON 損壞不該毀掉全部記憶 |
| `revise` 保留 `previous_value` | 「AI 記得舊版本並發現變化」是被記得感的最強證明 |
| `suppress` 不刪除 | 尊重使用者的「別再提」是最高優先；但保留用於避免未來誤提與安全審計 |
| 回傳 `candidates` 排序 | 喚醒檢索的排序品質是整個系統最難調的部分，必須可觀測 |

---

## 已知限制

- **記憶抽取未自動化**：`/api/memory/ingest` 收的是**已經抽取好**的候選條目。要接 LLM 抽取（`PROMPTS.md` 第 5 節的提示）才能端到端自動化。
- **Embedder 是哈希 bigram**：demo 級。生產要換真 embedding 模型（`HashingBigramEmbedder` → 替換 `Embedder` 協議實現）。
- **`ContextCompiler` 的摘要器是抽取式**：`ExtractiveSummarizer` 只是確定性回退，品質有限，生產要接 LLM。
- **無持久化索引**：每次啟動全量載入 JSONL。萬條以上需換 SQLite。
- **已在 2026-10-03 實測通過**：`python companion/selftest.py` 28 項斷言全 PASS；HTTP 端點與 Web UI 端到端驗證通過（本機 `pwsh` 沙盒需一次性 `danger-full-access` 升級才能執行，見專案筆記）。
- **重啟服務必須殺乾淨**：`job_kill` 只終止 pwsh 外殼，**Python 子進程會殘留**；而 Windows 的 `SO_REUSEADDR` 容許新舊進程同時 bind 同一埠，結果請求仍由**舊代碼**處理（症狀：改咗代碼但行為完全不變，例如新增的 `/ui` 回 `no route`）。正確做法：`netstat -ano | findstr :8731` 找 PID，再 `taskkill /F /PID <pid>`，確認埠已釋放後才啟動新的。
