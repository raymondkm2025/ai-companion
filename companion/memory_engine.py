"""
ANIMA 記憶引擎 —— 參考實現
=========================================================================
對應 DESIGN.md：
  · 第 4 節  記憶織體（L1/L2 資料結構、L4 關係記憶）
  · 第 5 節  顯著性評分、衰減（含閃光燈項）、喚醒檢索（非 top-k）、冷卻與多樣性
  · 第 6 節  Context Compiler 預算分配與三級壓縮 + 逐字錨點

設計約束：
  1) 只用標準庫，可直接執行 `python memory_engine.py` 看 demo。
  2) Embedder / Summarizer 均為可插拔協議 —— 生產環境接真模型，demo 走確定性回退。
  3) 在線路徑（write/retrieve/compile）保持 O(n) 且無外部呼叫；
     重活（抽取、去重、歸納、衰減）全部歸離線 Consolidator。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable, Protocol, Sequence

# =========================================================================
# 0. 基礎工具：分詞、向量、token 估算
# =========================================================================

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    """中文友好的粗分詞：漢字取 bigram，其餘按空白/標點切。無外部依賴。"""
    chars = [c for c in text if _CJK.match(c)]
    grams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)] or chars
    latin = re.findall(r"[A-Za-z0-9_]+", text.lower())
    return grams + latin


def estimate_tokens(text: str) -> int:
    """保守估算：漢字 1 字 ≈ 1 token，其餘 ≈ 4 字元/token。"""
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


class Embedder(Protocol):
    def encode(self, text: str) -> list[float]: ...


class HashingBigramEmbedder:
    """確定性哈希向量（demo 用）。生產環境替換為真實 embedding 模型。"""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            # 用 hashlib 而非內建 hash()：後者受 PYTHONHASHSEED 影響，跨進程不穩定，
            # 會讓持久化記憶的相似度在重啟後漂移。
            h = int.from_bytes(hashlib.sha1(tok.encode("utf-8")).digest()[:4], "big")
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class Summarizer(Protocol):
    def summarize(self, text: str, max_chars: int) -> str: ...


class ExtractiveSummarizer:
    """確定性回退摘要：取最長句 + 首句。生產環境應接 LLM。"""

    def summarize(self, text: str, max_chars: int) -> str:
        sents = [s for s in re.split(r"[。！？\n]", text) if s.strip()]
        if not sents:
            return text[:max_chars]
        head = sents[0]
        body = max(sents, key=len)
        out = head if head == body else f"{head}。…{body}"
        return out[:max_chars]


# =========================================================================
# 1. 資料結構（設計原則：逐字錨點與情感欄位是一等公民，不是 metadata）
# =========================================================================

LEVELS = ("working", "episodic", "semantic", "relational", "persona")

# 各層記憶半衰期（天）。None = 永久（不吃時間衰減）
HALF_LIFE_DAYS: dict[str, float | None] = {
    "working": 0.05,
    "episodic": 30.0,
    "semantic": None,
    "relational": 365.0,
    "persona": None,
}

FLASHBULB_AROUSAL = 0.8     # 情感峰值門檻 → 觸發不衰減下界
FLASHBULB_FLOOR = 0.55      # 下界值（相當於「永遠記得，只是細節會模糊」）


@dataclass
class Emotion:
    valence: float = 0.0        # -1 負面 ~ +1 正面
    arousal: float = 0.0        # 0 平靜 ~ 1 激動
    label: str = "neutral"

    @property
    def intensity(self) -> float:
        return min(1.0, abs(self.valence) * 0.5 + self.arousal * 0.5)


@dataclass
class OpenLoop:
    """未完成事項。DESIGN 5.1 的 w5，也是主動性（手冊第 6 條）的燃料。"""
    is_open: bool = False
    desc: str = ""
    due: float | None = None    # epoch seconds

    def urgency(self, now: float) -> float:
        if not self.is_open:
            return 0.0
        if self.due is None:
            return 0.4
        days = (self.due - now) / 86400.0
        if days <= 0:
            return 1.0
        return max(0.2, 1.0 - days / 14.0)


@dataclass
class MemoryItem:
    id: str
    level: str                      # L1..L5
    content: str                    # 摘要式描述（可壓縮的部分）
    verbatim: str = ""              # ★ 逐字錨點（永不壓縮）
    emotion: Emotion = field(default_factory=Emotion)
    self_disclosure: float = 0.0    # 使用者主動揭露脆弱程度 0~1
    relational: bool = False        # 是否涉及「我們」
    open_loop: OpenLoop = field(default_factory=OpenLoop)
    salience: float = 0.0
    user_pinned: bool = False       # 使用者明示「記住這個」→ 覆寫，不吃衰減
    suppressed: bool = False        # 使用者要求別提 → 永久排除召回
    created_at: float = field(default_factory=time.time)
    last_recalled_at: float = field(default_factory=time.time)
    recall_count: int = 0
    # L4 專用
    subtype: str = ""               # nickname_evolution | promise | inside_joke | repair_event
    status: str = ""                # pending | fulfilled | broken
    previous_value: str = ""        # 記憶修訂：保留舊版本（DESIGN 9.1）

    @property
    def is_flashbulb(self) -> bool:
        return self.emotion.arousal >= FLASHBULB_AROUSAL or self.self_disclosure >= 0.85

    def retention(self, now: float) -> float:
        """R(t) = max(S · e^(−λt), P)   —— DESIGN 附錄 B

        P 是「地板」而不是「加成」：情感峰值記憶不會衰減到地板以下，
        但也不會因此被推高過 1（實測過加成寫法會產生 1.30 這種非法維持度）。
        """
        if self.user_pinned:
            return 1.0
        hl = HALF_LIFE_DAYS.get(self.level)
        if hl is None:
            return self.salience
        t_days = max(0.0, (now - self.last_recalled_at) / 86400.0)
        lam = math.log(2) / hl
        decayed = self.salience * math.exp(-lam * t_days)
        p = FLASHBULB_FLOOR if self.is_flashbulb else 0.0
        return min(1.0, max(decayed, p))

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_dict(cls, raw: dict) -> "MemoryItem":
        d = dict(raw)
        d["emotion"] = Emotion(**d.get("emotion", {}))
        d["open_loop"] = OpenLoop(**d.get("open_loop", {}))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Turn:
    """L1 工作記憶的一輪。"""
    speaker: str                    # "user" | "ai"
    text: str
    ts: float = field(default_factory=time.time)
    emotion: Emotion = field(default_factory=Emotion)
    scene_id: int = 0


# =========================================================================
# 2. 顯著性評分器 —— DESIGN 5.1
# =========================================================================

@dataclass
class SalienceWeights:
    emotion: float = 0.30
    novelty: float = 0.10
    self_disclosure: float = 0.20
    relational: float = 0.20
    open_loop: float = 0.15

THRESHOLD = 0.35


class SalienceScorer:
    def __init__(self, embedder: Embedder, weights: SalienceWeights | None = None) -> None:
        self.embedder = embedder
        self.w = weights or SalienceWeights()

    def novelty(self, text: str, existing: Iterable[MemoryItem]) -> float:
        """與既有記憶的最大相似度取反。已知事實不加分。"""
        vec = self.embedder.encode(text)
        best = 0.0
        for m in existing:
            best = max(best, cosine(vec, self.embedder.encode(m.content)))
        return 1.0 - best

    def score(self, item: MemoryItem, existing: Iterable[MemoryItem] = ()) -> float:
        if item.user_pinned:
            return 1.0                      # ★ 覆寫項，非加權項
        w = self.w
        s = (
            w.emotion * item.emotion.intensity
            + w.novelty * self.novelty(item.content, existing)
            + w.self_disclosure * item.self_disclosure
            + w.relational * (1.0 if item.relational else 0.0)
            + w.open_loop * item.open_loop.urgency(time.time())
        )
        return round(min(1.0, max(0.0, s)), 4)

    def admits(self, item: MemoryItem, existing: Iterable[MemoryItem] = ()) -> bool:
        return self.score(item, existing) >= THRESHOLD


# =========================================================================
# 3. 記憶儲存
# =========================================================================

class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.items: dict[str, MemoryItem] = {}
        self.path = path

    def add(self, item: MemoryItem) -> None:
        self.items[item.id] = item

    def all(self, level: str | None = None) -> list[MemoryItem]:
        vals = list(self.items.values())
        return [m for m in vals if m.level == level] if level else vals

    def mark_suppressed(self, mem_id: str) -> None:
        """使用者說「別再提這個」→ 永久排除召回，但不刪除（DESIGN 5.2-3）。"""
        if mem_id in self.items:
            self.items[mem_id].suppressed = True

    def revise(self, mem_id: str, new_content: str) -> None:
        """記憶修訂：版本化而非覆蓋（DESIGN 9.1）。"""
        m = self.items[mem_id]
        m.previous_value = m.content
        m.content = new_content
        m.last_recalled_at = time.time()

    def prune(self, now: float, floor: float = 0.08) -> list[str]:
        """低於歸檔閾值者移出召回池（不刪除）。回傳被歸檔的 id。
        semantic / persona 層無限半衰期，不參與歸檔。"""
        archived = [
            m.id for m in self.items.values()
            if m.level not in ("semantic", "persona")
            and not m.user_pinned
            and m.retention(now) < floor
        ]
        return archived

    def save(self) -> None:
        """全量落盤（原子替換）。demo 規模足夠；上生產應改增量 append。"""
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "\n".join(m.to_json() for m in self.items.values()), encoding="utf-8"
        )
        tmp.replace(self.path)

    def load(self) -> int:
        """從磁盤讀回記憶。回傳載入條數。壞行跳過，不讓單行損壞毀掉整個庫。"""
        if not self.path or not self.path.exists():
            return 0
        n = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = MemoryItem.from_dict(json.loads(line))
            except Exception:
                continue
            self.items[item.id] = item
            n += 1
        return n


# =========================================================================
# 4. 喚醒檢索 —— DESIGN 5.3（不是向量 top-k）
# =========================================================================

@dataclass
class WakeWeights:
    semantic: float = 0.35
    recency: float = 0.15
    affect_resonance: float = 0.25
    open_loop: float = 0.20
    serendipity: float = 0.05


class AwakeningRetriever:
    COOLDOWN_SEC = 1800      # 30 分鐘
    COOLDOWN_PENALTY = 0.2

    def __init__(self, embedder: Embedder, store: MemoryStore, weights: WakeWeights | None = None,
                 rng: random.Random | None = None, min_lexical_overlap: float = 0.12) -> None:
        self.embedder = embedder
        self.store = store
        self.w = weights or WakeWeights()
        self.rng = rng or random.Random(20261003)
        # ★ 相關性當「閘門」而非加權項：否則情感共振／時近／驚喜會把
        #   完全無關的記憶推上來（實測：「加班」的查詢召回了「花生過敏」）。
        #   閘門用精確詞元重疊而不是向量相似度——哈希向量有碰撞，512 維下
        #   兩段毫無共同詞的中文短句也會因碰撞得到非零相似度而穿過閘門。
        #   生產環境接真 embedding 後，此處可改回向量相似度。
        self.min_lexical_overlap = min_lexical_overlap
        self._recently_mentioned: dict[str, float] = {}
        self._vec_cache: dict[str, tuple[str, list[float]]] = {}

    def _vec(self, m: MemoryItem) -> list[float]:
        """向量緩存：內容沒變就不重算，避免每次查詢重編碼整個記憶庫。"""
        key = f"{m.content} {m.verbatim}"
        hit = self._vec_cache.get(m.id)
        if hit and hit[0] == key:
            return hit[1]
        vec = self.embedder.encode(key)
        self._vec_cache[m.id] = (key, vec)
        return vec

    @staticmethod
    def _lexical_overlap(query: str, m: MemoryItem) -> float:
        """精確詞元重疊係數（除以較小集合，對短查詢友善）。
        確定性、無碰撞，適合作閘門；但無法處理同義詞——真實部署接 embedding 後應替換。"""
        q = set(_tokens(query))
        t = set(_tokens(f"{m.content} {m.verbatim}"))
        if not q or not t:
            return 0.0
        return len(q & t) / min(len(q), len(t))

    def _recency_score(self, m: MemoryItem, now: float) -> float:
        """時近性用 retention 的形狀，但避免語義層因半衰期 None 而失真。"""
        hl = HALF_LIFE_DAYS.get(m.level) or 365.0
        t_days = max(0.0, (now - m.created_at) / 86400.0)
        return math.exp(-math.log(2) / hl * t_days)

    def _affect_resonance(self, m: MemoryItem, affect: Emotion) -> float:
        """情感共振：當前情緒與記憶情緒同頻（效價接近 + 當前喚起度高 ⇒ 更容易想起）。"""
        valenced = 1.0 - abs(m.emotion.valence - affect.valence) / 2.0
        return valenced * (0.5 + 0.5 * affect.arousal)

    def score_all(self, query: str, affect: Emotion, now: float,
                  apply_gate: bool = True) -> list[tuple[MemoryItem, float]]:
        qv = self.embedder.encode(query)
        scored: list[tuple[MemoryItem, float]] = []
        for m in self.store.items.values():
            if m.suppressed or m.level == "working":
                continue
            sim = cosine(qv, self._vec(m))
            # 閘門：與當下情境毫無關聯的記憶，其他項再高都不該被想起
            if apply_gate and self._lexical_overlap(query, m) < self.min_lexical_overlap:
                continue
            w = (
                self.w.semantic * sim
                + self.w.recency * self._recency_score(m, now)
                + self.w.affect_resonance * self._affect_resonance(m, affect)
                + self.w.open_loop * m.open_loop.urgency(now)
                + self.w.serendipity * self.rng.random()
            ) * m.retention(now)          # 衰減參與排序，而非僅事後過濾
            last = self._recently_mentioned.get(m.id)
            if last is not None and now - last < self.COOLDOWN_SEC:
                w *= self.COOLDOWN_PENALTY  # 冷卻窗：避免復讀機感
            scored.append((m, round(w, 4)))
        return sorted(scored, key=lambda x: -x[1])

    def wake(self, query: str, affect: Emotion, top_k: int = 2,
             now: float | None = None, mmr_lambda: float = 0.7) -> list[MemoryItem]:
        """MMR 多樣性約束：避免五條記憶講同一件事。"""
        now = now or time.time()
        cands = self.score_all(query, affect, now)
        selected: list[MemoryItem] = []
        vecs = {m.id: self.embedder.encode(m.content) for m, _ in cands}
        pool = list(cands)
        while pool and len(selected) < top_k:
            def mmr(pair: tuple[MemoryItem, float]) -> float:
                m, rel = pair
                if not selected:
                    return rel
                redundancy = max(cosine(vecs[m.id], vecs[s.id]) for s in selected)
                return mmr_lambda * rel - (1 - mmr_lambda) * redundancy
            best = max(pool, key=mmr)
            pool.remove(best)
            selected.append(best[0])
        for m in selected:
            m.recall_count += 1
            m.last_recalled_at = now
            self._recently_mentioned[m.id] = now
        return selected


# =========================================================================
# 5. Context Compiler —— DESIGN 第 6 節
# =========================================================================

BUDGET = {
    "persona": 0.12,       # 不可淘汰
    "relational": 0.18,    # 不可淘汰
    "recalled": 0.25,
    "dialogue": 0.35,
    "state": 0.10,         # 不可淘汰
}

RECENT_VERBATIM_TURNS = 10


class ContextCompiler:
    def __init__(self, summarizer: Summarizer | None = None) -> None:
        self.summarizer = summarizer or ExtractiveSummarizer()

    # ---- 場景邊界檢測（DESIGN 6.4）----
    def segment_scenes(self, turns: Sequence[Turn]) -> list[int]:
        """回傳每輪所屬的 scene_id。按語義/情緒斷層切分，而非固定輪數。"""
        scene_ids: list[int] = []
        sid = 0
        for i, t in enumerate(turns):
            if i > 0:
                prev = turns[i - 1]
                valence_flip = abs(t.emotion.valence - prev.emotion.valence) > 0.8
                time_gap = (t.ts - prev.ts) > 6 * 3600
                closers = ("先這樣", "不說了", "早點睡", "先忙", "回頭聊")
                closing = any(c in prev.text for c in closers)
                if valence_flip or time_gap or closing:
                    sid += 1
            scene_ids.append(sid)
        return scene_ids

    # ---- 逐字錨點：無論壓縮到第幾級都永存的內容 ----
    def _anchors(self, turns: Sequence[Turn]) -> list[Turn]:
        out = []
        for t in turns:
            if t.speaker != "user":
                continue
            if t.emotion.intensity >= 0.8:
                out.append(t)
        return out

    # ---- 三級壓縮 ----
    def _compress_dialogue(self, turns: Sequence[Turn], budget_tokens: int) -> str:
        if not turns:
            return ""
        recent = list(turns[-RECENT_VERBATIM_TURNS:])
        older = list(turns[:-RECENT_VERBATIM_TURNS])
        parts: list[str] = []

        if older:
            scenes: dict[int, list[Turn]] = {}
            for sid, t in zip(self.segment_scenes(turns), older):
                scenes.setdefault(sid, []).append(t)
            lines = []
            for sid, ts in scenes.items():
                raw = " ".join(f"{x.speaker}:{x.text}" for x in ts)
                lines.append(f"[場景{sid}] " + self.summarizer.summarize(raw, 120))
            parts.append("## 較早的對話（場景摘要）\n" + "\n".join(lines))

        anchors = self._anchors(turns)
        if anchors:
            verbatim = "\n".join(f'- 「{a.text}」' for a in anchors)
            parts.append("## ★ 逐字錨點（原話，永不改寫）\n" + verbatim)

        parts.append(
            "## 最近的對話（逐字）\n"
            + "\n".join(f"{t.speaker}: {t.text}" for t in recent)
        )
        text = "\n\n".join(parts)
        while estimate_tokens(text) > budget_tokens and len(recent) > 2:
            recent.pop(0)
            text = "\n\n".join(parts[:-1] + [
                "## 最近的對話（逐字）\n" + "\n".join(f"{t.speaker}: {t.text}" for t in recent)
            ])
        return text

    def compile(self,
                total_budget_tokens: int,
                persona_core: str,
                relational_baseline: str,
                dynamic_state: str,
                turns: Sequence[Turn],
                recalled: Sequence[MemoryItem],
                now: float | None = None) -> dict:
        """輸出各區塊的最終文字與預算使用統計。"""
        now = now or time.time()
        b = {k: int(total_budget_tokens * v) for k, v in BUDGET.items()}

        # recalled：按 retention 排序後填預算（喚醒 ≠ 說出來，這裡只負責「帶上」）
        recalled_sorted = sorted(recalled, key=lambda m: -m.retention(now))
        recalled_lines: list[str] = []
        used = 0
        for m in recalled_sorted:
            line = f"- [{m.level}] {m.content}"
            if m.verbatim:
                line += f'\n  原話：「{m.verbatim}」'
            cost = estimate_tokens(line)
            if used + cost > b["recalled"]:
                break
            recalled_lines.append(line)
            used += cost

        dialogue = self._compress_dialogue(turns, b["dialogue"])

        sections = {
            "persona": persona_core,
            "relational": relational_baseline,
            "recalled": "\n".join(recalled_lines) if recalled_lines else "（本次無喚醒記憶）",
            "dialogue": dialogue,
            "state": dynamic_state,
        }
        usage = {k: estimate_tokens(v) for k, v in sections.items()}
        return {
            "sections": sections,
            "budget": b,
            "usage": usage,
            "over_budget": [k for k in b if usage[k] > b[k]],
            "prompt": "\n\n".join(f"### {k}\n{v}" for k, v in sections.items()),
        }


# =========================================================================
# 6. 鞏固器（離線）—— DESIGN 第 9 節
# =========================================================================

class Consolidator:
    def __init__(self, scorer: SalienceScorer, store: MemoryStore, retriever: AwakeningRetriever) -> None:
        self.scorer = scorer
        self.store = store
        self.retriever = retriever

    def run(self, session_turns: Sequence[Turn], now: float | None = None) -> dict:
        now = now or time.time()
        archived = self.store.prune(now)
        hooks = [
            m for m in self.store.all()
            if m.open_loop.is_open and not m.suppressed
        ]
        hooks.sort(key=lambda m: -m.open_loop.urgency(now))
        return {
            "archived": self.store.prune(now),
            "active": len(self.store.items),
            "hooks": [m.content for m in hooks[:3]],   # 下次可主動提起
        }


# =========================================================================
# 7. Demo
# =========================================================================

def _demo() -> None:
    emb = HashingBigramEmbedder()
    store = MemoryStore()
    scorer = SalienceScorer(emb)
    retriever = AwakeningRetriever(emb, store)
    compiler = ContextCompiler()
    now = time.time()

    def mk(mid, level, content, verbatim="", emo=None, sd=0.0, rel=False, loop=None, pin=False):
        return MemoryItem(
            id=mid, level=level, content=content, verbatim=verbatim,
            emotion=emo or Emotion(), self_disclosure=sd, relational=rel,
            open_loop=loop or OpenLoop(), user_pinned=pin,
        )

    item = mk(
        "ep1", "episodic",
        "使用者說項目被臨時改需求，加班到十點錯過孩子睡前故事",
        verbatim="算了，反正也沒人在意",
        emo=Emotion(-0.7, 0.85, "frustration"), sd=0.8, rel=True,
        loop=OpenLoop(True, "項目週五驗收", now + 2 * 86400),
    )
    item.salience = scorer.score(item, store.all())
    store.add(item)

    promise = mk("rel1", "relational", "約定：項目過了就請喝一年咖啡",
                 emo=Emotion(0.6, 0.4, "warmth"), rel=True)
    promise.subtype = "promise"
    promise.status = "pending"
    promise.salience = 0.9
    store.add(promise)

    pinned = mk("sem1", "semantic", "使用者對花生過敏", pin=True)
    pinned.salience = 1.0
    store.add(pinned)

    forgotten = mk("ep2", "episodic", "使用者提到上週去了趟海邊",
                   emo=Emotion(0.3, 0.2), sd=0.2)
    forgotten.salience = 0.4
    forgotten.created_at = now - 120 * 86400        # 120 天前
    store.add(forgotten)

    print("=== 顯著性與衰減 ===")
    for m in (item, pinned, forgotten):
        print(f"{m.id:6s} salience={m.salience:.2f}  retention={m.retention(now):.3f}  flashbulb={m.is_flashbulb}")

    print("\n=== 喚醒檢索（非 top-k）===")
    affect = Emotion(-0.6, 0.8, "sadness")
    for m in retriever.wake("今天又是加班到很晚", affect, top_k=2, now=now):
        print(f"  → {m.id}: {m.content}")

    print("\n=== Context Compiler（預算 1200 tokens）===")
    turns = [
        Turn("user", "算了，反正也沒人在意", ts=now - 3600, emotion=Emotion(-0.7, 0.9)),
        Turn("ai", "……", ts=now - 3500, emotion=Emotion(-0.3, 0.3)),
        Turn("user", "今天也是加班到十點", ts=now - 60, emotion=Emotion(-0.6, 0.7)),
    ]
    out = compiler.compile(
        total_budget_tokens=1200,
        persona_core="我說話簡短，不濫用安慰句式。我在意的是人有沒有被準確看見。",
        relational_baseline="使用者與我：認識 8 個月。稱呼已演化為「老莫」。進行中的約定：一年咖啡。",
        dynamic_state=f"現在時間：{time.strftime('%H:%M')}；距上次對話：1 小時；我的狀態：安靜、投入。",
        turns=turns,
        recalled=retriever.wake("今天又是加班到很晚", affect, top_k=3, now=now),
        now=now,
    )
    for k, v in out["usage"].items():
        print(f"  {k:10s} {v:4d} / {out['budget'][k]:4d} tokens")
    print("  超預算區塊:", out["over_budget"] or "無")

    print("\n=== 主動提起鉤子（鞏固器輸出）===")
    hooks = Consolidator(scorer, store, retriever).run(turns, now)["hooks"]
    for h in hooks:
        print("  ·", h)


if __name__ == "__main__":
    _demo()
