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
import hashlib
import json
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "ShortDramaPackTool"
APP_VERSION = "1.5.0"
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), APP_NAME)
CACHE_FILE = os.path.join(CACHE_DIR, "pool_cache.json")
CONFIG_FILE = os.path.join(CACHE_DIR, "config.json")
CACHE_TTL = 6 * 3600  # 缓存兜底 6 小时（匹配简介时强制刷新，不走缓存）
LOG_FILE = os.path.join(CACHE_DIR, "match_log.txt")


def log_match(line: str):
    """匹配流程运行日志（可审计实时访问情况）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        from datetime import datetime
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except Exception:
        pass

BEIDOU_API = "https://api-scenter.inbeidou.cn/agent/v1/task/page"  # inbeidou.cn/task 页面数据源
THEATER_IDS = [
    "dramabox", "netshort", "yourchannel_drama", "dramawave", "loveshots",
    "reelshort", "goodshort", "moboreels", "playlet", "flareflow",
    "kalostv", "honeyreels", "dreameshort", "topshort", "starshort",
    "sereal", "touchshort", "stardusttv",
]

DEFAULT_MCP_URL = ("http://gw-da5tlpem1hktoldq1bc0-w2wqzerrpavaetkkal-cn-beijing"
                   ".alicloudapi.com/mcp-servers/beidou-matrix-publish")

# LLM：默认阿里百炼（DashScope）OpenAI 兼容模式；密钥只存本地 config.json，不硬编码
LLM_DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LLM_DEFAULT_MODEL = "qwen-plus"
LLM_TIMEOUT = 60

INCOMPLETE_NOTE = "页面未提供完整简介"

MAX_KEYWORDS = 20
MAX_TITLES = 20

# ---------------------------------------------------------------- 配置

def load_config():
    cfg = {"mcp_url": DEFAULT_MCP_URL, "api_key": "",
           "llm_api_key": "", "llm_base_url": LLM_DEFAULT_BASE, "llm_model": LLM_DEFAULT_MODEL}
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


def _fetch_task_page(app_id, page, order_field, order_dir, timeout=15, retries=2):
    """单次请求任务页数据源（inbeidou.cn/task 页面接口）。失败重试两次（短退避）。"""
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
            time.sleep(0.8 * (attempt + 1))


def fetch_pool(force: bool = False, progress_cb=None, workers: int = 8, max_seconds: int = 180):
    """实时拉取北斗任务页（inbeidou.cn/task 数据源）。

    覆盖全部排序组合（publish_at/total_income × desc/asc）× 前两页 × 18 剧场，
    否则会漏掉 60% 以上的剧目（导致按剧名匹配不到简介）。
    force=True 强制实时检索，完全不读缓存；匹配简介流程必须用 force=True。
    返回 (pool_list, failed_requests)。max_seconds 为总时长兜底，超时返回已收集的部分结果。
    """
    if not force and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_TTL:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f), 0
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
    failed = [0]
    lock = threading.Lock()

    def run(job):
        items = _fetch_task_page(*job)
        with lock:
            if not items:
                failed[0] += 1
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

    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(run, j): j for j in jobs}
    timed_out = False
    try:
        for _ in as_completed(futs, timeout=max_seconds):
            pass
    except Exception:
        # 总时长兜底：网络极端缓慢时收敛，返回已收集的部分结果
        timed_out = True
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if timed_out:
        with lock:
            failed[0] = len(jobs) - done[0]
        log_match(f"实时检索总时长超 {max_seconds}s，完成 {done[0]}/{len(jobs)}，按部分结果继续")

    result = list(pool.values())
    result.sort(key=lambda x: x.get("publish_at") or "", reverse=True)
    log_match(f"实时检索任务页数据源: 请求 {len(jobs)} 个, 失败 {failed[0]} 个, "
              f"获得 {len(result)} 条")
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception:
        pass
    return result, failed[0]


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

# v1.4.0：常见动词形态（标题名词槽过滤用；promises/years 等名词不受影响）
VERB_FORMS = {
    "needed","needs","accepted","accepts","abandoned","abandons","begs","begged","beg",
    "suffered","suffers","promised","trusted","believed","believes","deserved","saved",
    "rescued","rescues","denied","blamed","chose","chosen","meant","owed","wanted",
    "loved","hated","feared","waited","wait","waits","held","hold","holds","stood",
    "walked","called","reached","knocked","answered","belong","belonged","apologized",
    "admitted","watched","noticed","carried","paid","cost","won","failed","survived",
    "returned","changed","change","changes","replaced","rejected","framed","ruined",
    "cheated","mocked","humiliated","kicked","dumped","punished","hurt","hurts",
    "deny","denies","arrived","arrives","disappeared","disappears","discovered",
    "discovers","decided","decides","agreed","agrees","refused","refuses","signed",
    "signs","swore","swears","vowed","vows","cried","cries","smiled","smiles",
    "entered","enters","brought","bring","brings","sent","send","sends","kept","keep",
    "keeps","put","puts","sat","sits","grew","hid","lied","lie","lies","fell","falls",
    "took","taken","gave","given","made","left","ran","broke","found","told","asked",
    "went","married","lost","bought","died","forced","fled","stole","stolen","opened",
    "closed","learned","turned","expired","thrown","burst","handed","offered",
    "exposed","revealed","became","kneels","kneel","kneeled","struggled","struggles",
    "broken","hidden","forgiven","admit","admits","losing","searching","begging",
    "trying","running","coming","taking","hiding","looking","falling","breaking",
    "kneeling","struggling","marrying","forcing","framing","protecting","promising",
    "rescuing","waiting","planning","regretted","regrets",
}

STOPWORDS_ALL = {
    "the","a","an","and","but","or","so","if","then","than","that","this","these","those",
    "he","she","it","they","them","his","her","their","him","its","we","our","you","your",
    "was","were","is","are","be","been","being","am","do","does","did","doing","done",
    "have","has","had","having","will","would","can","could","should","shall","may","might","must",
    "not","no","nor","yes","very","just","only","even","also","still","yet","ever","never",
    "about","into","onto","upon","over","under","after","before","while","when","where","what","who","whom","whose","which",
    "with","without","from","for","of","off","out","in","on","at","by","to","up","down","again",
    "there","here","one","two","three","five","ten","first","next","last","other","another","same","all","any","both","each","few","more","most","some","such",
    "because","until","although","though","however","instead","finally","later","once","soon","now",
    "when","every","own","mr","mrs","ms","dr","couldn","wouldn","shouldn","didn","doesn","wasn","weren","hasn","haven","won","ain",
}

CONFLICT_WORDS = ["steal","stole","stolen","taken","took","rejected","reject","betray","betrayed","lied","lie","lying","force","forced","framed","frame","fired","broke","broken","ruin","ruined","destroy","destroyed","cheat","cheated","bullied","bully","humiliated","humiliate","mocked","replace","replaced","abandon","abandoned","kicked","dumped","divorce","punish","punished","hurt"]

# 头衔/称谓词：不算人名（Alpha Kieran → 人名是 Kieran，不是 Alpha）
HONORIFICS = {"Alpha","Luna","Beta","Omega","King","Queen","Lord","Lady","Sir","Mr","Mrs","Miss","Ms","Doctor","Doctor","Captain","General","Boss","Don","Prince","Princess"}

# -ing 结尾但实际是名词的常用词（不受动词过滤）
NOUN_ING = {"wedding","weddings","morning","mornings","evening","evenings","building","buildings","clothing","offering","offerings","landing","landings","warning","warnings"}
REVEAL_WORDS = ["truth","revealed","reveal","secret","identity","discovered","discovery","exposed","expose","actually","real","disguise","hidden","found out","true","uncovered","unveiled"]
LOVE_WORDS = ["love","loved","romance","romantic","married","marriage","wedding","bride","groom","heart","soulmate","fate","destiny","kiss"]
RICH_WORDS = ["billionaire","ceo","chairman","company","empire","fortune","wealth","wealthy","capital","business","heir","heiress","tycoon","executive","conglomerate"]
POWER_WORDS = ["queen","king","alpha","luna","pack","boss","president","don","mafia","gang","underworld","warrior","general"]


def _is_name_word(w: str) -> bool:
    return (len(w) >= 3 and w[0].isupper() and w[1:].islower()
            and w.lower() not in STOPWORDS_ALL and w not in {"The","A","An","He","She","It","But","And","When","After","Before","Then","Their","There","This","That","They","His","Her","However","One","Five","Years","Later","Miss","Mister","Mr","Mrs"})


def _tag_camel(s: str) -> str:
    s = s.replace("'", "")
    return "".join(w[:1].upper() + w[1:] for w in s.split())


def extract_story_elements(title: str, synopsis: str) -> dict:
    """从剧名+简介抽取内容元素：人名、角色短语、高频词、冲突/反转/时间跳变标记。"""
    text = synopsis or ""
    low = text.lower()
    toks = [w.strip("'") for w in re.findall(r"[A-Za-z']+", text)]
    sents = re.split(r"(?<=[.!?])\s+", text) if text else []

    # 人名：全文大写词，统计句首/句中位置——句中出现过（或句首出现≥2次）才算人名
    caps_pos = {}
    for s in sents:
        ws = re.findall(r"[A-Za-z]{2,}", s)
        for i, w in enumerate(ws):
            if w[0].isupper():
                c = caps_pos.setdefault(w, {"mid": 0, "first": 0})
                c["first" if i == 0 else "mid"] += 1
    names = [w for w, c in caps_pos.items()
             if (c["mid"] > 0 or c["first"] > 1) and _is_name_word(w)
             and w not in HONORIFICS]  # Alpha/Luna/King 等头衔词不当人名
    # 疑似人名全集（含只出现一次的，如 "Aria was..."）：高频词槽要避开
    maybe_names = {w.lower() for w, c in caps_pos.items() if c["mid"] > 0 or c["first"] > 0}

    # 高频实义词
    freq = {}
    for w in toks:
        wl = w.lower()
        if len(wl) >= 4 and wl not in STOPWORDS_ALL:
            freq[wl] = freq.get(wl, 0) + 1
    top_words = sorted(freq, key=lambda x: (-freq[x], x))

    # 角色短语：连续两个实义词的 bigram，按词频之和排序
    # 过滤：含人名的（They Took the Anya Sorensen ❌）、动词结尾/开头的（family fell ❌）
    verb_tails = {"fell","destroy","destroys","destroyed","came","took","takes","gave","gives","made","makes","left","leaves","runs","ran","broke","breaks","found","finds","told","tells","asked","asks","gets","went","returns","return","married","marries","lost","loses","buys","bought","die","dies","died","forced","fled","flees","steals","stole","stolen","lied","hides","hid","hired","opens","opened","closes","closed","learns","learned","turns","turned","expires","expired","cuts","thrown","fabricates","fabricated","bursts","burst","falls","fallen","hands","handed","offers","offered","throws","exposes","exposed","reveals","revealed","becomes","became","admit","admits","regretted","regrets",
                  # v1.4.0 补充：标题槽混入的动词性短语（needed rescue / the accepted 等）
                  "needed","needs","accepted","accepts","abandoned","abandons","broken","begs","begged","beg","suffered","suffers","promised","promises","trusted","believed","believes","deserved","saved","rescued","rescues","denied","blamed","chose","chosen","meant","owed","wanted","loved","hated","feared","waited","wait","waits","grew","grown","held","hold","holds","stood","walked","called","reached","knocked","answered","belong","belonged","apologized","admitted","watched","noticed","carried","paid","cost","won","failed","survived","returned","changed","change","changes","replaced","replaces","rejected","rejects","framed","frames","ruined","ruins","cheated","cheats","mocked","mocks","humiliated","humiliates","kicked","dumped","punished","hurt","hurts","denied","deny","denies","arrived","arrives","disappeared","disappears","discovered","discovers","decided","decides","agreed","agrees","refused","refuses","signed","signs","swore","swears","vowed","vows","cried","cries","smiled","smiles","entered","enters","brought","bring","brings","sent","send","sends","kept","keep","keeps","put","puts","sat","sits","grew"}
    name_set = {n.lower() for n in names}
    phrases, seen_ph = [], set()
    for i in range(len(toks) - 1):
        a, b = toks[i].lower(), toks[i + 1].lower()
        b_raw = toks[i + 1]
        a = a.replace("'", "")
        b = b.replace("'", "")
        if (len(a) >= 4 and len(b) >= 4 and a not in STOPWORDS_ALL and b not in STOPWORDS_ALL):
            ph = f"{a} {b}"
            if ph in seen_ph:
                continue
            seen_ph.add(ph)
            if a in name_set or b in name_set:
                continue
            if b_raw[0].isupper():  # 短语尾部是专有名词（wedding Kieran ❌）
                continue
            if b in verb_tails or a in verb_tails:
                continue
            # v1.4.0：过去分词/动词式词尾（needed rescue / pack rejected ❌）
            if a.endswith("ed") or b.endswith("ed"):
                continue
            phrases.append((freq.get(a, 0) + freq.get(b, 0), ph))
    phrases.sort(key=lambda x: -x[0])
    phrases = [p for _, p in phrases]

    return {
        "names": names[:4],
        "maybe_names": maybe_names,
        "top_words": top_words[:10],
        "phrases": phrases[:6],
        "conflict": any(k in low for k in CONFLICT_WORDS),
        "reveal": any(k in low for k in REVEAL_WORDS),
        "love": any(k in low for k in LOVE_WORDS),
        "rich": any(k in low for k in RICH_WORDS),
        "power": any(k in low for k in POWER_WORDS),
        "time_jump": bool(re.search(r"\w+ (?:years|months|days) later|came back|returned", low)),
        "years": (re.search(r"([A-Za-z]+|\d+) (?:years|months|days) later", low) or [None, None]).group(1)
                  if re.search(r"([A-Za-z]+|\d+) (?:years|months|days) later", low) else None,
    }


def pick_title_word(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]{4,}", title or "")
             if w.lower() not in STOPWORDS_TITLE]
    good = [w for w in words if w.lower() not in VERB_FORMS
            and not w.lower().endswith("ed")]
    pick_from = good or words
    return max(pick_from, key=len).title() if pick_from else ""

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
    """生成 # 开头的推广关键词（3-20 个）：题材标签 + 剧名/简介内容标签。"""
    el = extract_story_elements(title, synopsis)
    gid = detect_genre(title, synopsis)
    tags = ["ShortDrama", "MiniSeries"]
    if gid:
        tags.extend(next(r[2] for r in GENRE_RULES if r[0] == gid))
    else:
        tags.append("StoryTime")

    # 内容标签：简介角色短语 / 高频词 / 人名（CamelCase 化，过滤过短的）
    content = []
    for ph in el["phrases"]:
        t = _tag_camel(ph)
        if len(t) >= 6 and t not in content:
            content.append(t)
    for n in el["names"]:
        if n not in content:
            content.append(n)
    for w in el["top_words"]:
        t = w.capitalize()
        if len(t) >= 5 and t not in content:
            content.append(t)
    # 剧名内容词（简介之外再兜一层）
    for w in re.findall(r"[A-Za-z]{4,}", title or ""):
        t = w.capitalize()
        if w.lower() not in STOPWORDS_ALL and t not in content:
            content.append(t)
    # 主题氛围标签按内容标记追加
    if el["reveal"] and "BigReveal" not in tags:
        tags.append("BigReveal")
    if el["conflict"] and "ComebackStory" not in tags:
        tags.append("ComebackStory")
    if el["love"] and "Romance" not in tags:
        tags.append("Romance")
    if el["rich"] and "Billionaire" not in tags:
        tags.append("Billionaire")

    tags.extend(content)
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
    "She Stopped Asking for Permission. Everything Changed.",
    "The Ending They Never Saw Coming.",
    "The Quiet One at the Table Held All the Cards.",
    "What They Did in the Dark Came Out at Dinner.",
    "They Underestimated the Wrong Person.",
    "The Last Laugh Wasn't Loud. It Was Final.",
    "Everyone Chose a Side. The Wrong One.",
    "She Didn't Need a Hero. She Needed Ten Minutes.",
    "The Truth Cost Nothing. It Changed Everything.",
    "One Room. One Secret. One Way Out.",
]


