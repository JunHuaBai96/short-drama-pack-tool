# ShortDramaPackTool · 短剧推广素材整理工具

Windows 桌面小工具：**从截图 / 剪贴板 / 手动输入 / 直接粘贴**提取短剧信息，实时检索任务页匹配官方剧情简介，并按固定规则生成可直接复制投放的推广素材。

## 更新记录

**v1.1.0**
- ✨ 支持从**剪贴板粘贴截图**识别剧名（Ctrl+V 或按钮）
- 🛠 修复「选择截图」识别失败：OCR 前自动放大小图 + 两遍识别（整图 → 逐行裁剪重识别）+ 行级清理与剧库纠错，小字号截图 5/5 剧名全部正确
- ✨ 简介匹配改为**实时检索**任务页（inbeidou.cn/task 数据源，强制刷新不走缓存）
- ✨ 新增**删除当前剧集**按钮
- 🛠 「导入北斗页面全部剧集」升级为「**导入北斗推广工作台待发布剧目**」：通过北斗 MCP 网关（用户 api-key）读取工作台剧单，无待发布时回退最近剧单
- 🛠 修复左侧剧名列表上方备注显示不全
- 🛠 关键词数量与标题数量上限均提升至 **20**

## 功能

1. **截图识别剧名**：调用 Windows 自带 OCR（Windows.Media.Ocr），无需安装任何第三方依赖
2. **手动输入剧名**：左侧文本框每行一个，批量加入
3. **官方简介自动匹配**：从任务页接口拉取剧集池（本地缓存 6 小时），按标题模糊匹配（支持截断剧名前缀匹配）
4. **忠实原文输出**：简介不编造不增删；匹配不到或疑似截断时，界面标注「页面未提供完整简介」并提示手动粘贴补全
5. **关键词库**：3–5 个 `#` 开头、适合 Facebook Reels 的推广关键词（按题材智能匹配，可编辑）
6. **标题生成**：默认 10 条简洁英文标题草稿（题材模板 + 通用模板，可编辑），数量可调 5–15 条
7. **一键复制**：单部复制 / 全部复制，结果仅显示在工具界面，不落盘

## 安装

### 方式一：直接使用（推荐）

到 [Releases](../../releases) 下载 `ShortDramaPackTool.exe`，双击运行即可。

- 无需安装 Python
- Windows 10 / 11（需系统已安装至少一个 OCR 语言包，中文或英文系统默认都有）

### 方式二：从源码运行

```bat
git clone https://github.com/JunHuaBai96/short-drama-pack-tool.git
cd short-drama-pack-tool
python short_drama_pack_tool.py
```

仅需 Python 3.10+（纯标准库，零第三方依赖）。

### 从源码打包 exe

```bat
pip install pyinstaller
pyinstaller --onefile --windowed --name ShortDramaPackTool --icon app.ico short_drama_pack_tool.py
```

产物在 `dist/ShortDramaPackTool.exe`。

## 使用方法

1. 点 **「选择截图识别剧名」** 或 **「📋 粘贴截图识别 (Ctrl+V)」**（先截图 Win+Shift+S）；候选剧名（已按剧库纠错）出现在左侧列表，点 **「按行加入右侧」**
   —— 或者直接在左侧文本框粘贴剧名（每行一个）后加入
2. 点 **「3️⃣ 匹配简介（实时检索任务页）」**：实时拉取任务页数据匹配官方简介；没匹配到的剧集按提示手动粘贴简介补全
   —— 或者点 **「2️⃣ 导入北斗推广工作台待发布剧目」**（首次会弹出设置，粘贴你的北斗 api-key）
3. 每部剧的标签页里：设置关键词数量（3–20）与标题数量（5–20），点 **「生成关键词与标题」**；多余的剧用 **「🗑 删除当前剧集」** 移除
4. 检查 / 修改右侧内容后，点 **「复制本剧结果」** 或底部 **「📋 一键复制全部结果」**

### 北斗 API Key 配置

点 **「⚙ 北斗 API Key」**：
- MCP 网关地址（默认已填）：`http://gw-da5tlpem1hktoldq1bc0-w2wqzerrpavaetkkal-cn-beijing.alicloudapi.com/mcp-servers/beidou-matrix-publish`
- x-api-key：北斗后台获取的个人 key
- 配置保存在本地 `%LOCALAPPDATA%\ShortDramaPackTool\config.json`，仅用于访问你的推广工作台

## 输出格式

每部剧固定输出：

```
【剧名】保持页面原文，按页面顺序排列

【剧情简介】忠实原文，缺失时标注「页面未提供完整简介」

【关键词库】3–5 个 # 开头关键词

【N 条标题】语言与剧名一致，与简介相符
```

## 隐私与数据

- 任务池缓存保存在 `%LOCALAPPDATA%\ShortDramaPackTool\pool_cache.json`
- 除匹配简介的网络请求外，所有数据只在本机处理；结果只存在于界面与剪贴板，不写文件

## License

[MIT](LICENSE)
