# ComfyUI-CivitaiHub

把 Civitai 内置进 ComfyUI：在 ComfyUI 页面里浏览、搜索、下载 Civitai 模型，界面内嵌，点右下角图标打开。

作者：**B站 Crystal_晴空**（蓝P 同名）

---

## 怎么把 C 站内置进来的

Civitai 的接口和图片都在国内连不上的域名上，这个插件的做法是**把能预置的都预置到本地，只把变化的部分留给网络**：

**模型目录本地化** —— 随插件带一份压缩快照（`data/models_snapshot.json.gz`，56720 条模型、103 个底模），首次启动解压到 `cache/`。筛选、排序、翻页全部读内存，约 0.002 秒，不等网络。

**封面预置** —— 随 Release 带 23813 张封面（`web/covers/`，368 MB）和 `index.json` 索引。每个筛选组合的前 300 张直接出图；超出部分滚动时按需补，落进运行时缓存。

**接口走镜像** —— `civitai.red` / `civitai.work` 是同一后端的两个入口，插件同时请求、用先返回的，坏掉的暂停一段时间再试。`civitai.com` 本身不参与。

**图片走代理** —— `image.civitai.com` 被墙，预览图经 `wsrv.nl` 转发并压到 240px。已在本地缓存的文件直接读盘，不再走网络。

**排行榜增量同步** —— 定时拉站上「Most Liked」前 100 页，按返回顺序更新已有行、只补新出现的行，旧行一律保留在末尾，不做整份替换。

---

## 安装

