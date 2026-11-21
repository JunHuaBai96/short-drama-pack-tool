#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShortDramaPackTool - 短剧推广素材整理工具（Windows 桌面版）

功能：
  1. 手动输入剧名 / 直接粘贴剧名与简介；左侧剧名列表按行批量加入；可删除单部或全部剧集
  2. 实时检索北斗任务页（inbeidou.cn/task 数据源）匹配官方简介
  3. 通过北斗 MCP 网关（用户 api-key）导入「推广工作台待发布剧目」
  4. 按规则输出：剧名原文、忠实简介、#关键词库、N 条标题（数量可调，上限 20）
  5. 结果仅显示在工具界面，提供一键复制（单部 / 全部）

作者：阿鹏（为小白的 Facebook 短剧出海工作流定制）
License: MIT
"""

import base64
import difflib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "ShortDramaPackTool"
APP_VERSION = "1.2.1"
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), APP_NAME)
CACHE_FILE = os.path.join(CACHE_DIR, "pool_cache.json")
CONFIG_FILE = os.path.join(CACHE_DIR, "config.json")
CACHE_TTL = 6 * 3600  # 缓存兜底 6 小时（匹配简介时强制刷新）

BEIDOU_API = "https://api-scenter.inbeidou.cn/agent/v1/task/page"  # inbeidou.cn/task 页面数据源
THEATER_IDS = [
    "dramabox", "netshort", "yourchannel_drama", "dramawave", "loveshots",
    "reelshort", "goodshort", "moboreels", "playlet", "flareflow",
    "kalostv", "honeyreels", "dreameshort", "topshort", "starshort",
    "sereal", "touchshort", "stardusttv",
]

DEFAULT_MCP_URL = ("http://gw-da5tlpem1hktoldq1bc0-w2wqzerrpavaetkkal-cn-beijing"
                   ".alicloudapi.com/mcp-servers/beidou-matrix-publish")

INCOMPLETE_NOTE = "页面未提供完整简介"

MAX_KEYWORDS = 20
MAX_TITLES = 20

# ---------------------------------------------------------------- 配置

def load_config():
    cfg = {"mcp_url": DEFAULT_MCP_URL, "api_key": ""}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            cfg.update({k: d[k] for k in cfg if d.get(k)})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 工具函数

def normalize_title(s: str) -> str:
    """标题归一化：去标点/弯引号/多余空白，统一小写。"""
    if not s:
        return ""
    s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _fetch_task_page(app_id, page, order_field, order_dir, timeout=15, retries=1):
    """单次请求任务页数据源（inbeidou.cn/task 页面接口）。失败重试一次。"""
    url = (f"{BEIDOU_API}?language=2&app_id={app_id}&page_num={page}"
           f"&page_size=100&order_field={order_field}&order_dir={order_dir}")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            return ((body or {}).get("body") or {}).get("data") or []
        except Exception:
            if attempt >= retries:
                return []
            time.sleep(0.5)


def fetch_pool(force: bool = False, progress_cb=None, workers: int = 12):
    """实时拉取北斗任务页（inbeidou.cn/task 数据源）。

    覆盖全部排序组合（publish_at/total_income × desc/asc）× 前两页 × 18 剧场，
    否则会漏掉 60% 以上的剧目（导致按剧名匹配不到简介）。
    force=True 强制实时检索，完全不读缓存；匹配简介流程必须用 force=True。
    """
    if not force and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_TTL:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    jobs = [(a, p, f, d)
            for a in THEATER_IDS
            for f in ("publish_at", "total_income")
            for d in ("desc", "asc")
            for p in (1, 2)]
    pool = {}
    done = [0]
    lock = threading.Lock()

    def run(job):
        items = _fetch_task_page(*job)
        with lock:
            for it in items:
                tid = str(it.get("task_id"))
                if not tid or tid == "None":
                    continue
                old = pool.get(tid)
                # 同剧多来源/多排序会出现多条记录，保留简介最完整的那条
                if old is None or len((it.get("description") or "").strip()) > \
                        len((old.get("description") or "").strip()):
                    pool[tid] = it
            done[0] += 1
            if progress_cb:
                progress_cb(done[0], len(jobs))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, jobs))

    result = list(pool.values())
    result.sort(key=lambda x: x.get("publish_at") or "", reverse=True)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception:
        pass
    return result


def match_in_pool(title: str, pool):
    """在任务池中模糊匹配剧名，返回 (record, score) 或 (None, 0)。"""
    norm = normalize_title(title)
    if not norm:
        return None, 0.0
    best, best_score = None, 0.0
    for item in pool:
        t = normalize_title(item.get("title") or "")
        if not t:
            continue
        if len(t) < 10:  # 跳过截断残标题（如 "The"）
            continue
        if t == norm:
            return item, 1.0
        # 前缀匹配（截断剧名场景）
        if len(norm) >= 6 and (t.startswith(norm) or norm.startswith(t)):
            score = 0.92
        else:
            score = difflib.SequenceMatcher(None, t, norm).ratio()
        if score > best_score:
            best, best_score = item, score
    if best_score >= 0.82:
        return best, best_score
    return None, best_score


def looks_truncated(desc: str) -> bool:
    """简介疑似截断判定。"""
    if not desc:
        return True
    if len(desc) < 120:
        return True
    tail = desc.rstrip()
    if tail and tail[-1] not in ".!?'\"。！？":
        return True
    return False


# ---------------------------------------------------------------- 北斗 MCP 客户端

class BeidouMCPError(Exception):
    pass


def parse_mcp_payload(text: str) -> dict:
    """解析 MCP content text：剥离 markdown 标题行 + 双层 JSON 字符串编码。"""
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    s = "\n".join(lines).strip()
    for _ in range(3):
        if s.startswith('"'):
            s = json.loads(s)  # 解开转义字符串
        else:
            break
    if isinstance(s, str) and s.lstrip().startswith("{"):
        return json.loads(s)
    raise BeidouMCPError("无法解析网关返回内容")


class BeidouMCP:
    """最小 MCP streamable-HTTP 客户端（JSON-RPC，兼容 SSE 返回）。"""

    def __init__(self, url, api_key):
        self.url = url
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-api-key": api_key,
            "Accept-Encoding": "identity",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }
        self._inited = False
        self._id = 0

    def _rpc(self, method, params=None, session=None):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        data = json.dumps(payload).encode("utf-8")
        headers = dict(self.headers)
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(self.url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
            raw = r.read().decode("utf-8", "replace")
        obj = None
        if raw.lstrip().startswith("{"):
            obj = json.loads(raw)
        else:
            for line in raw.splitlines():  # SSE
                if line.startswith("data:"):
                    try:
                        p = json.loads(line[5:].strip())
                        if p.get("id") == self._id:
                            obj = p
                    except Exception:
                        continue
        if obj is None:
            raise BeidouMCPError("网关返回无法解析")
        if obj.get("error"):
            raise BeidouMCPError(f"网关错误: {obj['error']}")
        return obj, sid

    def _ensure_init(self):
        if self._inited:
            return
        _, sid = self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": APP_NAME, "version": APP_VERSION}})
        try:
            self._rpc("notifications/initialized", session=sid)
        except Exception:
            pass
        self._inited = True
        self._session = sid

    def call_tool(self, name, arguments=None):
        self._ensure_init()
        obj, _ = self._rpc("tools/call", {"name": name, "arguments": arguments or {}},
                           session=getattr(self, "_session", None))
        result = obj.get("result") or {}
        if result.get("isError"):
            raise BeidouMCPError(f"{name} 调用失败")
        for c in result.get("content") or []:
            if c.get("type") == "text" and c.get("text"):
                return parse_mcp_payload(c["text"])
        raise BeidouMCPError(f"{name} 返回为空")


def fetch_workbench(cfg):
    """拉取推广工作台剧目。返回 (pending_items, all_items)。"""
    mcp = BeidouMCP(cfg.get("mcp_url") or DEFAULT_MCP_URL, cfg.get("api_key") or "")
    d = mcp.call_tool("get_api_fb_workbench_list", {})
    body = d.get("body") or d
    items = body.get("data") or []
    pending = [i for i in items if (i.get("status") or "") == "pending"]
    return pending, items


# ---------------------------------------------------------------- 题材识别 / 关键词 / 标题

GENRE_RULES = [
    # (题材 id, 关键词, 关键词标签池, 标题模板池)
    ("bride", ["bride", "wedding", "marry", "marriage", "groom", "vows"],
     ["RunawayBride", "WeddingDrama", "HiddenIdentity", "RichFamily"],
     ["They Threw Her Away on Her Wedding Day. Big Mistake.",
      "She Ran From the Altar — and Into Her Real Life.",
      "The Bride They Discarded Married Better.",
      "They Mocked the Bride. She Returned as the Boss.",
      "The Wedding Was Off. Her Life Was Just Starting."]),
    ("werewolf", ["alpha", "werewolf", "wolf", "luna", "pack", "omega"],
     ["AlphaKing", "WerewolfRomance", "FatedMate", "LunaQueen"],
     ["The Reject Became the Queen They Feared.",
      "They Rejected the Wrong Wolf.",
      "Her Second Chance Was Their Worst Nightmare.",
      "The Pack Learned Who They Had Cast Out.",
      "She Was Nobody's Luna — Until She Chose Herself."]),
    ("baby", ["baby", "nanny", "toddler", "kids", "children", "heir", "twins", "expectant"],
     ["SingleMom", "SecretHeir", "NannyStory", "FamilyDrama"],
     ["The Child Nobody Wanted Changed Everything.",
      "She Raised the Heir. Then They Learned the Truth.",
      "The Nanny They Underestimated.",
      "Two Little Hands Pulled the Whole Family Together.",
      "They Took Her Child's Future. She Took It Back."]),
    ("mafia", ["mafia", "mob", "gang", "crime", "don", "underworld"],
     ["MafiaRomance", "PowerCouple", "DarkRomance", "UnderworldQueen"],
     ["The Man at the Head of the Table Chose Her.",
      "She Married Into Power. They Never Saw It Coming.",
      "Everyone Feared Him. She Made Him Wait.",
      "The Quiet Bride of the Most Dangerous Man.",
      "They Threatened Her. The Whole City Went Silent."]),
    ("billionaire", ["billionaire", "ceo", "capital", "heir", "fortune", "tycoon"],
     ["Billionaire", "CEOromance", "HiddenHeiress", "RichFamily"],
     ["They Laughed at Her Salary. Not at Her Name.",
      "The Janitor's Daughter Owned the Building.",
      "He Signed the Contract. She Signed the Future.",
      "The Poor Girl at the Table Owned the Company.",
      "Her Real Name Was Worth More Than Their Empire."]),
    ("revenge", ["revenge", "frame", "betray", "cheat", "steal", "took", "bully", "ruin"],
     ["RevengeStory", "ComebackStory", "FaceSlap", "SweetPayback"],
     ["They Took Everything. She Took It Back.",
      "The Quiet One Came Back Louder.",
      "Five Years Later, They Mocked the Wrong Woman.",
      "She Left With Nothing. She Returned With Everything.",
      "One Lie Ended Them. One Truth Saved Her."]),
    ("identity", ["secret", "hidden", "identity", "disguise", "impoverished", "true identity"],
     ["HiddenIdentity", "SecretHeiress", "TrueColors", "BigReveal"],
     ["They Thought She Was Poor. Then the Truth Walked In.",
      "The Nobody in the Corner Was the Owner.",
      "Her Last Name Changed the Whole Room.",
      "They Planned to Humiliate Her. They Planned Wrong.",
      "The Secret Sat Quietly Through Dinner."]),
    ("escape", ["escape", "run", "flee", "runaway", "vanish", "left"],
     ["Runaway", "SecondChance", "NewLife", "StrongFemaleLead"],
     ["She Walked Out and Never Looked Back.",
      "The Door Closed. Her Life Opened.",
      "Leaving Was the Best Decision She Ever Made.",
      "They Watched Her Go. Then They Begged.",
      "One Suitcase. One Exit. One New Beginning."]),
    ("survival", ["survive", "island", "wild", "forest", "mountain", "storm", "king", "lion"],
     ["SurvivalStory", "Wilderness", "StrongFemaleLead", "Adventure"],
     ["The Wilderness Didn't Break Her. It Built Her.",
      "Alone Against Everything. She Chose to Stay.",
      "They Left Her Behind. She Outlived Them All.",
      "No Signal. No Help. No Fear.",
      "The Storm Took Everything Except Her."]),
]

STOPWORDS_TITLE = {"the","they","their","that","this","with","from","couldn","would","should","didn","doesn","wasn","weren","have","has","had","never","about","into","when","where","what"}


def pick_title_word(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]{4,}", title or "")
             if w.lower() not in STOPWORDS_TITLE]
    return max(words, key=len).title() if words else ""

DEFAULT_TAGS = ["ShortDrama", "MiniSeries", "DramaReels", "StoryTime", "MustWatch"]
EXTRA_TAGS = ["ShortFilm", "WebSeries", "NewSeries", "WatchNow", "FullSeries",
              "DramaAddict", "SeriesLover", "BingeWatch", "NewEpisode", "DramaClips",
              "ReelsVideo", "TrendingNow", "Episode1", "SneakPeek", "PlotTwist"]


def detect_genre(title: str, synopsis: str):
    text = f"{title} {synopsis}".lower()
    scores = {}
    for gid, kws, _, _ in GENRE_RULES:
        s = sum(text.count(k) for k in kws)
        if s > 0:
            scores[gid] = s
    return max(scores, key=scores.get) if scores else None


def generate_keywords(title: str, synopsis: str, count: int = 4):
    """生成 # 开头的推广关键词（3-20 个）。"""
    gid = detect_genre(title, synopsis)
    tags = ["ShortDrama", "MiniSeries"]
    if gid:
        genre_tags = next(r[2] for r in GENRE_RULES if r[0] == gid)
        tags.extend(genre_tags)
    else:
        tags.append("StoryTime")
    w = pick_title_word(title)
    if w:
        tags.append(w)
    tags.extend(t for t in DEFAULT_TAGS if t not in tags)
    tags.extend(t for t in EXTRA_TAGS if t not in tags)
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    count = max(3, min(MAX_KEYWORDS, count))
    return ["#" + t for t in out[:count]]


