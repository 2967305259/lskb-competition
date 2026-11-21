# 冷水坑杯 #3 比赛管理系统

> 本文与 [复用指南](docs/复用指南.md) 记录当前可操作功能。改造范围、验收记录和待办见 [改造最终参考](docs/改造最终参考.md)。[初版需求 00–19](冷水坑杯3-项目需求文档-初版/00-项目概述.md) 留作历史设计资料。


《明日方舟》冷水坑杯 #3 赛事管理系统是一个基于 Flask + SQLite/MySQL 的 Web 应用，用于管理赛事报名、队伍创建、比赛匹配、积分计算等全流程。

## 功能概览

### 用户系统
- ✅ 用户注册与登录（密码强度验证：8位+字母+数字）
- ✅ 四级角色：超级管理员、管理员、队长、选手
- ✅ 密码哈希存储、修改和重置
- ✅ 登录会话管理（24小时超时）
- ✅ 注册后直接可用（无审批环节）
- ✅ 新手引导系统

### 队伍系统
- ✅ 队长创建队伍
- ✅ 队伍人数可配置，默认 4 人（含队长）
- ✅ 队伍名称、口号、照片管理
- ✅ 队长邀请选手 + 选手主动申请加入（双向）
- ✅ 位置分配：冷水位、普通位①、普通位②、坑位

### 比赛系统
- ✅ 正式排赛支持同赛道、跨队自动匹配，以及后台手动排赛
- ✅ 比赛时间协商与确认
- ✅ 双方独立难度选择
- ✅ 比赛结果提交与管理员裁定（修改留痕 ScoreLog）
- ✅ 弃权机制
- ✅ 选手间留言沟通

### 积分系统
- ✅ 管理员确认整场结算后计分，计算 `原始成绩 × 难度倍率` 后向零取整
- ✅ 难度倍率 = 1 + (难度 - 肉鸽最低难度) × 难度阶级
- ✅ 每个肉鸽独立配置难度范围与倍率
- ✅ 队伍积分 = 队员比赛积分及有效人工调整之和 × 当届队伍倍率
- ✅ 当前赛季积分重建（往届只允许超管专项纠错）

### 肉鸽系统
- ✅ 赛道可以新增、编辑、停用，未被引用时可删除
- ✅ 每队人数上限控制
- ✅ 独立难度配置（最低/最高/阶级）

### 排行榜
- ✅ 队伍排行榜按总分排名，并列占位跳号
- ✅ 晋级线同分队伍全部晋级
- ✅ 赛事结果展示（冠亚季军）
- ✅ 往届赛事归档（`/history`：历届积分榜、队员积分榜、比赛结果，只读）

### 管理后台
- ✅ 仪表盘（赛事统计概览）
- ✅ 用户管理（角色升降级、重置密码、删除）
- ✅ 队伍管理（查看、删除、积分修改）
- ✅ 比赛管理（结果修改、审核、成绩日志）
- ✅ 当届轮播管理（支持图片、视频、链接与排序，条目为空时读取 `IMAGE_DIR`，有条目但全停用时隐藏）
- ✅ 晋级名额和当届队伍倍率配置
- ✅ 报名控制（开放/关闭）
- ✅ 赛事流程控制（报名→匹配→比赛→结束）
- ✅ 当届 Excel 导出（报名、比赛结果、队伍积分与队员积分）
- ✅ 操作审计日志（SystemLog）

### 前端设计
- ✅ Bootstrap 5 响应式设计
- ✅ 深色主题（明日方舟风格）
- ✅ 轮播图组件（淡入淡出 + 触摸滑动）
- ✅ PC 端 + 移动端适配
- ✅ Font Awesome 图标库

## 系统需求

- Python 3.12（本项目验证版本）
- pip 包管理器
- （可选）MySQL 8 用于生产环境

## 快速启动

原生启动前先把实例配置导出到进程环境，包括初始 `ADMIN_PASSWORD`。同一组配置用于准备和启动。开发默认使用 SQLite，监听端口默认 `19198`。

```bash
python -m pip install -r requirements.txt
export FLASK_ENV=development FLASK_SKIP_DOTENV=1
python -m flask --app 'app:create_app()' prepare
python run.py
```

| 用途 | 入口 |
|---|---|
| 初始化、迁移和空库播种 | `python -m flask --app 'app:create_app()' prepare` |
| 原生开发 | `python run.py`，`python main.py` 为兼容转发，`python app.py` 为兼容直跑入口 |
| 原生生产 | 设置 `FLASK_ENV=production`，导出 `SECRET_KEY` 和 `ADMIN_PASSWORD`。执行 `prepare` 后，由 Gunicorn 加载 `wsgi:app` |
| Compose 生产 | 从 [.env.example](.env.example) 填写实例配置后使用对应的 Compose 文件。入口脚本在启动 worker 前执行一次 `prepare` |

