# javbus-crawler

Docker 化的 [seedmm.bond](https://seedmm.bond)（JavBus 系 AV 磁力站）采集器，**带网页管理界面**：
在浏览器里修改全部配置、启动/停止采集、实时看日志、浏览已采集的影片与磁力链接。

界面为侧边栏菜单布局（仪表盘 / 采集数据 / 采集设置 / 关于），桌面与移动端自适应。

## 快速开始

```bash
# 保存 docker-compose.yml 后一条命令启动：
docker compose up -d
```

打开 **http://localhost:7878**，**使用 compose 里设置的管理员账号登录**（`ADMIN_USER` / `ADMIN_PASSWORD`，见 compose 注释）：

- **登录保护**：所有页面与 API 均需登录；会话 7 天有效；失败有防爆破延迟；
  不设置 `ADMIN_PASSWORD` 时每次启动生成随机密码（`docker logs javbus-crawler` 查看）
- **采集设置**：站点地址、请求间隔、随机抖动、重试次数、超时、端口——全部在网页表单里改，
  保存即生效（持久化在 `./data/config.json`，业务配置不使用环境变量）
- **采集控制**：填页码（如 `1` 或 `2-10`）、勾选“抓磁力 / 刷新已有”，点“开始采集”，
  实时滚动日志与统计（新增/更新/磁力/错误），随时可停止
- **采集数据**：分页 + 搜索（番号/标题/演员），点击行展开详情与全部磁力链接
- **在线更新**：自动检测 GitHub 新版本，网页内一键更新

## 项目结构

```
├── docker-compose.yml      # 常驻 Web 服务, 端口 7878
├── Dockerfile
├── app/
│   ├── main.py             # 入口：默认启动 Web（serve）；crawl 子命令保留 CLI 采集
│   ├── web.py              # Flask API：配置/采集控制/状态日志/数据查询/更新检查
│   ├── crawler.py          # 采集引擎（CLI 与 Web 共用，支持协作式停止）
│   ├── fetcher.py          # HTTP 层：限速、抖动、重试退避
│   ├── parser.py           # 列表页/详情页/磁力解析
│   ├── db.py               # SQLite schema、upsert、查询
│   ├── settings.py         # JSON 配置文件（/data/config.json）
│   ├── updater.py          # GitHub Releases 在线更新
│   └── templates/index.html# 单页前端
└── data/                   # 挂载卷：seedmm.db（数据）+ config.json（配置）
```

## 配置

全部配置在网页“采集设置”卡片中修改，保存后立即生效，存储于 `data/config.json`：

| 键 | 默认值 | 说明 |
|---|---|---|
| `BASE_URL` | `https://www.seedmm.bond` | 站点根地址 |
| `CATEGORY` | `censored` | 采集频道：有码 `censored` / 无码 `uncensored` |
| `PROXY` | 空 | HTTP 代理（如 `http://127.0.0.1:7890`），留空不使用 |
| `DELAY_SECONDS` | `2.0` | 请求间隔下限（秒） |
| `JITTER_SECONDS` | `1.0` | 请求间隔随机抖动上限（秒） |
| `MAX_RETRIES` | `3` | 单请求重试次数（指数退避） |
| `TIMEOUT` | `30` | 请求超时（秒） |
| `USER_AGENT` | Chrome UA | 请求头 |
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
| POST | `/api/crawl/start` | `{"pages":"1-5","magnets":true,"refresh":false}` 启动采集 |
| POST | `/api/crawl/stop` | 停止当前采集 |
| GET | `/api/crawl/status` | 运行状态、统计、日志尾部 |
| GET | `/api/movies?q=&page=&size=` | 已采集影片分页/搜索 |
| GET | `/api/movies/<番号>` | 影片详情 + 磁力列表 |
| GET | `/api/update/check` | 检查新版本 |
| POST | `/api/update/apply` | 返回容器场景的更新命令 |

## 数据表

- `movies` — 番号(主键)、标题、封面、发行日期、时长、导演、制作商、发行商、系列、
  演员(JSON)、类别(JSON)、样图(JSON)、磁力数、首次/最近采集时间
- `magnets` — btih 哈希(主键)、所属番号、名称、大小、分享日期、完整 magnet 链接

## 实现要点

- 磁力链接不在详情页 HTML 中：站点由 `js/gallery.js` 通过
  `GET /ajax/uncledatoolsbyajax.php?gid=<gid>&lang=zh&img=<img>&uc=<uc>&floor=<rand>`
  动态填充 `#magnet-table`；`gid/uc/img` 从详情页内联脚本提取。
- 限速默认 2-3 秒/请求，礼貌抓取；404 跳过，网络错误指数退避重试。
- 断点续采：按番号幂等 upsert，重复运行不产生重复行。
- 发版流程：改 `app/version.py` → push → `gh release create vX.Y.Z`，
  Actions 自动构建并推送 `ghcr.io/daisyyijin/javbus-crawler:{latest,vX.Y.Z}`，
  旧版本网页自动提示更新。

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