GENERAL_TITLES = [
    "The Nobody They Ignored Changed Everything.",
    "One Decision. One Night. One New Life.",
    "They Underestimated the Wrong Person.",
    "The Truth Walked in When They Least Expected It.",
    "She Was Never the Weak One. They Just Weren't Watching.",
    "Everything Fell Apart the Moment She Stopped Asking.",
    "The Quiet One at the Table Held All the Cards.",
    "What They Did in the Dark Came Out at Dinner.",
    "She Didn't Fight Back. She Just Won.",
    "The Ending They Never Saw Coming.",
    "She Stopped Asking for Permission. Everything Changed.",
    "They Buried Her Name. She Built an Empire on It.",
    "The Final Word Was Hers. They Just Didn't Know It Yet.",
    "One Room. One Secret. One Way Out.",
    "The Person They Forgot Came Back First.",
    "He Showed Her the Door. She Bought the Building.",
    "The Last Laugh Wasn't Loud. It Was Final.",
    "Everyone Chose a Side. The Wrong One.",
    "She Didn't Need a Hero. She Needed Ten Minutes.",
    "The Truth Cost Nothing. It Changed Everything.",
]


def generate_titles(title: str, synopsis: str, count: int = 10):
    """生成标题草稿（可编辑）。剧名词槽 → 题材模板 → 通用模板，均不重复。"""
    gid = detect_genre(title, synopsis)
    key = pick_title_word(title)
    pool = []
    if key:
        pool.extend([
            f"The Truth About {key} They Never Wanted Told.",
            f"{key}: The Story They Tried to Erase.",
            f"Everyone Lied About {key}. She Didn't.",
            f"{key} Changed Two Families Forever.",
            f"The Night {key} Came Back, Everything Broke.",
        ])
    if gid:
        pool.extend(next(r[3] for r in GENRE_RULES if r[0] == gid))
    pool.extend(GENERAL_TITLES)
    seen, out = set(), []
    for t in pool:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= max(1, min(MAX_TITLES, count)):
            break
    return out[:max(1, min(MAX_TITLES, count))]


