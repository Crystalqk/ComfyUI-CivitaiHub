# ComfyUI-CivitaiHub

在 ComfyUI 里浏览、搜索、下载 Civitai 模型的插件。**不挂梯子可用**，界面内嵌在 ComfyUI 页面里，点右下角图标打开。

作者：**B站 Crystal_晴空**（蓝P 同名）

---

## 特性

**开箱即用**
- 模型快照（5.5 万条，含完整底模标记）随包分发，首次启动自动解出，**打开面板列表就在**，不用等重建
- 23800+ 张封面已打包，96 个底模各自的前 300 条里 81.5% 直接出图；其余 18% 是视频模型，源站本身只有 mp4，没有静态封面可打包

**浏览与搜索**
- 类型、底模、排序（推荐 / 最新 / 最多下载）、时间范围筛选
- 与站内一致的排序：官方置顶块在前，其余按点赞降序
- 搜索支持模型名、作者、标签
- 成人内容开关

**详情页**
- 多版本切换，每个版本显示发布时间
- 版本图集、触发词、文件列表、简介
- 点图看大图 + 该图的完整生成参数（提示词、采样器、步数、CFG、种子、使用的 LoRA）

**下载**
- 64 段并发，实测约 5.8 MB/s（单流只有 40~220 KB/s）
- 断点续传（中断后只补缺失的段）
- SHA256 校验，不匹配的文件不会进入 `models/`
- 按类型分别指定保存目录，支持完整路径

**性能**
- 筛选和翻页走本地快照，约 0.002 秒
- 封面按筛选组合预取：每个组合前 300 张，看到 30% 时后台补下一批
- 详情图、新模型封面按需下载并缓存

---

## 安装

1. 把 `ComfyUI-CivitaiHub` 整个文件夹放进 `ComfyUI/custom_nodes/`
2. 重启 ComfyUI
3. 打开任意工作流，点右下角的圆形图标

---

## 必填配置：API Key

**不填只能浏览，不能下载模型文件。**

获取步骤：

