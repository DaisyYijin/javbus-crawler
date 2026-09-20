# javbus-crawler

Docker 化的 [seedmm.bond](https://seedmm.bond)（JavBus 系 AV 磁力站）采集器，**带网页管理界面**：
在浏览器里修改全部配置、启动/停止采集、实时看日志、浏览已采集的影片与磁力链接。

界面为侧边栏菜单布局（仪表盘 / 采集设置 / 定时任务 / 采集数据 / 维度浏览 / 关于），桌面与移动端自适应。

## 快速开始

```bash
# 保存 docker-compose.yml 后一条命令启动：
docker compose up -d
```

打开 **http://localhost:7878**，**使用 compose 里设置的管理员账号登录**（`ADMIN_USER` / `ADMIN_PASSWORD`，见 compose 注释）：

- **登录保护**：所有页面与 API 均需登录；会话 7 天有效；失败有防爆破延迟；
  不设置 `ADMIN_PASSWORD` 时每次启动生成随机密码（`docker logs javbus-crawler` 查看）
- **采集设置**：站点地址、有码/无码多选、请求间隔、随机抖动、重试次数、超时、端口——
  全部在网页表单里改，保存即生效（持久化在 `./data/config.json`）
- **采集控制**：追新 / 补历史两种模式、页码范围（单次上限 500 页）、抓磁力、刷新已有，
  实时滚动日志与统计，随时可停止；支持定时自动采集；支持按站点类别（有码/无码 × 类别）精准采集
- **补采番号**：数据页输入番号（如 `BANK-248`）单独抓取一部影片，漏采补录
- **采集数据**：分页 + 搜索（番号/标题/演员）+ 高级筛选（按演员/片商/发行商/系列/导演/标签
  精确过滤 + 发行日期区间），点击行展开详情与全部磁力链接，可删除单片
- **维度浏览**：按演员 / 片商 / 发行商 / 系列 / 导演聚合排行，点击名称直接筛选影片
- **精准筛选**：关键词（顺序即优先级、大小写不敏感）匹配标签或磁力名，推荐磁力或只收命中的影片；
  常用标记快捷标签按库内磁力真实命中数动态生成，采集后自动更新
- **MetaTube 联动**：配置服务地址 + Token，详情页一键跳转查询
- **115 网盘**：扫码登录（可选登录设备）、影片一键发送磁力离线下载、任务监控、
  下载完成后自动重命名「番号 标题」并移动到目标目录（基于 [p115client](https://github.com/ChenyangGao/p115client)）
- **导出 / 清空**：全库 JSON 流式导出；一键清空数据（任务运行时拒绝）
- **在线更新**：自动检测 GitHub 新版本，网页内一键更新

## 项目结构

```
├── docker-compose.yml      # 常驻 Web 服务, 端口 7878
├── Dockerfile
├── app/
│   ├── main.py             # 入口：默认启动 Web（serve）；crawl 子命令保留 CLI 采集
│   ├── web.py              # Flask API：配置/采集控制/状态日志/数据查询/更新检查
│   ├── crawler.py          # 采集引擎（CLI 与 Web 共用，支持协作式停止、单番号补采）
│   ├── fetcher.py          # HTTP 层：限速、抖动、重试退避
│   ├── parser.py           # 列表页/详情页/磁力解析
│   ├── db.py               # SQLite schema、索引、upsert、多维查询
│   ├── settings.py         # JSON 配置文件（/data/config.json）
│   ├── taxonomy.py         # 标签归一化（统计页标准分类）
│   ├── p115.py             # 115 网盘：扫码登录 / 磁力离线 / 任务整理
│   ├── updater.py          # GitHub Releases 在线更新
│   ├── selfupdate.py       # docker.sock 容器自更新
│   └── templates/          # 单页前端（index.html / login.html）
└── tests/                  # pytest 测试套件（CI 自动运行）
```

## 配置

全部配置在网页“采集设置”卡片中修改，保存后立即生效，存储于 `data/config.json`：

| 键 | 默认值 | 说明 |
|---|---|---|
| `BASE_URL` | `https://www.seedmm.bond` | 站点根地址 |
| `CATEGORY` | `censored` | 采集频道：有码 `censored` / 无码 `uncensored`，可逗号分隔同时采 |
| `GENRE` | 空 | 类别筛选：站点类别 ID/slug（如 `42`、`hd`），逗号分隔多个，按「频道 × 类别」组合精准采集；留空采全部 |
| `PROXY` | 空 | HTTP 代理（如 `http://127.0.0.1:7890`），留空不使用 |
| `DELAY_SECONDS` | `2.0` | 请求间隔下限（秒） |
| `JITTER_SECONDS` | `1.0` | 请求间隔随机抖动上限（秒） |
| `MAX_RETRIES` | `3` | 单请求重试次数（指数退避） |
| `TIMEOUT` | `30` | 请求超时（秒） |
| `TAG_FILTERS` | 空 | 筛选关键词（逗号分隔，顺序即优先级，大小写不敏感；站点常用：字幕 / 高清 / -U 无码流出 / -UC 无码中字 / AI） |
| `TAG_FILTER_MODE` | `mark` | `mark` 全部入库并标记 / `only` 只收命中的 |
| `METATUBE_URL` / `METATUBE_TOKEN` | 空 | MetaTube 服务地址与 Token（详情页跳转用） |
| `P115_DOWNLOAD_DIR` | `待整理` | 115 离线下载落盘目录（留空用 115 默认） |
| `P115_TARGET_DIR` | `已整理` | 115 整理目标目录（完成后重命名并移动到这里） |
| `P115_AUTO_ORGANIZE` | `false` | 自动整理：每分钟检查完成并重命名+移动 |
| `AUTO_CRAWL_ENABLED` | `false` | 定时自动采集开关 |
| `AUTO_CRAWL_INTERVAL_HOURS` | `24` | 自动采集间隔（小时） |
| `AUTO_CRAWL_PAGES` | `1-3` | 每次自动采集的页码范围 |
| `WEB_PORT` | `7878` | Web 端口（重启容器后生效） |

## CLI（可选）

```bash
# 一次性命令行采集（配置来自网页/配置文件）
docker compose run --rm crawler crawl --pages 1-5
docker compose run --rm crawler crawl --pages 1 --no-magnets

# 更新检查
docker compose run --rm crawler --check-update
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/config` | 读取 / 保存配置 |
| POST | `/api/crawl/start` | `{"pages":"1-5","mode":"new|backfill",...}` 启动采集（单次 ≤500 页） |
| POST | `/api/crawl/stop` | 停止当前采集 |
| GET | `/api/crawl/status` | 运行状态、统计、日志尾部 |
| POST | `/api/crawl/code` | `{"code":"BANK-248"}` 补采单个番号 |
| GET | `/api/movies` | 影片分页/搜索；支持 `actor/studio/label/series/director/genre` 精确筛选与 `date_from/date_to` |
| GET | `/api/movies/<番号>` | 影片详情 + 磁力列表 + 推荐磁力 |
| DELETE | `/api/movies/<番号>` | 删除单片及其磁力 |
| GET | `/api/browse?type=actor|studio|label|series|director` | 维度聚合排行 |
| GET | `/api/stats` | 库存统计与热门标签（30s 缓存） |
| GET | `/api/export` | 全库 JSON 流式导出 |
| POST | `/api/data/clear` | 清空数据（采集运行中返回 409） |
| POST | `/api/metatube/test` | 测试 MetaTube 服务连通性 |
| POST | `/api/site/test` | 测试站点地址连通性（返回耗时 / 拦截提示） |
| GET | `/api/filter/chips` | 快捷标签（按库内磁力命中的标记及数量） |
| GET/POST | `/api/p115/status` · `/api/p115/qr/start` · `/api/p115/qr/poll` | 115 登录状态 / 扫码登录 |
| POST | `/api/p115/magnet` | 影片最优磁力发送到 115 离线下载 |
| GET | `/api/p115/tasks` | 115 离线任务列表（监控） |
| POST | `/api/p115/organize` | 立即整理完成的任务（重命名+移动） |
| GET | `/api/update/check` · POST `/api/update/apply` | 检查 / 一键更新 |

## 数据表

- `movies` — 番号(主键)、标题、封面、发行日期、时长、导演、制作商、发行商、系列、
  演员(JSON)、类别(JSON)、样图(JSON)、磁力数、首次/最近采集时间
- `magnets` — btih 哈希(主键)、所属番号、名称、大小、分享日期、完整 magnet 链接

## 实现要点

- 磁力链接不在详情页 HTML 中：站点由 `js/gallery.js` 通过
  `GET /ajax/uncledatoolsbyajax.php?gid=<gid>&lang=zh&img=<img>&uc=<uc>&floor=<rand>`
  动态填充 `#magnet-table`；`gid/uc/img` 从详情页内联脚本提取。
- 限速默认 2-3 秒/请求，礼貌抓取；404 跳过，网络错误指数退避重试。
- 断点续采：按番号幂等 upsert，重复运行不产生重复行；补历史按频道独立记录深度。
- movies 表建有 category / first_seen / magnet_count / matched_count / release_date 索引。
- 发版流程：改 `app/version.py` → push → `gh release create vX.Y.Z`，
  Actions 自动构建并推送 `ghcr.io/daisyyijin/javbus-crawler:{latest,vX.Y.Z}`，
  旧版本网页自动提示更新。

## 测试

```bash
pip install -r requirements.txt pytest
pytest -q          # 63 项离线测试（不需要网络与数据库）
```

CI（`.github/workflows/tests.yml`）在每次 push / PR 时自动运行。

## 注意

- 请遵守目标站点的服务条款与当地法律，控制采集频率，数据仅供个人学习研究。
- 从 v0.3.0 升级的用户：若 `data/config.json` 中保存过 `WEB_PORT: 8000`，请在网页中改为 `7878`
  并更新 compose 的端口映射（或直接删除 `config.json` 重新生成）。

## 网页一键更新（v0.5.0+）

「关于 / 更新」页的 **🚀 一键更新** 可在网页内直接完成：拉取最新镜像 → 重建同名容器
（端口/卷/重启策略全部保留）→ 新容器接管 → 自动清理旧容器 → 页面自动刷新。
失败时自动回滚，旧容器继续运行。

前提：`docker-compose.yml` 中挂载了 `/var/run/docker.sock`（默认已配置）。
安全提示：挂载 docker.sock 等于授予容器宿主机 Docker 的完全控制权，仅在可信环境使用；
不需要此功能可删除该挂载行，网页会退回显示手动更新命令。

## 故障排查

**`sqlite3.OperationalError: unable to open database file` 或 `Permission denied: /data/...`**

容器内以 `nobody` 用户运行，而宿主机挂载的 `./data` 目录属主不对（常见于 Linux 上由 root 自动创建）。
v0.4.1 起容器启动会自动修正属主；旧版本请手动执行：

```bash
sudo chown -R 65534:65534 ./data && docker compose restart
```