应用导入、工厂和 WSGI worker 不建表、不迁移、不播种。原生生产使用 `python -m pip install gunicorn==21.2.0` 安装 Gunicorn，再运行 `gunicorn --bind "0.0.0.0:${PORT:-19198}" --workers 4 wsgi:app`。部署、升级和恢复步骤见 [复用指南](docs/复用指南.md#原生和-compose-部署)。

## 环境变量配置（复用到新赛事）

办新一届或其他赛事时，无需改代码，设置环境变量即可：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EVENT_NAME` | `冷水坑杯` | 赛事短名 |
| `SEASON_NUMBER` | `3` | 赛季届数 |
| `TOURNAMENT_NAME` | `{EVENT_NAME} #{SEASON_NUMBER}` | 完整赛事名（页面标题/导航栏/导出文件名） |
| `TEAM_SIZE` | `4` | 队伍满员人数（含队长） |
| `TEAM_POSITIONS` | `cold,normal1,normal2,pit` | 队内位置列表，逗号分隔 |
| `TRACK_LABEL` | `肉鸽` | 赛道/玩法的页面文案 |
| `MEMBER_FIELD1_LABEL` | `分队` | 队员自定义字段① 的页面文案 |
| `MEMBER_FIELD2_LABEL` | `开局干员` | 队员自定义字段② 的页面文案 |
| `SCHEDULE_SLOT_TIMES` | `14:00,19:00` | 手动建赛的时段，逗号分隔（支持 3 段以上） |
| `SCHEDULE_SLOT_MINUTES` | `120` | 每段比赛时长（分钟） |
| `DEFAULT_BONUS_SCORE` | `500` | 队伍积分加成倍数默认值 |
| `DEFAULT_DIFFICULTY_MIN` / `_MAX` / `_STEP` | `12` / `18` / `0.1` | 新建赛道的默认难度区间与倍率阶级 |
| `BRAND_PRIMARY_COLOR` | `#0d6efd` | 品牌主色（导航/按钮/卡片边框跟随） |
| `PORT` | `19198` | 监听端口 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / 空 | 空赛事库执行 `prepare` 时创建初始管理员，生产入口要求显式设置口令 |
| `SUPER_ADMIN_USERNAME` / `SUPER_ADMIN_PASSWORD` | 空（不创建） | 初始超级管理员 |
| `DATABASE_URL` | SQLite | 生产环境 MySQL 连接串 |
| `SECRET_KEY` | 开发应用工厂生成临时随机密钥 | 生产入口要求显式设置随机会话密钥 |

原生部署可通过 `DB_FILE`、`UPLOAD_ROOT` 和 `IMAGE_DIR` 指定存储位置。Compose 使用独立上传卷，宿主机轮播素材可通过 `IMAGE_HOST_DIR` 指定。目录默认值、上传去向及多实例配置见[复用指南的素材与上传目录](docs/复用指南.md#步骤-3素材与上传目录)。

> 赛事名称、队伍满员人数（含队长）也可以在后台「赛事设置」页面直接修改，即时生效，优先级高于环境变量；该页另有「允许未满员开赛」开关，勾选后开赛不再校验队伍满员。
>
> 该页底部还有「开启新赛季」：当前赛事转入归档（队伍、比赛、成绩全部保留可查），新赛季重新报名。**所有用户账号保留**，但参赛进度归零、需要重新建队；上届未处理的邀请/申请会自动清理。队名与队长的唯一性按赛事隔离，同一队长、同一队名可在不同赛季重复使用；首页统计、排行榜、报名列表、队伍搜索、赛事大厅、日程与抽签等页面只展示当前赛季数据。
>
> 换赛事 / 换赛季的完整操作清单、配置优先级、必须手改的写死项与已知限制，见 [docs/复用指南.md](docs/复用指南.md)。

选手资料和「我的比赛」默认显示本届正式比赛，可切换至累计范围。往届结果和规则通过归档按届查询。[战绩与归档](docs/复用指南.md#63-往届赛事归档页)

## 初始账号与登录

显式执行 `prepare` 时，空赛事库按 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 创建初始管理员。若需要初始超级管理员，同时设置 `SUPER_ADMIN_USERNAME` 和 `SUPER_ADMIN_PASSWORD`。

`wsgi:app` 默认使用生产配置，生产服务和两套 Compose 均要求提供 `SECRET_KEY` 和 `ADMIN_PASSWORD`。开发环境缺少密钥时会记录告警并使用临时随机密钥，服务重启后需重新登录。已知默认配置会触发告警，仍在使用旧版默认口令或重置口令的管理员会在后台看到改密提示。

已有账号继续使用数据库中保存的密码并正常进入登录后页面。修改部署环境变量只影响初始创建配置。升级到数据库结构版本 4 前，先停止旧服务，使用同一数据库和存储路径执行 `prepare` 完成备份与迁移。

## 项目结构

```
├── app.py                   # 单文件后端（配置/模型/表单/路由/工厂）
├── run.py                   # 启动脚本（依赖清单来自 requirements.txt）
├── main.py                  # 转发入口（等价 python run.py，兼容旧脚本）
├── wsgi.py                  # 已准备数据库的生产 WSGI 入口
├── .env.example             # 赛事复用配置模板（复制为 .env）
├── app/
│   ├── templates/           # Jinja2 模板
│   │   ├── _macros.html     # 公共宏（分页 / 状态徽章）
│   │   ├── admin/           # 管理后台模板
│   │   ├── auth/            # 认证模板
│   │   ├── captain/         # 队长模板
│   │   ├── history/         # 往届赛事归档
│   │   ├── player/          # 选手模板
│   │   └── tournament/      # 赛事模板
│   └── static/              # 静态文件（CSS/JS/上传文件）
│       └── css/theme.css    # 主题样式（原 base.html 内联样式）
├── image/                   # 轮播图片/视频
├── docs/                    # 复用指南（换赛事操作手册）
├── .db/                     # SQLite 默认数据库目录，由 prepare 创建
├── requirements.txt         # 依赖清单
└── README.md
```

## 数据库

原生默认使用 SQLite（`.db/lskb.db`）。MySQL 可用 `DB_BACKEND=mysql` 和独立连接字段配置，显式 `DATABASE_URL` 优先。两种数据库均由 `prepare` 显式准备和迁移。

分别使用 `docker-compose.sqlite.yml` 或 `docker-compose.yml` 部署，按实例设置项目名、端口和素材路径。运行后可用 `deployment-info` 核对实际数据库、目录和版本。 [原生和 Compose 部署示例](docs/复用指南.md#原生和-compose-部署)

## 用户指南

### 选手流程
1. 注册账号 → 2. 接受邀请/主动申请 → 3. 选择肉鸽 → 4. 填写宣言 → 5. 设置可用时间 → 6. 参加比赛

### 队长流程
1. 注册并升级为队长 → 2. 创建队伍 → 3. 邀请选手/审核申请 → 4. 分配位置 → 5. 选择肉鸽和宣言 → 6. 参加比赛

### 管理员流程
1. 登录后台 → 2. 管理赛事流程 → 3. 裁定比赛结果 → 4. 导出数据

正式比赛由后台「执行匹配」或「日程管理」写入。后台「分组抽取器」读取已创建的比赛，在浏览器随机分组供现场展示。`抽取/` 是需要手动更新选手 JSON、赛道映射及头像的独立工具。[排赛与抽签工具](docs/复用指南.md#64-排赛与抽签工具)

## 安全特性

- ✅ Flask-Login 用户认证
- ✅ 四级 RBAC 权限控制
- ✅ CSRF 防护（Flask-WTF）
- ✅ 密码加密存储（Werkzeug）
- ✅ SQL 注入防护（SQLAlchemy ORM）
- ✅ 密码强度验证（8位+字母+数字）
- ✅ 操作审计日志（SystemLog）
- ✅ 路径穿越防护

## 故障排除

### 数据库连接失败
- 检查 MySQL 服务是否运行
- 检查连接字符串中的用户名、密码、主机名
- 确保数据库已创建

### 导入包错误
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 模板错误
- 检查 `app/templates` 目录是否存在
- 检查模板文件名是否正确

## 测试

在仓库根目录安装开发依赖。日常回归使用 SQLite 临时文件，需要数据库的用例各自复制数据库，并使用自己的上传、素材和备份目录。

```bash
python -m pip install -r requirements-dev.txt
env -u P0_MYSQL_URL python -m pytest -q --disable-warnings -m 'not maintenance'
```

完整 SQLite 回归包含旧库迁移、跨进程准备、备份和恢复。单独执行这些用例时可加 `-m maintenance`。

```bash
env -u P0_MYSQL_URL python -m pytest -q --disable-warnings
```

MySQL 回归需要本地 Docker。脚本创建一个临时 MySQL 8 容器，默认分配本机端口，每个用例在容器里创建独立数据库，退出时清理容器。

```bash
PYTHON=python tests/run_mysql.sh -k mysql
```

容器持久化验证使用 `tests/container_smoke.py`，输入为本地测试镜像。验收结果见 [改造最终参考](docs/改造最终参考.md#6-p3-配置部署与运维)。

两套 Compose 的端到端验证会创建并清理独立测试项目和卷。空间有限时可分别传入 `--backend sqlite` 和 `--backend mysql`。

```bash
docker build -t lskb-p3-7-test:local .
python tests/compose_smoke.py lskb-p3-7-test:local
```

## 开发建议

- 使用 `FLASK_ENV=development` 启用调试模式
- 定期备份数据库
- 生产环境配置独立的随机 `SECRET_KEY`
- 使用环境变量管理敏感配置

## 许可证

Copyright © 2026 冷水坑杯 #3 赛事组委会

---

有问题或建议？请联系管理员。
"# lskb-competition" 

