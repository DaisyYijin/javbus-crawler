# seedmm-crawler

Docker 化的 [seedmm.bond](https://seedmm.bond)（JavBus 系 AV 磁力站）采集器：
遍历列表页 → 抓取影片详情页元数据 → 抓取磁力链接 → 存入 SQLite。

## 项目结构

```
.
├── Dockerfile              # python:3.12-slim, 非 root 用户
├── docker-compose.yml      # 环境变量 + ./data 卷挂载
├── requirements.txt        # requests, beautifulsoup4
├── app/
│   ├── main.py             # CLI 入口与采集流程
│   ├── fetcher.py          # HTTP 层：限速、随机抖动、重试退避
│   ├── parser.py           # 列表页/详情页/磁力片段解析
│   ├── db.py               # SQLite schema 与幂等 upsert
│   └── settings.py         # 环境变量配置
└── data/                   # 采集输出（挂载卷, 自动创建）
    └── seedmm.db
```

## 使用

```bash
# 采集第 1 页（每页 30 部影片）
docker compose up

# 采集第 2-10 页
docker compose run --rm crawler --pages 2-10

# 增量重跑（已在库中的番号自动跳过）
docker compose run --rm crawler --pages 1-10

# 只抓元数据不抓磁力 / 调整限速
docker compose run --rm crawler --pages 1 --no-magnets
docker compose run --rm crawler --pages 1 --delay 5
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEEDMM_BASE` | `https://www.seedmm.bond` | 站点根地址 |
| `DB_PATH` | `/data/seedmm.db` | SQLite 路径 |
| `DELAY_SECONDS` | `2.0` | 请求间隔下限（秒） |
| `JITTER_SECONDS` | `1.0` | 请求间隔随机抖动上限 |
| `MAX_RETRIES` | `3` | 单请求重试次数 |
| `TIMEOUT` | `30` | 请求超时（秒） |

## 数据表

- `movies` — 番号(主键)、标题、封面、发行日期、时长、导演、制作商、发行商、系列、
  演员(JSON)、类别(JSON)、样图(JSON)、磁力数、首次/最近采集时间
- `magnets` — btih 哈希(主键)、所属番号、名称、大小、分享日期、完整 magnet 链接

查询示例：

```bash
docker run --rm -v "$(pwd)/data:/data" alpine \
  sqlite3 /data/seedmm.db "SELECT code, title FROM movies ORDER BY first_seen DESC LIMIT 5;"
```

## 实现要点

- 磁力链接不在详情页 HTML 中：站点由 `js/gallery.js` 通过
  `GET /ajax/uncledatoolsbyajax.php?gid=<gid>&lang=zh&img=<img>&uc=<uc>&floor=<rand>`
  动态填充 `#magnet-table`；`gid/uc/img` 从详情页内联脚本提取。
- 限速默认 2-3 秒/请求，礼貌抓取；404 跳过，网络错误指数退避重试。
- 断点续采：按番号幂等 upsert，重复运行不产生重复行。

## 在线更新

程序通过 GitHub Releases 检测新版本（仅标准库，无额外依赖）：

```bash
# 采集时自动检查；发现新版会在启动时提醒
docker compose run --rm crawler --pages 1

# 手动检查并查看更新日志
docker compose run --rm crawler --check-update

# 查看更新日志并执行更新（仅在宿主机源码目录下运行有效）
python -m app.main --update
```

- 容器内运行 `--update` 只会提示需要在宿主机执行的命令（容器无法重建自身镜像）：
  `git pull && docker compose build`，或直接 `docker pull ghcr.io/daisyyijin/javbus-crawler:latest`
- 离线环境 / CI：加 `--no-check-update` 跳过启动检查
- 更新源可用 `UPDATE_REPO` 环境变量覆盖（默认 `DaisyYijin/javbus-crawler`）

## 注意

- 请遵守目标站点的服务条款与当地法律，控制采集频率，数据仅供个人学习研究。