def _title_elements(title: str, synopsis: str):
    """标题可注入的内容元素池：(文本, 是否人名)。
    名词短语（角色短语/剧名词/高频词）用于 the {X} 槽；人名只进人名槽，
    避免 'The Eve Walked In' 这类硬套。"""
    el = extract_story_elements(title, synopsis)
    nouns, names, seen = [], [], set()

    def add(w, is_name=False):
        w = (w or "").strip().replace("'", "")
        if not w or len(w) < 3 or w.lower() in seen:
            return
        lw = w.lower()
        # 名词槽只收名词性元素：滤动词式词尾（needed/losing ❌，promises/wedding ✓）
        if not is_name and (lw.endswith("ed") or lw in VERB_FORMS
                            or (lw.endswith("ing") and lw not in NOUN_ING)):
            return
        seen.add(lw)
        (names if is_name else nouns).append(w)

    for n in el["names"]:
        add(n, is_name=True)
    for p in el["phrases"]:
        add(p.title())
    add(pick_title_word(title))
    for w in re.findall(r"[A-Za-z]{4,}", title or ""):
        if w.lower() not in STOPWORDS_ALL:
            add(w.title())
    for w in el["top_words"][:5]:
        if w in el.get("maybe_names", set()):
            continue  # 疑似人名不进名词槽（Aria ❌）
        add(w.capitalize())
    return el, nouns, names


