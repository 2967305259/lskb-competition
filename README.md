# 冷水坑杯 #3 报名管理系统

《明日方舟》冷水坑杯 #3 赛事报名管理系统是一个基于 Flask + MySQL 的 Web 应用，用于管理赛事报名、队伍创建、队员邀请等功能。

## 功能概览

### 用户系统
- ✅ 用户注册与登录
- ✅ 三种角色：管理员、队长、选手
- ✅ 密码加密存储与修改
- ✅ 登录会话管理

### 队伍系统
- ✅ 队长创建队伍
- ✅ 4人队伍结构（队长1人 + 队员3人）
- ✅ 队伍名称和宣言管理
- ✅ 队伍成员邀请系统

### 邀请系统
- ✅ 队长搜索并邀请选手
- ✅ 选手接受/拒绝邀请
- ✅ 邀请状态管理（待处理、已接受、已拒绝）

### 位置系统
- ✅ 4个位置分配：冷水位、普通位①、普通位②、坑位
- ✅ 队长为队员分配位置
- ✅ 每个位置只能一人

### 肉鸽选择
- ✅ 队员选择肉鸽副本：水月、探索者、萨卡兹、岁的界园
- ✅ 每位队员只能选一个

### 参赛宣言
- ✅ 队员填写参赛宣言（最多200字）
- ✅ 公开展示

### 管理后台
- ✅ 用户管理（删除、角色调整）
- ✅ 队伍管理（查看、删除）
- ✅ 报名状态控制（开启/关闭报名）
- ✅ Excel 报名表导出
- ✅ 数据统计与监控

### 前端设计
- ✅ Bootstrap 5 响应式设计
- ✅ 深色主题（明日方舟风格）
- ✅ PC 端 + 移动端适配
- ✅ Font Awesome 图标库

## 系统需求

- Python 3.12
- MySQL 8
- pip 包管理器

## 依赖安装

```bash
pip install -r requirements.txt
```

## 配置数据库

默认使用 SQLite（无需额外配置），数据库文件自动创建在 `.db/lskb.db`。

如需使用 MySQL，编辑 `app.py` 中的 `Config` 类：

```python
SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://username:password@localhost:3306/lskb_signup?charset=utf8mb4'
```

或通过环境变量：

```bash
set DATABASE_URL=mysql+pymysql://username:password@localhost:3306/lskb_signup?charset=utf8mb4
```

## 初始化数据库

首次运行自动创建所有表，无需手动操作：

```bash
python app.py
```

如需手动重置数据库，删除 `.db/lskb.db` 后重新运行即可。

## 运行项目

```bash
python app.py
```

应用将在 `http://localhost:19198` 启动

## 默认管理员账号

系统首次启动时自动创建：

- **用户名**: `admin`
- **密码**: `admin123`

> ⚠️ **重要**: 首次登录必须修改密码！

## 项目结构

```
├── app.py                   # 单文件后端（配置/模型/表单/路由/工厂）
├── app/
│   ├── templates/           # Jinja2 模板
│   └── static/              # 静态文件（CSS/JS/图片）
├── image/                   # 轮播图片/视频
├── .db/                     # SQLite 数据库（自动创建）
├── requirements.txt         # 依赖清单
└── README.md
```

> 所有后端代码（配置、数据模型、表单、路由、蓝图、应用工厂）均整合在 `app.py` 中，便于 Docker 打包部署。

## 数据库模型

### users 表
```sql
- id: 主键
- username: 唯一用户名 (3-20字符)
- nickname: 游戏昵称 (2-20字符)
- password_hash: 加密密码
- role: 角色 (admin/captain/player)
- password_changed: 是否修改过密码
- created_at: 创建时间
- updated_at: 更新时间
```

### teams 表
```sql
- id: 主键
- team_name: 队伍名称
- team_slogan: 队伍宣言
- captain_id: 队长ID (外键)
- created_at: 创建时间
- updated_at: 更新时间
```

### team_members 表
```sql
- id: 主键
- team_id: 队伍ID (外键)
- user_id: 用户ID (外键)
- position: 位置 (cold/normal1/normal2/pit)
- roguelike: 肉鸽选择 (water/sami/sarkaz/garden)
- declaration: 参赛宣言
- joined_at: 加入时间
- updated_at: 更新时间
```

### invitations 表
```sql
- id: 主键
- team_id: 队伍ID (外键)
- user_id: 用户ID (外键)
- status: 邀请状态 (pending/accepted/rejected)
- created_at: 创建时间
- updated_at: 更新时间
```

### settings 表
```sql
- id: 主键
- registration_open: 报名是否开放
- updated_at: 更新时间
```

## 用户指南

### 选手流程
1. 注册账号 → 2. 接受队长邀请 → 3. 选择肉鸽 → 4. 填写宣言 → 5. 查看队伍

### 队长流程
1. 升级为队长 → 2. 创建队伍 → 3. 邀请选手 → 4. 分配位置 → 5. 查看队伍

### 管理员流程
1. 登录后台 → 2. 管理用户/队伍 → 3. 控制报名状态 → 4. 导出 Excel

## Excel 导出

管理员可以在后台导出报名信息 Excel 文件，包含：
- 队伍名称
- 队长昵称
- 队员昵称
- 位置分配
- 肉鸽选择
- 参赛宣言

## 安全特性

- ✅ Flask-Login 用户认证
- ✅ RBAC 权限控制
- ✅ CSRF 防护
- ✅ 密码加密存储 (Werkzeug)
- ✅ SQL 注入防护 (SQLAlchemy ORM)

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

## 开发建议

- 使用 `FLASK_ENV=development` 启用调试模式
- 定期备份数据库
- 生产环境更改 SECRET_KEY
- 使用环境变量管理敏感配置

## 许可证

Copyright © 2026 冷水坑杯 #3 赛事组委会

---

有问题或建议？请联系管理员。
"# lskb-competition" 
