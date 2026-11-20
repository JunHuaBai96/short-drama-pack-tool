#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShortDramaPackTool - 短剧推广素材整理工具（Windows 桌面版）

功能：
  1. 截图识别剧名（Windows 自带 OCR，无需安装第三方库）
  2. 手动输入剧名 / 直接粘贴剧名与简介
  3. 从北斗任务页自动匹配官方简介（本地缓存 + 模糊匹配）
  4. 按规则输出：剧名原文、忠实简介、#关键词库、N 条标题（数量可调）
  5. 结果仅显示在工具界面，提供一键复制（单部 / 全部）

作者：阿鹏（为小白的 Facebook 短剧出海工作流定制）
License: MIT
"""

import base64
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "ShortDramaPackTool"
APP_VERSION = "1.0.0"
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), APP_NAME)
CACHE_FILE = os.path.join(CACHE_DIR, "pool_cache.json")
CACHE_TTL = 6 * 3600  # 缓存 6 小时

BEIDOU_API = "https://api-scenter.inbeidou.cn/agent/v1/task/page"
THEATER_IDS = [
    "dramabox", "netshort", "yourchannel_drama", "dramawave", "loveshots",
    "reelshort", "goodshort", "moboreels", "playlet", "flareflow",
    "kalostv", "honeyreels", "dreameshort", "topshort", "starshort",
    "sereal", "touchshort", "stardusttv",
]

INCOMPLETE_NOTE = "页面未提供完整简介"

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


def fetch_pool(force: bool = False, progress_cb=None):
    """拉取并缓存北斗任务池。返回 list[dict]。"""
    if not force and os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_TTL:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    pool = {}
    total = len(THEATER_IDS)
    for idx, app_id in enumerate(THEATER_IDS):
        for page in (1, 2):
            url = (f"{BEIDOU_API}?language=2&app_id={app_id}&page_num={page}"
                   f"&page_size=100&order_field=publish_at&order_dir=desc")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read().decode("utf-8"))
                data = ((body or {}).get("body") or {}).get("data") or []
                for item in data:
                    pool[str(item.get("task_id"))] = item
            except Exception:
                continue
        if progress_cb:
            progress_cb(idx + 1, total)
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


# ---------------------------------------------------------------- OCR（Windows 自带）

OCR_PS1 = r"""
param([string]$ImagePath)
$ErrorActionPreference = 'Stop'
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime]
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
  }
  $engine = $null
  foreach ($lang in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($engine) { break }
  }
  if (-not $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  }
  if (-not $engine) { Write-Output '__OCR_NO_ENGINE__'; exit 2 }
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  Write-Output $result.Text
} catch {
  Write-Output ('__OCR_FAIL__ ' + $_.Exception.Message)
  exit 3
}
"""


def ocr_image(image_path: str) -> str:
    """调用 Windows 自带 OCR 识别图片文字。非 ASCII 路径先复制到临时 ASCII 路径。"""
    safe = os.path.abspath(image_path)
    try:
        image_path.encode("ascii")
    except UnicodeEncodeError:
        ext = os.path.splitext(image_path)[1] or ".png"
        safe = os.path.join(tempfile.gettempdir(), f"sdpt_ocr_img{ext}")
        shutil.copyfile(image_path, safe)
    ps1 = os.path.join(tempfile.gettempdir(), "sdpt_ocr.ps1")
    with open(ps1, "w", encoding="ascii") as f:
        f.write(OCR_PS1)
    ps_exe = shutil.which("powershell") or "powershell"
    proc = subprocess.run(
        [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", ps1, "-ImagePath", safe],
        capture_output=True, encoding="utf-8", errors="replace", timeout=120,
    )
    out = (proc.stdout or "").strip()
    if out.startswith("__OCR_"):
        raise RuntimeError(out)
    return out


def extract_candidate_titles(ocr_text: str):
    """从 OCR 文本里提取疑似剧名候选行（英文标题优先，过滤 UI 噪音）。"""
    noise_pat = re.compile(
        r"(task|page|filter|search|sort|income|publish|detail|click|download|"
        r"refresh|login|logout|home|menu|setting|total|count|app_id|language|"
        r"钻|积分|任务|筛选|搜索|排序|收益|发布|下载|刷新|登录|设置|首页)", re.I)
    cands = []
    seen = set()
    for raw in ocr_text.splitlines():
        # 一行里被 丨 / | / ｜ 连接的多行先拆开
        for line in re.split(r"[丨|｜│┃]", raw):
            line = line.strip()
            if not line or len(line) < 6 or len(line) > 90:
                continue
            if noise_pat.search(line):
                continue
            # 疑似剧名：以大写字母开头 / 含空格分词的英文短语
            words = line.split()
            if len(words) >= 2 and re.match(r"^[A-Z0-9'\u2019]", line):
                key = normalize_title(line)
                if key and key not in seen:
                    seen.add(key)
                    cands.append(line)
    return cands


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
    ("werewolf", ["alpha", "werewolf", "wolf", "luna", "pack"],
     ["AlphaKing", "WerewolfRomance", "FatedMate", "LunaQueen"],
     ["The Reject Became the Queen They Feared.",
      "They Rejected the Wrong Wolf.",
      "Her Second Chance Was Their Worst Nightmare.",
      "The Pack Learned Who They Had Cast Out.",
      "She Was Nobody's Luna — Until She Chose Herself."]),
    ("baby", ["baby", "nanny", "toddler", "kids", "children", "heir", "twins"],
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
    ("revenge", ["revenge", "frame", "betray", "cheat", "steal", "took", "bully"],
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
    ("survival", ["survive", "island", "wild", "forest", "mountain", "storm"],
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


def detect_genre(title: str, synopsis: str):
    text = f"{title} {synopsis}".lower()
    scores = {}
    for gid, kws, _, _ in GENRE_RULES:
        s = sum(text.count(k) for k in kws)
        if s > 0:
            scores[gid] = s
    return max(scores, key=scores.get) if scores else None


def generate_keywords(title: str, synopsis: str, count: int = 4):
    """生成 # 开头的推广关键词（3-5 个）。"""
    gid = detect_genre(title, synopsis)
    tags = ["ShortDrama", "MiniSeries"]
    if gid:
        genre_tags = next(r[2] for r in GENRE_RULES if r[0] == gid)
        tags.extend(genre_tags[:2])
    else:
        tags.append("StoryTime")
    # 从剧名抽一个词做话题标签
    w = pick_title_word(title)
    if w:
        tags.append(w)
    tags.extend(t for t in DEFAULT_TAGS if t not in tags)
    # 去重并按请求数量截取（至少 3，至多 5）
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    count = max(3, min(5, count))
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
]