def _title_skeleton(t: str, elements) -> str:
    """把标题里的内容元素替换成占位符，得到句式骨架指纹（同骨架限流用）。"""
    sk = t
    for e in elements:
        if e:
            sk = re.sub(re.escape(e), "{E}", sk, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", sk).lower().strip()


def _pick_titles(cands, fallback, elements, count, rng):
    """选标题：精确去重 + 近似去重（≥0.80 视为重复）+ 同句式骨架最多 2 条。"""
    picked, seen, skels = [], set(), {}

    def take(pool):
        for t in pool:
            low = re.sub(r"\s+", " ", t.lower()).strip()
            if low in seen:
                continue
            if any(difflib.SequenceMatcher(None, low, p.lower()).ratio() >= 0.80
                   for p in picked):
                continue
            sk = _title_skeleton(t, elements)
            if skels.get(sk, 0) >= 2:
                continue
            seen.add(low)
            skels[sk] = skels.get(sk, 0) + 1
            picked.append(t)
            if len(picked) >= count:
                return True
        return False

    pool1 = list(cands)
    rng.shuffle(pool1)
    if not take(pool1):
        pool2 = list(fallback)
        rng.shuffle(pool2)
        take(pool2)
    return picked[:count]


def generate_titles(title: str, synopsis: str, count: int = 10):
    """生成标题草稿（可编辑）。v1.4.0 重写：
    - 内容驱动：每条标题注入剧名/简介元素（角色短语、人名、剧名实义词、高频词），
      元素轮换避免集中在同一个词上；
    - 多样化：模板槽按 (剧名+简介) 的确定性散列洗牌——同剧重生成结果一致（可复现），
      不同剧句式顺序不同，跨剧不再共用同一批通用句；
    - 去重：精确 + 近似（相似度 ≥0.80）+ 同句式骨架限流（最多 2 条）；
    - 数量保证：内容槽不足时才用题材/通用兜底补足到 count。"""
    el, nouns, names = _title_elements(title, synopsis)
    gid = detect_genre(title, synopsis)
    seed = hashlib.sha1(
        f"{(title or '').strip()}|{(synopsis or '').strip()}".encode("utf-8")
    ).hexdigest()
    rng = random.Random(seed)
    count = max(1, min(MAX_TITLES, count))

    name = names[0] if names else ""
    name2 = names[1] if len(names) > 1 else ""
    years = (el["years"] or "Five").capitalize()

    # 名词元素轮换器：洗牌后循环取用，相邻槽尽量用不同元素
    rot = list(nouns)
    rng.shuffle(rot)
    pos = {"i": 0}

    def X():
        if not rot:
            return ""
        v = rot[pos["i"] % len(rot)]
        pos["i"] += 1
        return v

    def be(x):  # 单复数系动词（The Packs Were / The Lodge Was）
        return "Were" if x and x[-1:].lower() == "s" else "Was"

    def plural(x):  # 元素是否复数（粗判：以 s 结尾）
        return bool(x) and x[-1:].lower() == "s"

    cands = []

    # A. 名词内容槽（无元素时自动跳过）
    x = X()
    if x:
        cands.extend([
            f"The {x} {be(x)} Never Theirs to Take.",
            f"Everything Changed the Night the {x} Disappeared.",
            f"The Truth About the {x} Changed Everything.",
            f"They Underestimated the {x}. It Cost Them Everything.",
            f"The {x} Everyone Believed {be(x)} a Lie.",
            f"All It Took Was the {x}." if plural(x) else f"All It Took Was One {x}.",
            f"The {x} Decided Everything. They Just Didn't Know It Yet.",
            f"The {x} Changed Every Promise in That House." if plural(x)
            else f"One {x} Changed Every Promise in That House.",
        ])
    x = X()
    if x:
        cands.extend([
            f"No One Saw the {x} Coming.",
            f"The {x} They Laughed at Won in the End.",
            f"They Drew the Line. The {x} Crossed It.",
            f"The {x} Held a Secret Nobody Dared to Open.",
            f"It All Came Down to the {x}.",
            f"The {x}. One Decision. One New Life." if plural(x)
            else f"One {x}. One Decision. One New Life.",
        ])
    x = X()
    if x:
        cands.extend([
            f"The {x} {be(x)} Louder Than All Their Lies.",
            f"They Buried the {x}. It Grew Back Louder.",
            f"The {x} Walked In When They Least Expected It.",
        ])

    # B. 人名槽（有人名才启用）
    if name:
        cands.extend([
            f"Everyone Lied About {name}. She Didn't.",
            f"{name} Was Never the Weak One. They Just Weren't Watching.",
            f"The Truth About {name} They Never Wanted Told.",
            f"They Chose Sides. {name} Chose Herself.",
        ])
        y = X()
        if y:
            cands.extend([
                f"{name} and the {y}: The Story They Tried to Erase.",
                f"{name} Walked Away. The {y} Followed.",
                f"They Underestimated {name}. Then Came the {y}.",
            ])
    if name2 and el["conflict"]:
        cands.append(f"{name} and {name2}. Only One of Them Told the Truth.")

    # C. 时间跳变槽
    if el["time_jump"]:
        y = X()
        if y:
            cands.extend([
                f"{years} Years Later, the {y} Came Back to Them.",
                f"{years} Years of Silence Ended With One {y}.",
            ])
        else:
            cands.append(f"{years} Years Later, Everything They Said Came Undone.")

    # D. 剧名词槽（剧名本身进标题，加强剧名关联）
    key = pick_title_word(title)
    if key and key.lower() not in {n.lower() for n in names}:
        cands.extend([
            f"{key}: The Story They Tried to Erase.",
            f"What Happened to {key} Was Never an Accident.",
            f"{key} Changed Every Rule in That House.",
        ])

    # E. 内容标记槽（冲突/反转/情感/权力，各限 1-2 条）
    if el["conflict"]:
        y = X()
        if y:
            cands.extend([
                f"They Took the {y}. She Took Everything Back.",
                f"What They Did to the {y} Could Not Be Undone.",
            ])
    if el["reveal"]:
        y = X()
        if y:
            cands.append(f"The Secret Behind the {y} Rewrote Every Rule.")
    if el["love"]:
        y = X()
        if y:
            cands.append(f"They Promised Forever. The {y} Said Otherwise.")
    if el["power"] or el["rich"]:
        y = X()
        if y:
            cands.append(f"The {y} Belonged to the Wrong Family. Until Tonight.")

    # F. 题材槽（最多 2 条，洗牌取样，降低跨剧重复）
    if gid:
        gtitles = list(next(r[3] for r in GENRE_RULES if r[0] == gid))
        rng.shuffle(gtitles)
        cands.extend(gtitles[:2])

    return _pick_titles(cands, GENERAL_TITLES, nouns + names, count, rng)


# ---------------------------------------------------------------- GUI

DRAMA_TEMPLATE = """【剧名】{title}

【剧情简介】
{synopsis}

【关键词库】
{keywords}

【{title_count} 条标题】
{titles}
"""


# ---------------------------------------------------------------- LLM 生成（默认阿里百炼千问）

LLM_BANNED = ("kill", "murder", "dead", "death", "die", "died", "blood", "suicide",
              "arrest", "jail", "prison", "inmate", "obsession", "obsessed", "affair",
              "cheating", "pregnant", "pregnancy", "heat", "knot", "fangs", "desire")


def _llm_chat(cfg, messages, timeout=LLM_TIMEOUT, retries=1):
    """OpenAI 兼容 chat/completions（默认 DashScope 兼容模式）。返回首条回复文本。"""
    base = (cfg.get("llm_base_url") or LLM_DEFAULT_BASE).strip().rstrip("/")
    key = (cfg.get("llm_api_key") or "").strip()
    model = (cfg.get("llm_model") or LLM_DEFAULT_MODEL).strip()
    if not key:
        raise RuntimeError("未配置 LLM API Key")
    url = base + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.8,
    }).encode("utf-8")
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, method="POST", headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            text = ((body or {}).get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not text.strip():
                raise RuntimeError("模型返回内容为空")
            return text
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}（{detail or e.reason}）"
            if e.code in (401, 403, 404):  # 鉴权/地址错误重试无意义
                break
        except Exception as e:
            last_err = str(e) or e.__class__.__name__
        if attempt < retries:
            time.sleep(1.0)
    raise RuntimeError(f"LLM 调用失败：{last_err}")