# ---------------------------------------------------------------- GUI

DRAMA_TEMPLATE = """【剧名】{title}

【剧情简介】
{synopsis}

【关键词库】
{keywords}

【{title_count} 条标题】
{titles}
"""


class DramaTab:
    """一部剧的编辑标签页。"""

    def __init__(self, notebook, title=""):
        self.frame = ttk.Frame(notebook)
        self.title_var = tk.StringVar(value=title)
        self.status_var = tk.StringVar(value="待匹配简介")
        self.keyword_count = 4
        self.title_count = 10

        head = ttk.Frame(self.frame)
        head.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(head, text="剧名：").pack(side="left")
        ttk.Entry(head, textvariable=self.title_var, width=42).pack(
            side="left", padx=4, fill="x", expand=True)
        ttk.Label(head, textvariable=self.status_var, foreground="#0a7d32").pack(side="right")

        body = ttk.Frame(self.frame)
        body.pack(fill="both", expand=True, padx=6, pady=2)

        # 简介区
        g1 = ttk.LabelFrame(body, text="剧情简介（忠实原文；缺失时请手动粘贴补全）")
        g1.pack(fill="both", expand=True, side="left", padx=(0, 4))
        self.synopsis = tk.Text(g1, width=52, height=14, wrap="word", undo=True)
        self.synopsis.pack(fill="both", expand=True, padx=2, pady=2)

        # 生成区
        g2 = ttk.Frame(body)
        g2.pack(fill="both", expand=True, side="left")
        gk = ttk.LabelFrame(g2, text="关键词库（# 开头，空格分隔，可改）")
        gk.pack(fill="x")
        self.keywords = tk.StringVar()
        ttk.Entry(gk, textvariable=self.keywords).pack(fill="x", padx=2, pady=2)
        gt = ttk.LabelFrame(g2, text="标题（每行一条，可改）")
        gt.pack(fill="both", expand=True, pady=(4, 0))
        self.titles = tk.Text(gt, width=46, height=9, wrap="word", undo=True)
        self.titles.pack(fill="both", expand=True, padx=2, pady=2)

        gf = ttk.LabelFrame(g2, text="本剧结果预览（一键复制，仅显示在界面）")
        gf.pack(fill="both", expand=True, pady=(4, 0))
        self.result = tk.Text(gf, height=8, wrap="word")
        self.result.pack(fill="both", expand=True, padx=2, pady=2)
        self.result.config(state="disabled")

        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Button(bar, text="生成关键词与标题", command=self.generate).pack(side="left")
        ttk.Button(bar, text="复制本剧结果", command=self.copy_result).pack(side="left", padx=6)
        ttk.Label(bar, text="关键词数:").pack(side="left", padx=(12, 2))
        self.kw_spin = ttk.Spinbox(bar, from_=3, to=MAX_KEYWORDS, width=3)
        self.kw_spin.set(4)
        self.kw_spin.pack(side="left")
        ttk.Label(bar, text="标题数:").pack(side="left", padx=(12, 2))
        self.ti_spin = ttk.Spinbox(bar, from_=5, to=MAX_TITLES, width=3)
        self.ti_spin.set(10)
        self.ti_spin.pack(side="left")

    def current_title(self):
        return self.title_var.get().strip()

    def generate(self):
        title = self.current_title()
        synopsis = self.synopsis.get("1.0", "end").strip()
        if not title:
            messagebox.showwarning(APP_NAME, "请先填写剧名。")
            return
        try:
            self.keyword_count = int(self.kw_spin.get())
        except Exception:
            self.keyword_count = 4
        try:
            self.title_count = int(self.ti_spin.get())
        except Exception:
            self.title_count = 10
        kws = generate_keywords(title, synopsis, self.keyword_count)
        titles = generate_titles(title, synopsis, self.title_count)
        self.keywords.set(" ".join(kws))
        self.titles.delete("1.0", "end")
        self.titles.insert("1.0", "\n".join(titles))
        self.render_result()
        if not synopsis:
            self.status_var.set("⚠ 未提供简介——" + INCOMPLETE_NOTE + "，请手动粘贴")
        else:
            self.status_var.set("已生成 ✓（可编辑后复制）")

    def build_result_text(self):
        title = self.current_title()
        synopsis = self.synopsis.get("1.0", "end").strip() or ("（" + INCOMPLETE_NOTE + "，请补充）")
        kws = self.keywords.get().strip()
        titles = [l.strip() for l in self.titles.get("1.0", "end").splitlines() if l.strip()]
        return DRAMA_TEMPLATE.format(
            title=title, synopsis=synopsis, keywords=kws, title_count=len(titles),
            titles="\n".join(f"{i+1}. {t}" for i, t in enumerate(titles)))

    def render_result(self):
        self.result.config(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", self.build_result_text())
        self.result.config(state="disabled")

    def copy_result(self):
        text = self.build_result_text()
        copy_to_clipboard(text)
        self.status_var.set("已复制到剪贴板 ✓")


def copy_to_clipboard(text):
    if _ROOT is None:
        return
    _ROOT.clipboard_clear()
    _ROOT.clipboard_append(text)
    _ROOT.update()


_ROOT = None


def simple_count_dialog(message, default=20):
    """模态弹窗：询问一个正整数（取消返回 None）。"""
    dialog = tk.Toplevel(_ROOT)
    dialog.title("导入数量")
    dialog.transient(_ROOT)
    dialog.grab_set()
    dialog.resizable(False, False)
    result = {"value": None}

    ttk.Label(dialog, text=message, padding=12, wraplength=320).pack()
    var = tk.StringVar(value=str(default))
    entry = ttk.Entry(dialog, textvariable=var, width=10, justify="center")
    entry.pack(padx=12)
    entry.select_range(0, "end")
    entry.focus_set()

    def ok(event=None):
        try:
            v = int(var.get())
            if v <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("导入数量", "请输入正整数。", parent=dialog)
            return
        result["value"] = v
        dialog.destroy()

    def cancel(event=None):
        dialog.destroy()

    btns = ttk.Frame(dialog)
    btns.pack(pady=10)
    ttk.Button(btns, text="确定", command=ok).pack(side="left", padx=6)
    ttk.Button(btns, text="取消", command=cancel).pack(side="left", padx=6)
    dialog.bind("<Return>", ok)
    dialog.bind("<Escape>", cancel)
    dialog.update_idletasks()
    x = _ROOT.winfo_x() + (_ROOT.winfo_width() - dialog.winfo_width()) // 2
    y = _ROOT.winfo_y() + (_ROOT.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")
    _ROOT.wait_window(dialog)
    return result["value"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global _ROOT
        _ROOT = self
        self.title(f"{APP_NAME} v{APP_VERSION} · 短剧推广素材整理")
        self.geometry("1080x720")
        self.minsize(900, 600)
        self.pool = []
        self.tabs = []  # list[DramaTab]

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(6, 0))
        row1 = ttk.Frame(top)
        row1.pack(fill="x")
        ttk.Button(row1, text="＋ 手动添加剧名", command=self.on_add_manual).pack(side="left")
        ttk.Button(row1, text="1️⃣ 导入北斗推广工作台待发布剧目",
                   command=self.on_import_workbench).pack(side="left", padx=6)
        ttk.Button(row1, text="⚙ 北斗 API Key", command=self.open_settings).pack(side="left")
        ttk.Button(row1, text="2️⃣ 匹配简介（实时检索任务页）",
                   command=self.on_match_all).pack(side="left", padx=6)
        ttk.Button(row1, text="🗑 删除当前剧集", command=self.on_delete_current).pack(side="left")
        ttk.Button(row1, text="🗑🗑 删除全部剧集", command=self.on_delete_all).pack(side="left", padx=6)

        self.progress_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.progress_var, foreground="#555").pack(side="right")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        # 左：剧名粘贴区
        left = ttk.LabelFrame(mid, text="剧名列表（每行一个）")
        left.pack(fill="both", side="left", padx=(0, 6))
        left.config(width=300)
        left.pack_propagate(False)
        self.name_box = tk.Text(left, width=34, wrap="none", undo=True)
        self.name_box.pack(fill="both", expand=True, padx=2, pady=2)
        lbar = ttk.Frame(left)
        lbar.pack(fill="x", padx=2, pady=2)
        ttk.Button(lbar, text="按行加入右侧", command=self.on_add_from_box).pack(side="left")
        ttk.Button(lbar, text="清空", command=lambda: self.name_box.delete("1.0", "end")).pack(side="left", padx=4)
        ttk.Label(left, text="手动粘贴剧名（每行一个）或工作台导入的结果会放进这里；\n确认后点「按行加入右侧」批量建页。",
                  foreground="#666", wraplength=280, justify="left").pack(fill="x", padx=4, pady=(0, 4))

        # 右：剧集标签页
        right = ttk.Frame(mid)
        right.pack(fill="both", expand=True, side="left")
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=6)
        ttk.Button(bottom, text="全部重新生成", command=self.on_generate_all).pack(side="left")
        ttk.Button(bottom, text="📋 一键复制全部结果", command=self.on_copy_all).pack(side="left", padx=8)
        ttk.Label(bottom, foreground="#555",
                  text="规则：剧名保持原文 · 简介忠实原文（缺失标注） · 关键词 # 开头 · 标题语言随剧名").pack(side="left", padx=12)

        self.statusbar = ttk.Label(self, text="提示：左侧粘贴剧名按行加入，或导入工作台剧目 → 匹配简介 → 生成 → 复制。",
                                   relief="sunken", anchor="w")
        self.statusbar.pack(fill="x", side="bottom")

    # ---------- 剧集管理 ----------
    def add_tab(self, title=""):
        tab = DramaTab(self.notebook, title)
        self.notebook.add(tab.frame, text=(title[:16] or "新剧集"))
        self.tabs.append(tab)
        self.notebook.select(tab.frame)
        return tab

    def _current_tab_index(self):
        if not self.tabs:
            return None
        try:
            sel = self.notebook.select()
        except Exception:
            sel = None
        for i, t in enumerate(self.tabs):
            if str(t.frame) == sel:
                return i
        return 0

    def on_delete_current(self):
        idx = self._current_tab_index()
        if idx is None:
            messagebox.showinfo(APP_NAME, "还没有可删除的剧集。")
            return
        tab = self.tabs[idx]
        name = tab.current_title() or "未命名剧集"
        if not messagebox.askyesno("删除剧集", f"确定删除「{name}」吗？"):
            return
        self.tabs.pop(idx)
        self.notebook.forget(tab.frame)
        if self.tabs:
            self.notebook.select(self.tabs[min(idx, len(self.tabs) - 1)].frame)
        self.progress_var.set(f"已删除 1 部（剩 {len(self.tabs)} 部）")
        self.statusbar.config(text=f"已删除「{name}」。当前共 {len(self.tabs)} 部剧集。")

    def on_delete_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "还没有可删除的剧集。")
            return
        if not messagebox.askyesno("删除全部剧集",
                                   f"确定删除全部 {len(self.tabs)} 部剧集吗？此操作不可撤销。"):
            return
        n = len(self.tabs)
        for tab in self.tabs:
            self.notebook.forget(tab.frame)
        self.tabs.clear()
        self.progress_var.set(f"已删除全部 {n} 部（剩 0 部）")
        self.statusbar.config(text="已删除全部剧集。可从左侧剧名列表重新按行加入。")

    # ---------- 左侧列表 ----------
    def on_add_from_box(self):
        names = [l.strip() for l in self.name_box.get("1.0", "end").splitlines() if l.strip()]
        if not names:
            messagebox.showinfo(APP_NAME, "左侧列表为空。")
            return
        for n in names:
            self.add_tab(n)
        self.statusbar.config(text=f"已加入 {len(names)} 部剧集，按页面顺序排列。")

    def on_add_manual(self):
        self.add_tab("")

    # ---------- 设置 ----------
    def open_settings(self):
        cfg = load_config()
        dialog = tk.Toplevel(self)
        dialog.title("北斗 API Key 设置")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        saved = {"ok": False}

        ttk.Label(dialog, text="MCP 网关地址（mcpServers.beidou-matrix-publish.url）",
                  padding=(12, 10, 12, 0)).pack(fill="x")
        url_var = tk.StringVar(value=cfg.get("mcp_url") or DEFAULT_MCP_URL)
        ttk.Entry(dialog, textvariable=url_var, width=72).pack(padx=12, fill="x")

        ttk.Label(dialog, text="API Key（headers.x-api-key，北斗后台获取）",
                  padding=(12, 8, 12, 0)).pack(fill="x")
        key_var = tk.StringVar(value=cfg.get("api_key") or "")
        ttk.Entry(dialog, textvariable=key_var, width=72, show="*").pack(padx=12, fill="x")

        def save():
            cfg["mcp_url"] = url_var.get().strip() or DEFAULT_MCP_URL
            cfg["api_key"] = key_var.get().strip()
            if save_config(cfg):
                saved["ok"] = True
                self.statusbar.config(text="北斗 API Key 已保存（本地 config.json）。")
            else:
                messagebox.showerror(APP_NAME, "保存失败：无法写入配置文件。", parent=dialog)
            dialog.destroy()

        btns = ttk.Frame(dialog)
        btns.pack(pady=10)
        ttk.Button(btns, text="保存", command=save).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dialog.destroy).pack(side="left", padx=6)
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")
        self.wait_window(dialog)
        return saved["ok"]

    # ---------- 工作台导入 ----------
    def on_import_workbench(self):
        cfg = load_config()
        if not cfg.get("api_key"):
            messagebox.showinfo(APP_NAME, "首次使用请先配置北斗 API Key。")
            if not self.open_settings():
                return
            cfg = load_config()
            if not cfg.get("api_key"):
                return
        self.progress_var.set("连接北斗 MCP 网关…")
        self.statusbar.config(text="正在通过 MCP 网关读取推广工作台待发布剧目…")

        def work():
            try:
                pending, items = fetch_workbench(cfg)
                self.after(0, lambda: self._import_workbench_done(pending, items, None))
            except Exception as e:
                self.after(0, lambda: self._import_workbench_done(None, None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _import_workbench_done(self, pending, items, err):
        if err:
            self.progress_var.set("导入失败")
            self.statusbar.config(text=f"工作台导入失败：{err}")
            messagebox.showerror(APP_NAME, f"工作台导入失败：\n{err}\n\n请检查 API Key 与网络。")
            return
        rows = []
        if pending:
            source = "待发布剧目"
            for it in pending:
                d = it.get("drama") or {}
                title = (d.get("title") or d.get("chinese_title") or "").strip()
                if title:
                    rows.append((title, (d.get("description") or "").strip()))
        else:
            # 无待发布时回退：最近剧单日期的全部剧目
            dates = sorted({(i.get("list_date") or "") for i in items if i.get("list_date")},
                           reverse=True)
            latest = dates[0] if dates else ""
            for it in items:
                if (it.get("list_date") or "") != latest:
                    continue
                d = it.get("drama") or {}
                title = (d.get("title") or d.get("chinese_title") or "").strip()
                if title:
                    rows.append((title, (d.get("description") or "").strip()))
            source = f"最近剧单 {latest}（当前无待发布剧目）"
        if not rows:
            self.progress_var.set("工作台为空")
            self.statusbar.config(text="工作台没有剧目。请先在北斗后台把剧目加入推广剧单。")
            return
        n = simple_count_dialog(f"工作台{source}共 {len(rows)} 部，导入前多少部？",
                                default=min(len(rows), 20))
        if n is None:
            self.statusbar.config(text="已取消导入。")
            return
        rows = rows[:n]
        for title, desc in rows:
            tab = self.add_tab(title)
            if desc:
                tab.synopsis.insert("1.0", desc)
                if looks_truncated(desc):
                    tab.status_var.set("⚠ 简介疑似截断——" + INCOMPLETE_NOTE + "，请补全")
                else:
                    tab.status_var.set("简介已就绪（工作台原文）")
            else:
                tab.status_var.set("⚠ " + INCOMPLETE_NOTE + "，请手动粘贴")
        self.pool = fetch_pool(force=False) if not self.pool else self.pool
        self.progress_var.set(f"已导入 {len(rows)} 部（{source}）")
        self.statusbar.config(text=f"已从推广工作台导入 {len(rows)} 部剧目，可点「匹配简介」补全后生成。")

    # ---------- 匹配简介 ----------
    def on_match_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "请先添加剧集（左侧剧名按行加入 / 手动添加 / 工作台导入）。")
            return
        self.progress_var.set("实时检索任务页…")
        self.statusbar.config(text="正在实时检索 inbeidou.cn/task 任务页数据源（全部排序组合，强制刷新，禁止缓存）…")

        def progress(idx, total):
            self.after(0, lambda: self.progress_var.set(f"实时拉取任务页 {idx}/{total}"))

        def work():
            try:
                pool = fetch_pool(force=True, progress_cb=progress)
            except Exception as e:
                self.after(0, lambda: self.statusbar.config(text=f"匹配失败：{e}"))
                return
            self.pool = pool
            if not pool:
                self.after(0, lambda: self.statusbar.config(
                    text="实时检索失败（任务页数据源无返回）。请检查网络后重试。"))
                self.after(0, lambda: self.progress_var.set("检索失败"))
                return
            self.after(0, lambda: self._do_match())

        threading.Thread(target=work, daemon=True).start()

    def _do_match(self):
        hit, miss, skip = 0, 0, 0
        for tab in self.tabs:
            title = tab.current_title()
            if not title:
                continue
            if tab.synopsis.get("1.0", "end").strip():
                skip += 1  # 已有简介不覆盖
                continue
            rec, score = match_in_pool(title, self.pool)
            if rec:
                desc = (rec.get("description") or "").strip()
                if desc:
                    tab.synopsis.insert("1.0", desc)
                    if looks_truncated(desc):
                        tab.status_var.set(f"⚠ 匹配成功但疑似截断（相似度 {score:.0%}）——" + INCOMPLETE_NOTE)
                    else:
                        tab.status_var.set(f"✓ 已匹配官方简介（相似度 {score:.0%}）")
                    hit += 1
                    continue
            tab.status_var.set("✗ 未匹配到——" + INCOMPLETE_NOTE + "，请手动粘贴简介")
            miss += 1
        self.progress_var.set(f"匹配完成：{hit} 成功 / {skip} 已有简介 / {miss} 待补")
        self.statusbar.config(text="匹配完成。绿色=已就绪；黄色提示=请手动粘贴简介后重新生成。")

    # ---------- 生成 / 复制 ----------
    def on_generate_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "还没有剧集。")
            return
        for tab in self.tabs:
            tab.generate()
        self.statusbar.config(text="已全部生成（标题为草稿，可直接在各剧页修改后复制）。")

    def on_copy_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "还没有剧集。")
            return
        parts = []
        for i, tab in enumerate(self.tabs, 1):
            parts.append(f"—— 第 {i} 部 / 共 {len(self.tabs)} 部 ——\n{tab.build_result_text()}")
        copy_to_clipboard("\n\n".join(parts))
        self.statusbar.config(text=f"已复制全部 {len(self.tabs)} 部结果到剪贴板 ✓")


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