def generate_titles(title: str, synopsis: str, count: int = 10):
    """生成标题草稿（可编辑）。优先用题材模板，再补通用模板。"""
    gid = detect_genre(title, synopsis)
    pool = []
    if gid:
        pool.extend(next(r[3] for r in GENRE_RULES if r[0] == gid))
    pool.extend(GENERAL_TITLES)
    # 按剧名关键词造 2 条专属标题
    key = pick_title_word(title)
    if key:
        pool.insert(0, f"The Truth About {key} They Never Wanted Told.")
        pool.insert(1, f"{key}: The Story They Tried to Erase.")
    seen, out = set(), []
    for t in pool:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= count:
            break
    while len(out) < count and pool:
        out.append(pool[len(out) % len(pool)])
    return out[:count]


# ---------------------------------------------------------------- GUI

DRAMA_TEMPLATE = """【剧名】{title}

【剧情简介】
{synopsis}

【关键词库】
{keywords}

【10 条标题】
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
        gk = ttk.LabelFrame(g2, text="关键词库（# 开头，逗号分隔，可改）")
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
        self.kw_spin = ttk.Spinbox(bar, from_=3, to=5, width=3,
                                   command=lambda: None)
        self.kw_spin.set(4)
        self.kw_spin.pack(side="left")
        ttk.Label(bar, text="标题数:").pack(side="left", padx=(12, 2))
        self.ti_spin = ttk.Spinbox(bar, from_=5, to=15, width=3)
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

    def build_result_text(self):
        title = self.current_title()
        synopsis = self.synopsis.get("1.0", "end").strip() or ("（" + INCOMPLETE_NOTE + "，请补充）")
        kws = self.keywords.get().strip()
        titles = [l.strip() for l in self.titles.get("1.0", "end").splitlines() if l.strip()]
        return DRAMA_TEMPLATE.format(
            title=title, synopsis=synopsis, keywords=kws,
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

    ttk.Label(dialog, text=message, padding=12).pack()
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
    # 居中
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
        top.pack(fill="x", padx=8, pady=6)

        ttk.Button(top, text="1️⃣ 选择截图识别剧名", command=self.on_ocr).pack(side="left")
        ttk.Button(top, text="2️⃣ 导入北斗页面全部剧集（按页面顺序）",
                   command=self.on_import_all).pack(side="left", padx=6)
        ttk.Button(top, text="＋ 手动添加剧名", command=self.on_add_manual).pack(side="left")
        ttk.Button(top, text="3️⃣ 匹配简介（全部）", command=self.on_match_all).pack(side="left", padx=6)

        self.progress_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.progress_var, foreground="#555").pack(side="right")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8, pady=4)

        # 左：剧名粘贴区
        left = ttk.LabelFrame(mid, text="剧名列表（手动输入，每行一个；也可从截图识别后粘贴到此）")
        left.pack(fill="both", side="left", padx=(0, 6))
        left.config(width=300)
        left.pack_propagate(False)
        self.name_box = tk.Text(left, width=34, wrap="none", undo=True)
        self.name_box.pack(fill="both", expand=True, padx=2, pady=2)
        lbar = ttk.Frame(left)
        lbar.pack(fill="x", padx=2, pady=2)
        ttk.Button(lbar, text="按行加入右侧", command=self.on_add_from_box).pack(side="left")
        ttk.Button(lbar, text="清空", command=lambda: self.name_box.delete("1.0", "end")).pack(side="left", padx=4)

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

        self.statusbar = ttk.Label(self, text="提示：先选截图识别剧名，或直接粘贴剧名；再点“匹配简介”。",
                                   relief="sunken", anchor="w")
        self.statusbar.pack(fill="x", side="bottom")

    # ---------- 功能 ----------
    def add_tab(self, title=""):
        tab = DramaTab(self.notebook, title)
        self.notebook.add(tab.frame, text=(title[:16] or "新剧集"))
        self.tabs.append(tab)
        self.notebook.select(tab.frame)
        return tab

    def on_ocr(self):
        path = filedialog.askopenfilename(
            title="选择截图", filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"), ("所有文件", "*.*")])
        if not path:
            return
        self.progress_var.set("OCR 识别中…")
        self.statusbar.config(text="正在用 Windows 自带 OCR 识别截图…")

        def work():
            try:
                text = ocr_image(path)
            except Exception as e:
                self.after(0, lambda: self._ocr_done(None, str(e)))
                return
            self.after(0, lambda: self._ocr_done(text, None))

        threading.Thread(target=work, daemon=True).start()

    def _ocr_done(self, text, err):
        if err:
            self.progress_var.set("OCR 失败")
            self.statusbar.config(text=f"OCR 失败：{err}。可在下方手动输入剧名。")
            messagebox.showwarning(APP_NAME,
                                   "OCR 识别失败（部分系统未启用 Windows OCR 语言包）。\n"
                                   "请直接在左下方文本框手动粘贴剧名，每行一个。")
            return
        cands = extract_candidate_titles(text)
        self.name_box.delete("1.0", "end")
        self.name_box.insert("1.0", "\n".join(cands))
        self.progress_var.set(f"OCR 完成，识别出 {len(cands)} 个候选剧名")
        self.statusbar.config(text="已把候选剧名放入左侧列表——删掉噪音行后点“按行加入右侧”。")

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

    def on_import_all(self):
        """按页面顺序（publish_at 降序）导入北斗全部剧集。"""
        self.progress_var.set("拉取北斗任务池…")
        self.statusbar.config(text="正在拉取北斗任务页（首次较慢，之后走本地缓存）…")

        def progress(idx, total):
            self.after(0, lambda: self.progress_var.set(f"拉取任务池 {idx}/{total}"))

        def work():
            try:
                pool = fetch_pool(force=False, progress_cb=progress)
                self.after(0, lambda: self._import_done(pool, None))
            except Exception as e:
                self.after(0, lambda: self._import_done(None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _import_done(self, pool, err):
        if err:
            self.progress_var.set("导入失败")
            self.statusbar.config(text=f"拉取失败：{err}")
            return
        self.pool = pool
        self.progress_var.set(f"任务池就绪（{len(pool)} 条，含缓存）")
        # 默认导入最新发布日期那一批（对应当前任务页），可手动调整条数
        dates = sorted({(i.get("publish_at") or "")[:10] for i in pool if i.get("publish_at")},
                       reverse=True)
        latest = dates[0] if dates else ""
        batch = [i for i in pool if (i.get("publish_at") or "")[:10] == latest]
        n = simple_count_dialog(f"页面 {latest} 共 {len(batch)} 部剧集，导入前多少部？",
                                default=min(len(batch), 20))
        if n is None:
            self.statusbar.config(text="已取消导入。")
            return
        batch = batch[:n]
        for item in batch:
            t = (item.get("title") or "").strip()
            if not t:
                continue
            tab = self.add_tab(t)
            desc = (item.get("description") or "").strip()
            if desc:
                tab.synopsis.insert("1.0", desc)
                if looks_truncated(desc):
                    tab.status_var.set("⚠ 简介疑似截断——" + INCOMPLETE_NOTE + "，请补全")
                else:
                    tab.status_var.set("简介已就绪（北斗页面原文）")
            else:
                tab.status_var.set("⚠ " + INCOMPLETE_NOTE + "，请手动粘贴")
        self.statusbar.config(text=f"已按页面顺序导入 {len(batch)} 部剧集（{latest}）。")

    @staticmethod
    def _ask_count(batch, latest):
        """兼容入口：弹窗询问导入条数。"""
        return simple_count_dialog(f"页面 {latest} 共 {len(batch)} 部剧集，导入前多少部？",
                                   default=min(len(batch), 20))

    def on_match_all(self):
        if not self.tabs:
            messagebox.showinfo(APP_NAME, "请先添加剧集（截图识别 / 手动输入 / 导入全部）。")
            return
        if not self.pool:
            self.progress_var.set("拉取北斗任务池…")
            self.statusbar.config(text="首次匹配需先拉取任务池…")

        def work():
            try:
                pool = fetch_pool(force=False)
                self.pool = pool
                self.after(0, lambda: self._do_match())
            except Exception as e:
                self.after(0, lambda: self.statusbar.config(text=f"匹配失败：{e}"))

        threading.Thread(target=work, daemon=True).start()
        return

    def _do_match(self):
        hit, miss = 0, 0
        for tab in self.tabs:
            title = tab.current_title()
            if not title:
                continue
            if tab.synopsis.get("1.0", "end").strip():
                continue  # 已有简介不覆盖
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
        self.progress_var.set(f"匹配完成：{hit} 成功 / {miss} 待补")
        self.statusbar.config(text="匹配完成。绿色=已就绪；黄色提示=请手动粘贴简介后重新生成。")

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