def _llm_parse_pack(text, kw_count, ti_count):
    """从模型回复中稳健解析 JSON 包：剥代码围栏、截取首尾大括号、校验并去重。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("模型未返回 JSON（无法解析）")
    data = json.loads(s[i:j + 1])
    kws, titles = [], []

    def clean_list(items, want, prefix_ok=None):
        seen, out = set(), []
        for it in items or []:
            if not isinstance(it, str):
                continue
            w = " ".join(it.split()).strip()
            if not w or w.lower() in seen:
                continue
            seen.add(w.lower())
            out.append(w)
        return out[:want]

    for it in clean_list(data.get("keywords"), kw_count):
        if not it.startswith("#"):
            it = "#" + it.replace(" ", "").replace("#", "")
        kws.append(it)
    titles = clean_list(data.get("titles"), ti_count)
    if not kws or not titles:
        raise RuntimeError("模型返回的 JSON 缺少 keywords/titles 内容")
    return {"keywords": kws, "titles": titles}


def llm_generate_pack(title, synopsis, kw_count, ti_count, cfg, timeout=LLM_TIMEOUT):
    """调用用户配置的 LLM，按剧名+简介生成关键词与标题。失败抛 RuntimeError。"""
    kw_count = max(3, min(MAX_KEYWORDS, kw_count))
    ti_count = max(5, min(MAX_TITLES, ti_count))
    sys_prompt = (
        "You are a promotion copywriter for Facebook short-drama (vertical drama) ads. "
        "Given a drama title and its official synopsis, produce promotion copy tied to THIS "
        "story's specific plot elements (character names, relationships, the central conflict, "
        "the reversal). Do not write generic copy that could fit any drama.\n"
        "Rules:\n"
        f"- keywords: exactly {kw_count} hashtag keywords in English, each starts with '#', "
        "no spaces inside; mix genre tags, character/relationship phrases, plot hooks, emotion words.\n"
        f"- titles: exactly {ti_count} English titles, 8-20 words each, varied sentence "
        "structures and hooks; every title must reference this story's specifics; no near-duplicates.\n"
        f"- Banned words (never use): {', '.join(LLM_BANNED)}.\n"
        "- The synopsis language sets the copy language (English synopsis → English copy).\n"
        'Return ONLY valid JSON: {"keywords": ["#...", ...], "titles": ["...", ...]}'
    )
    user_prompt = (f"Drama title: {title}\n\n"
                   f"Synopsis:\n{synopsis or '（not provided; infer from the title only）'}\n\n"
                   f"Generate {kw_count} keywords and {ti_count} titles now.")
    text = _llm_chat(cfg, [{"role": "system", "content": sys_prompt},
                           {"role": "user", "content": user_prompt}])
    pack = _llm_parse_pack(text, kw_count, ti_count)
    log_match(f"LLM 生成成功: {title}（kw={len(pack['keywords'])} ti={len(pack['titles'])}）")
    return pack


class DramaTab:
    """一部剧的编辑标签页。"""

    def __init__(self, notebook, title=""):
        self.frame = ttk.Frame(notebook)
        self.title_var = tk.StringVar(value=title)
        self.status_var = tk.StringVar(value="待匹配简介")
        self.keyword_count = 4
        self.title_count = 10
        self._llm_busy = False  # AI 生成进行中（防重复点击）

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
        """生成入口：配置了 LLM Key → AI 生成（后台线程，失败自动回退本地规则）；
        未配置 → 直接本地规则生成（原 v1.4.0 逻辑）。"""
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
        cfg = load_config()
        if cfg.get("llm_api_key"):
            if self._llm_busy:
                self.status_var.set("AI 生成中，请等这轮完成…")
                return
            self._llm_busy = True
            self.status_var.set("AI 生成中…（失败会自动回退本地规则）")
            # 必须经 _start_bg：它同时启动 UI 队列轮询，否则结果永远回不来
            _ROOT._start_bg(self._llm_worker(cfg, title, synopsis))
        else:
            self._apply_pack(self._local_pack(title, synopsis), note="本地规则")

    def _local_pack(self, title, synopsis):
        return {"keywords": generate_keywords(title, synopsis, self.keyword_count),
                "titles": generate_titles(title, synopsis, self.title_count)}

    def _llm_worker(self, cfg, title, synopsis):
        # 后台线程只做网络与解析，UI 更新经队列回主线程（跨线程调 Tk 在打包 exe 里会静默崩）
        def work():
            try:
                pack = llm_generate_pack(title, synopsis, self.keyword_count,
                                         self.title_count, cfg)
                pack["_ai"] = True
                _ROOT._ui_queue.put(("llm", self, pack, None))
            except Exception as e:
                log_match(f"LLM 生成失败: {title} → {e}")
                _ROOT._ui_queue.put(("llm", self, None, str(e)))
        return work

    def _llm_done(self, pack, err):
        self._llm_busy = False
        title = self.current_title()
        synopsis = self.synopsis.get("1.0", "end").strip()
        if pack:
            self._apply_pack(pack, note="AI")
            return
        self._apply_pack(self._local_pack(title, synopsis), note=None, llm_error=err)

    def _apply_pack(self, pack, note="AI", llm_error=None):
        self.keywords.set(" ".join(pack["keywords"]))
        self.titles.delete("1.0", "end")
        self.titles.insert("1.0", "\n".join(pack["titles"]))
        self.render_result()
        synopsis = self.synopsis.get("1.0", "end").strip()
        tail = f"（{note} 生成 ✓，可编辑后复制）"
        if llm_error:
            self.status_var.set(f"⚠ AI 生成失败：{llm_error}。已用本地规则兜底生成。")
        elif not synopsis:
            self.status_var.set("⚠ AI 已生成，但未提供简介——" + INCOMPLETE_NOTE +
                                "，建议补全简介后重新生成")
        else:
            self.status_var.set("AI 已生成 ✓" if note == "AI" else
                                "已生成 ✓（本地规则；配置 LLM Key 可获得更贴剧情的结果）")

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
        self._ui_queue = queue.Queue()
        self._polling = False

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
        ttk.Button(row1, text="⚙ 设置（北斗 / LLM）", command=self.open_settings).pack(side="left")
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
        dialog.title("设置 · 北斗 / LLM")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        saved = {"ok": False}

        ttk.Label(dialog, text="MCP 网关地址（mcpServers.beidou-matrix-publish.url）",
                  padding=(12, 10, 12, 0)).pack(fill="x")
        url_var = tk.StringVar(value=cfg.get("mcp_url") or DEFAULT_MCP_URL)
        ttk.Entry(dialog, textvariable=url_var, width=72).pack(padx=12, fill="x")

        ttk.Label(dialog, text="北斗 API Key（headers.x-api-key，北斗后台获取）",
                  padding=(12, 8, 12, 0)).pack(fill="x")
        key_var = tk.StringVar(value=cfg.get("api_key") or "")
        ttk.Entry(dialog, textvariable=key_var, width=72, show="*").pack(padx=12, fill="x")

        ttk.Separator(dialog).pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(dialog, text="—— LLM 生成（留空 Key 则用本地规则兜底）——",
                  foreground="#555").pack(fill="x", padx=12)
        ttk.Label(dialog, text="LLM API Key（默认阿里百炼 DashScope：sk-… 开头；只在本地 config.json 保存）",
                  padding=(12, 4, 12, 0)).pack(fill="x")
        llm_key_var = tk.StringVar(value=cfg.get("llm_api_key") or "")
        ttk.Entry(dialog, textvariable=llm_key_var, width=72, show="*").pack(padx=12, fill="x")

        row_llm = ttk.Frame(dialog)
        row_llm.pack(fill="x", padx=12)
        c1 = ttk.Frame(row_llm); c1.pack(side="left", fill="x", expand=True)
        c2 = ttk.Frame(row_llm); c2.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Label(c1, text="Base URL（OpenAI 兼容，默认阿里百炼）",
                  padding=(0, 4, 0, 0)).pack(fill="x")
        base_var = tk.StringVar(value=cfg.get("llm_base_url") or LLM_DEFAULT_BASE)
        ttk.Entry(c1, textvariable=base_var).pack(fill="x")
        ttk.Label(c2, text="模型（默认 qwen-plus）", padding=(0, 4, 0, 0)).pack(fill="x")
        model_var = tk.StringVar(value=cfg.get("llm_model") or LLM_DEFAULT_MODEL)
        ttk.Entry(c2, textvariable=model_var).pack(fill="x")

        test_var = tk.StringVar(value="")

        def run_test():
            # 主线程先读齐配置；worker 只做网络请求；结果由主线程 after 轮询取回
            test_cfg = {"llm_api_key": llm_key_var.get().strip(),
                        "llm_base_url": base_var.get().strip() or LLM_DEFAULT_BASE,
                        "llm_model": model_var.get().strip() or LLM_DEFAULT_MODEL}
            result = {"msg": None}
            test_var.set("测试中…（最长约 30 秒）")
            test_btn.state(["disabled"])

            def work():
                try:
                    pack = llm_generate_pack("The Lodge They Couldn't Steal",
                                             "A woman inherits a mountain lodge, but the family "
                                             "that tried to steal it returns with a claim no one "
                                             "expected.", 3, 2, test_cfg, timeout=30)
                    result["msg"] = (f"连接成功 ✓（返回 {len(pack['keywords'])} 关键词 / "
                                     f"{len(pack['titles'])} 标题）")
                except Exception as e:
                    result["msg"] = f"测试失败 ✗ {e}"

            threading.Thread(target=work, daemon=True).start()

            def check():
                if result["msg"] is not None:
                    test_var.set(result["msg"])
                    test_btn.state(["!disabled"])
                else:
                    dialog.after(200, check)

            dialog.after(200, check)

        test_row = ttk.Frame(dialog)
        test_row.pack(fill="x", padx=12, pady=(8, 0))
        test_btn = ttk.Button(test_row, text="测试连接", command=run_test)
        test_btn.pack(side="left")
        ttk.Label(test_row, textvariable=test_var, foreground="#0a7d32").pack(side="left", padx=8)

        def save():
            cfg["mcp_url"] = url_var.get().strip() or DEFAULT_MCP_URL
            cfg["api_key"] = key_var.get().strip()
            cfg["llm_api_key"] = llm_key_var.get().strip()
            cfg["llm_base_url"] = base_var.get().strip() or LLM_DEFAULT_BASE
            cfg["llm_model"] = model_var.get().strip() or LLM_DEFAULT_MODEL
            if save_config(cfg):
                saved["ok"] = True
                mode = "AI 生成已启用" if cfg["llm_api_key"] else "未配置 LLM Key（本地规则生成）"
                self.statusbar.config(text=f"设置已保存（本地 config.json）。{mode}。")
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
        self._start_bg(self._bg_workbench(cfg))

    def _bg_workbench(self, cfg):
        def work():
            try:
                pending, items = fetch_workbench(cfg)
                self._ui_queue.put(("workbench", pending, items, None))
            except Exception as e:
                self._ui_queue.put(("workbench", None, None, str(e)))
        return work

    def _start_bg(self, target):
        """后台线程只做计算，所有 UI 更新经队列回到主线程（跨线程调 Tk 会静默崩）。"""
        threading.Thread(target=target, daemon=True).start()
        if not self._polling:
            self._polling = True
            self.after(120, self._poll_ui_queue)

    def _poll_ui_queue(self):
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, idx, total, note = msg
                    self.progress_var.set(note or f"实时拉取任务页 {idx}/{total}")
                elif kind == "pool":
                    _, pool = msg
                    self.pool = pool or []
                    if not self.pool:
                        self.progress_var.set("检索失败")
                        self.statusbar.config(text="实时检索完成但未获得数据，请检查网络后重试。")
                    else:
                        self._do_match()
                    self._polling = False
                    return
                elif kind == "match_error":
                    self._match_error(msg[1])
                    self._polling = False
                    return
                elif kind == "match_stats":
                    _, reqs, n, failed, secs = msg
                    self._fetch_note = (f"已实时访问任务页检索接口（{reqs} 个请求 / {n} 条 / "
                                        f"{secs:.1f}s，未使用缓存）")
                    if failed > reqs // 2:
                        self._fetch_note += f"。注意：{failed} 个请求失败，结果可能不全，建议重试一次。"
                elif kind == "llm":
                    _, tab, pack, err = msg
                    if tab in self.tabs:  # 生成期间被删除的页不再回写
                        tab._llm_done(pack, err)
                elif kind == "workbench":
                    _, pending, items, err = msg
                    self._import_workbench_done(pending, items, err)
                    self._polling = False
                    return
        except queue.Empty:
            pass
        self.after(120, self._poll_ui_queue)

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
        if not self.pool:
            _pool, _ = fetch_pool(force=False)
            self.pool = _pool
        self.progress_var.set(f"已导入 {len(rows)} 部（{source}）")
        self.statusbar.config(text=f"已从推广工作台导入 {len(rows)} 部剧目，可点「匹配简介」补全后生成。")

    # ---------- 匹配简介 ----------
    def on_match_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "请先添加剧集（左侧剧名按行加入 / 手动添加 / 工作台导入）。")
            return
        self.progress_var.set("实时检索 inbeidou.cn/task …")
        self.statusbar.config(text="正在实时访问 https://inbeidou.cn/task 检索接口（全部排序组合，禁止缓存）…")
        self._start_bg(self._bg_match())

    def _bg_match(self):
        def work():
            t0 = time.time()
            try:
                pool, failed = fetch_pool(force=True, progress_cb=lambda i, t: self._ui_queue.put(
                    ("progress", i, t, f"实时访问 inbeidou.cn/task（{i}/{t}）…")))
            except Exception as e:
                log_match(f"实时检索失败: {e}")
                self._ui_queue.put(("match_error", str(e)))
                return
            secs = time.time() - t0
            self._ui_queue.put(("match_stats", len(THEATER_IDS) * 4 * 2, len(pool), failed, secs))
            self._ui_queue.put(("pool", pool))
        return work

    def _match_error(self, err):
        self.progress_var.set("检索失败")
        self.statusbar.config(text=f"实时检索失败：{err}。请检查网络后重试（不会回退缓存）。")

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
        note = getattr(self, "_fetch_note", "")
        if note:
            self.statusbar.config(text=f"{note}。匹配完成：{hit} 成功 / {skip} 已有简介 / {miss} 待补。绿色=已就绪。")
        else:
            self.statusbar.config(text="匹配完成。绿色=已就绪；黄色提示=请手动粘贴简介后重新生成。")

    # ---------- 生成 / 复制 ----------
    def on_generate_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "还没有剧集。")
            return
        n_ai = sum(1 for t in self.tabs if load_config().get("llm_api_key"))
        for tab in self.tabs:
            tab.generate()
        if n_ai:
            self.statusbar.config(
                text=f"已为 {n_ai}/{len(self.tabs)} 部发起 AI 生成（失败自动回退本地规则），"
                     "各剧页状态栏会逐个就绪。")
        else:
            self.statusbar.config(text="未配置 LLM Key，已按本地规则生成（⚙ 设置里可配置阿里百炼）。")

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