1. 浏览器打开 [civitai.com](https://civitai.com) 并登录
2. 右上角头像 → **Account settings**（账户设置）
3. 左侧选 **API Keys** → **Add API key**
4. 名字随便填（例如 `comfyui`），生成后复制那串 **32 位字符**
5. 回到插件 → **通道设置** → 粘贴进「Civitai API Key」→ 点 **保存设置**

Key 只保存在本机 `config.json`，不写进代码，不发送到 Civitai 之外的任何地方。分享插件前删掉这个文件即可。

---

## 保存目录

「通道设置」里每个模型类型单独一行：

| 类型 | 默认目录 |
|---|---|
| LoRA / LoCon / DoRA / Aesthetic Gradient | `models/loras` |
| Checkpoint | `models/checkpoints` |
| Embedding (TextualInversion) | `models/embeddings` |
| ControlNet | `models/controlnet` |
| VAE | `models/vae` |
| Upscaler | `models/upscale_models` |
| Hypernetwork | `models/hypernetworks` |
| Motion | `animatediff_models` |

两种写法都支持：

```
loras                        相对 models/ 的子目录
D:\AI\MyLoras                完整路径（模型放 ComfyUI 外面时用）
%USERPROFILE%\loras          环境变量会展开
```

点「重置目录」可以把所有输入框恢复默认值。

---

## 为什么快

Civitai 的接口和图片都存在国内连不上的域名上，这个插件做了三件事绕开：

**接口走镜像** —— `civitai.red` / `civitai.work` 是同一后端的两个入口，插件同时请求，用先返回的那个，坏掉的会暂停一段时间再试。

**图片走代理** —— `image.civitai.com` 被墙，所有预览图经 `wsrv.nl` 转发并压到 240px，比原图快得多。

**数据本地化** —— 模型列表存本地快照，筛选、排序、翻页都在内存里做（约 0.002 秒），只有搜索关键词才查网络。

下载走 `civitai.red` 拿 307 重定向到 Cloudflare R2 的签名地址，然后 64 段并发拉取。单流只有 40~220 KB/s，64 段能到 5.8 MB/s。

---

## 缓存与磁盘占用

```
web/covers/                318 MB   23811 张封面，随插件打包，不清理
data/models_snapshot.json.gz 27 MB  压缩快照，随插件分发
cache/models_snapshot.json 106 MB   运行时解压出来的快照
cache/thumbs/              运行时缩略图，翻页时按需补
```

首次启动时会把 `data/` 里的压缩快照解成 `cache/models_snapshot.json`，之后所有筛选都读它。这样装好就能用，不必等两分钟的冷爬取。

清理机制：

- 默认浏览视图（全部类型 / LoRA，推荐排序）的封面标 `core`，永久保留
- 其他组合标 `temp`，切换筛选时清、启动面板时也清
- 每次启动额外清掉 30 天没碰过的文件
- 下载失败的文件自动删除，不会留下半截模型

想让榜单纯净如初，删掉 `cache/models_snapshot.json` 重启，插件会从随包快照重新解一份出来。

---

## 安全须知

**凭据会经过第三方镜像。** 下载请求带 `Authorization: Bearer <API Key>`，发往 `civitai.red` / `civitai.work`，不是 Civitai 官方域名。原因是官方域名在国内不可达，而不挂梯子就只剩镜像可选——镜像运营方能看到这个密钥。建议用专门的 API Key，方便随时在 Civitai 后台吊销。

**配置接口只接受本机请求。** `/civitai_hub/config` 会读写凭据，已加 loopback 校验，非本机来源返回 403。API Key 在响应里默认显示为 `***`，用 `_has_api_key` 标识是否已配置。如果你用 `--listen 0.0.0.0` 对外开 ComfyUI，请在反向代理上加鉴权——本插件的校验只认 TCP 来源地址，反代过来的请求会被当作本机。

**`/civitai_hub/thumb` 是通用图片代理。** 只校验 `http(s)` 前缀，没有域名白名单，请求经 `wsrv.nl` 转发。够不到内网，但可以被当作免费转发器使用，并占用本地封面缓存。

**`/civitai_hub/local` 的 folder 参数有白名单。** 只接受 `[A-Za-z0-9_-]{1,64}`，含 `..`、斜杠、盘符的取值直接 400。

---

## 已知限制

**视频封面无法显示** —— 部分模型用 `.mp4` 当封面。图片代理不处理视频，`image.civitai.com` 又直连不上，所以这类模型会显示占位图加“重试”按钮。受影响最明显的是视频类底模（Wan Video、LTXV、MiniMax、Hunyuan Video）——它们的大部分条目只有视频预览。能做的是用同一模型的其他静态截图代替，一张静态图都没有时只能留空。

**部分类型本身没有预览图** —— `Wildcards`（提示词库）、`Workflows` / `ComfyWorkflows`（工作流 json）、`MotionModule` 这些不是模型，C 站上大多数条目没配图，所以这些类型下封面覆盖率低是正常的，不是打包漏了。

**真人/动漫无法准确区分** —— 试过用标签、底模、模型名判断，都不可靠：`realistic` 既指真人照片也指写实画风动漫，Anima、Krea 2、Pony 这些底模下两种模型都有。要准确区分需要本地跑图像分类模型，不在本插件范围内。

**“最新”有延迟** —— 最新视图独立缓存 10 分钟，启动时预热。超过 10 分钟会重新拉一次（约 3 秒）。

**快照有边界** —— 随包的快照 55107 条，只保证前 1 万名的顺序和站上一字不差（实测缺 2 条）。更冷门的内容靠关键词搜索，搜索走实时查询。

**快照会自动刷新** —— 启动 90 秒后跑一次，之后每 6 小时一次：只拉站上「Most Liked」前 100 页（约 2.5 分钟，后台进行），按返回顺序重写快照，顺手把新出现的模型和它们的封面补进随包资源。因为站点自己的排序就是这个接口的返回顺序，所以刷完的顺序是照抄，不是本地重算。涨赞和上新最多滞后 6 小时；想立刻刷一次，重启 ComfyUI 就行。

---

## 通道设置里的高级项

**Civitai API 镜像** —— 默认 `civitai.red, civitai.work`，两个都挂了才需要换。填任意能访问 `/api/v1` 的反代地址。

**图片代理** —— 默认 `wsrv.nl`。它挂了才需要换，填任意支持 `?url=` 参数的公益图片代理。

**并发下载段数** —— 默认 64。网络不稳可调到 32；实测 96 段收益已趋缓。

---

## 故障排查

**面板打不开** → 看 ComfyUI 控制台有没有 `[CivitaiHub]` 开头的报错。

**图片不显示** → 检查「通道设置 → 检测通道」，看图片代理是否可达。首次加载某张图需要 1~2 秒，之后从本地缓存读。

**下载失败** → 确认 API Key 已填且有效。错误信息会显示在「下载任务」面板里。

**筛选结果和站内不一致** → 插件搜索时匿名请求。站内登录后服务端会返回更大的模型集合，那个差异来自登录态，插件不再读取浏览器 Cookie 去补它。

**改了代码不生效** → 前端改动按 `Ctrl+Shift+R` 强制刷新；后端改动必须重启 ComfyUI。

**快照需要重置** → 删掉 `cache/models_snapshot.json` 后重启，会从随包的 `data/models_snapshot.json.gz` 重新解出原始那份，下一次刷新再按站上顺序覆盖。

---

## 目录结构

```
ComfyUI-CivitaiHub/
├── __init__.py            节点注册
├── hub_routes.py          HTTP 接口、快照、筛选
├── hub_client.py          镜像通信、图片代理、文件解析
├── hub_downloader.py      下载任务队列、分段下载、校验
├── hub_config.py          配置读写
├── hub_providers.py       站点适配
├── selfcheck.py           自检
├── config.json            本地配置（API Key 存这里，不要提交）
├── data/
│   └── models_snapshot.json.gz   随包分发的压缩快照（26 MB）
├── cache/                 运行时数据（首次启动自动生成，不提交）
└── web/
    ├── civitai_hub_v142.js    前端面板（单文件）
    └── covers/                打包的封面图与索引
```

## 关键参数

改这些值可以调行为，都在 `hub_routes.py` 顶部：

```python
_THUMB_BASE_PER_COMBO = 300   每个筛选组合预取的封面数
_THUMB_STORE_WIDTH    = 240   封面存储宽度（240px 在 300px 卡片上几乎无差别）
_THUMB_REFILL_RATIO   = 0.3   新一批下来后，滚过它前 30% 就补下一批
_SNAPSHOT_VERSION     = 10    结构版本，改了会自动作废旧快照重建
_DEEP_BLOCK           = 300   每次从 v1 补的临时模型条数（翻到列表 75% 时触发）
_NEWEST_TTL           = 600   "最新"视图缓存秒数
_REFRESH_PAGES        = 100   每次刷新拉站上 Most Liked 的页数（前 1 万名）
_REFRESH_INTERVAL     = 21600 刷新间隔，6 小时
```

`config.json` 里可调：

```json
"download_segments": 64        单文件并发段数
"save_dirs": {"LORA": "loras"} 各类型保存目录
```

---

## 许可

仅用于个人学习和使用。所有模型版权归 Civitai 及其作者所有，下载后请遵守各模型页面标注的许可条款。