**方式一（推荐，开箱即用）**：从 [Releases](https://github.com/Crystalqk/ComfyUI-CivitaiHub/releases/latest) 下载完整包，解压到 `ComfyUI/custom_nodes/`，重启 ComfyUI。

**方式二（源码）**：`git clone` 本仓库到 `ComfyUI/custom_nodes/`，重启 ComfyUI。
本仓库**不含** `web/covers/`（368 MB、23813 个文件，不适合放进 git），所以首屏封面会走代理、一张约 1 秒。想要秒出图就用方式一。

装好后打开任意工作流，点右下角圆形图标。

---

## 配置

首次运行会把 `config.example.json` 作为默认值。想自定义就复制一份：

```
copy config.example.json config.json
```

`config.json` 存 API Key，已被 `.gitignore` 排除，不要提交。

### API Key（下载模型文件必需）

不填只能浏览，不能下载模型文件。

1. 浏览器打开 [civitai.com](https://civitai.com) 并登录
2. 右上角头像 → **Account settings** → **API Keys** → **Add API key**
3. 名字随便填（例如 `comfyui`），生成后复制那串 **32 位字符**
4. 回到插件 → **通道设置** → 粘贴进「Civitai API Key」→ 点 **保存设置**

Key 只保存在本机 `config.json`。

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

三种写法都支持：

```
loras                        相对 models/ 的子目录
D:\AI\MyLoras                完整路径（模型放 ComfyUI 外面时用）
%USERPROFILE%\loras          环境变量会展开
```

---

## 功能

**浏览与搜索**
- 类型、底模、排序（推荐 / 最新 / 最多下载）、时间范围筛选
- 顺序与官网一致：官方置顶块（88 个）在前，其余按点赞降序，同赞按下载量
- 搜索支持模型名、作者、标签
- 成人内容开关

**详情页**
- 多版本切换，各版本显示发布时间；按当前底模筛选自动选中对应版本
- 版本图集、触发词、文件列表、简介
- 点图看大图 + 该图的完整生成参数（提示词、采样器、步数、CFG、种子、使用的 LoRA）
- 该版本没有示例图时，会借用同模型其它版本的图并**标明来源**，不会假装是本版本的

**下载**
- 默认 64 段并发，实测约 5.8 MB/s（单流只有 40~220 KB/s）
- 下载前先查目标盘剩余空间，不够直接拒绝并说明缺多少，不会白下几个 G 再失败
- 同版本同文件重复点击不会排第二个任务
- SHA256 校验，不匹配的文件不会进入 `models/`
- 下载失败或取消时自动删除半截文件与临时分片
- 按类型分别指定保存目录

---

## 缓存与磁盘占用

```
web/covers/                   368 MB   23813 张封面（Release 附带，不在 git 里）
data/models_snapshot.json.gz   28 MB   压缩快照，随仓库分发
cache/models_snapshot.json    106 MB   运行时解压出来的快照
cache/merge_cache.json         45 MB   合并列表缓存，随用过的筛选组合增长
cache/thumbs/                  19 MB   运行时缩略图，随浏览增长
cache/tmp/                      0 MB   下载分片，完成后移走
```

首次启动把 `data/` 里的压缩快照解成 `cache/models_snapshot.json`，之后所有筛选都读它。

清理规则：

- 启动时删除所有**非核心**缩略图（默认浏览视图的封面标 `core`，永久保留）
- 核心缓存**不按时间清理**，多久没访问都留
- 启动时删除 `cache/tmp` 里超过 6 小时没被动过的下载分片（进程被关掉时那段清理代码不会执行，会留下野分片）
- 下载失败/取消的文件即时删除

想把榜单恢复成随包状态：删掉 `cache/models_snapshot.json` 重启，会从 `data/` 重新解一份。

**下载时的临时占用**：分片按成品大小写入 `cache/tmp`，同时下载数默认 2，所以峰值约等于「两个正在下载的文件大小之和」。插件目录和模型目录在同一块盘时，完成后是改名移动，不额外占一份。

---

## 数据刷新

由**第一次打开面板**触发（不是启动就触发——导入期 ComfyUI 的事件循环还不存在，任务会被丢弃），之后每 6 小时一轮：

1. 拉站上「Most Liked」前 100 页（约 10000 条，2~3 分钟），更新这些行的点赞、下载数和排名
2. 榜单里出现、本地没有的模型补进来，并抓它们的封面
3. 按（底模 × 类型）补齐覆盖不足的组合：每轮 20 个组合，每个最多 3 页 = 300 条，游标记在 `cache/base_fill_cursor.json`，下轮接着走

整个过程**只拉列表数据，不碰封面**；封面由浏览时的取图接口按需下载，本地已有就跳过。

日志写在 `cache/refresh.log`，每轮结束记一行 `refreshed: ...`，补齐记 `combinations filled: ...` 或 `thin pass: ...`。

上一轮没跑完就退出（关 ComfyUI、崩溃）时，会留一个标记文件；下次启动**跳过 90 秒延迟**直接开跑，跑完才清掉标记。

---

## 为什么快

筛选、排序、翻页走内存中的快照（约 0.002 秒），不查网络；封面命中随包索引时直接读静态文件。

下载走 `civitai.red` 拿 307 重定向到对象存储的签名地址，然后分段并发拉取。单流只有 40~220 KB/s，64 段能到 5.8 MB/s。

---

## 安全须知

**凭据会经过第三方镜像。** 下载请求带 `Authorization: Bearer <API Key>`，发往 `civitai.red` / `civitai.work`，不是 Civitai 官方域名。原因是官方域名在国内不可达，而不挂梯子就只剩镜像可选——镜像运营方能看到这个密钥。建议用专门的 API Key，方便随时在 Civitai 后台吊销。

**配置接口只接受本机请求。** `/civitai_hub/config` 会读写凭据，已加 loopback 校验，非本机来源返回 403。API Key 在响应里默认显示为 `***`，用 `_has_api_key` 标识是否已配置。如果你用 `--listen 0.0.0.0` 对外开 ComfyUI，请在反向代理上加鉴权——本插件的校验只认 TCP 来源地址，反代过来的请求会被当作本机。

**`/civitai_hub/thumb` 是通用图片代理。** 只校验 `http(s)` 前缀，没有域名白名单，请求经 `wsrv.nl` 转发。够不到内网，但可以被当作免费转发器使用，并占用本地封面缓存。

**`/civitai_hub/local` 的 folder 参数有白名单。** 只接受 `[A-Za-z0-9_-]{1,64}`，含 `..`、斜杠、盘符的取值直接 400。

---

## 已知限制

**没有断点续传。** 关掉 ComfyUI 会中断下载，已下部分留在 `cache/tmp`（文件名带任务 id，下次用不上），6 小时后被启动清理回收。重新下载从 0 开始。

**视频封面无法显示。** 部分模型用 `.mp4` 当代封面，图片代理不处理视频，`image.civitai.com` 又直连不上，这类模型显示占位图。受影响最明显的是视频类底模（Wan Video、LTXV、MiniMax、Hunyuan Video），它们的条目大多只有视频预览。插件会跳过视频、改用同一模型的第一张静态截图。

**部分类型本身没有预览图。** `Wildcards`（提示词库）、`Workflows` / `ComfyWorkflows`（工作流 json）、`MotionModule` 这些不是模型，C 站上多数条目没配图，这些类型下封面覆盖率低是正常的。

**真人/动漫无法准确区分。** 试过用标签、底模、模型名判断都不可靠：`realistic` 既指真人照片也指写实画风动漫。准确区分需要在本地跑图像分类模型，不在本插件范围内。

**「最新」有延迟。** 最新视图独立缓存 10 分钟，启动时预热，超时会重新拉一次（约 3 秒）。

**快照有边界。** 56720 条，只保证前 1 万名的顺序和站上一字不差（实测站上前 100 名里本地覆盖 99 条，共同条目零顺序反转）。更冷门的内容靠关键词搜索，搜索走实时查询。

**站上索引与版本列表可能不同步。** Civitai 会把模型算在它曾经有过的底模下，作者事后删版本的情况存在。这时筛选能筛到、点开却没有对应版本的模型，界面会明确写出「索引仍算在 X 里，但它现在没有任何 X 版本」，不会静默换成别的版本。

**涨赞与上新最多滞后 6 小时。** 想立刻刷一次，重启 ComfyUI 并打开一次面板。

---

## 通道设置里的高级项

**Civitai API 镜像** —— 默认 `civitai.red, civitai.work`，两个都挂了才需要换。填任意能访问 `/api/v1` 的反代地址。

**图片代理** —— 默认 `wsrv.nl`。它挂了才需要换，填任意支持 `?url=` 参数的公益图片代理。

**并发下载段数** —— 默认 64。网络不稳可调到 32；实测 96 段收益已趋缓。

---

## 故障排查

**面板打不开** → 看 ComfyUI 控制台有没有 `[CivitaiHub]` 开头的报错。

**图片不显示** → 「通道设置 → 检测通道」看图片代理是否可达。首次加载某张图需要 1~2 秒，之后从本地缓存读。

**下载失败** → 确认 API Key 已填且有效。错误信息显示在「下载任务」面板里。

**提示磁盘空间不够** → 按提示清理，或在「通道设置」里把该类型的保存目录改到别的盘。

**筛选结果和站内不一致** → 插件搜索时匿名请求。站内登录后服务端会返回更大的模型集合，差异来自登录态。

**改了代码不生效** → 前端改动按 `Ctrl+Shift+R`；后端改动必须重启 ComfyUI。

**快照需要重置** → 删掉 `cache/models_snapshot.json` 后重启，会从 `data/` 重新解一份。

**刷新没动静** → 打开一次面板（刷新由第一个请求触发），然后看 `cache/refresh.log` 最后一行是不是 `ranking walk failed`。

---

## 目录结构

```
ComfyUI-CivitaiHub/
├── __init__.py            节点注册
├── hub_routes.py          HTTP 接口、快照、筛选、刷新
├── hub_client.py          镜像通信、图片代理、文件解析
├── hub_downloader.py      下载任务队列、分段下载、校验
├── hub_config.py          配置读写
├── hub_providers.py       站点适配
├── selfcheck.py           网络自检
├── config.example.json    配置模板（复制成 config.json 使用）
├── data/
│   └── models_snapshot.json.gz   随包压缩快照（28 MB）
├── cache/                 运行时数据（自动生成，不提交）
└── web/
    ├── civitai_hub_v142.js    前端面板（单文件）
    └── covers/                封面图与索引（Release 附带，不在 git 里）
```

## 关键参数

都在 `hub_routes.py` 顶部：

```python
_SNAPSHOT_VERSION     = 10     结构版本，改了会自动作废旧快照重建
_REFRESH_PAGES        = 100    每轮刷新拉站上 Most Liked 的页数（约前 1 万名）
_REFRESH_INTERVAL     = 21600  刷新间隔，6 小时
_THIN_BASE_ROWS       = 300    每个（底模 × 类型）组合的目标行数
_BASE_FILL_PAGES      = 3      每个组合最多拉几页
_BASE_FILL_MAX        = 20     每轮补多少个组合（游标轮转）
_THUMB_BASE_PER_COMBO = 300    每个筛选组合预取的封面数
_THUMB_STORE_WIDTH    = 240    封面存储宽度
_THUMB_REFILL_RATIO   = 0.3    滚过已缓存的前 30% 就补下一批
_DEEP_BLOCK           = 300    从 v1 临时补的模型条数（翻到列表深处时触发）
_NEWEST_TTL           = 600    「最新」视图缓存秒数
```

`config.json` 里可调：

```json
"download_segments": 64          单文件并发段数
"max_concurrent_downloads": 2    同时下载数
"save_dirs": {"LORA": "loras"}   各类型保存目录
```

---

## 许可

**插件代码**按 [GPL-3.0](LICENSE) 发布：可以自由使用、修改、再分发，但衍生作品必须以同样的许可证开源并保留版权声明。

**模型文件、封面与元数据不属于本仓库**：版权归 Civitai 及各模型作者所有，下载和使用请遵守各模型页面标注的许可条款。本插件只做索引与转发，不对这些内容主张任何权利。
