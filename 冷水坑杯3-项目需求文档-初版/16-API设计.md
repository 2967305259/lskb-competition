# API 设计

## 当前 API 状态

系统目前是服务端渲染（SSR）为主的架构，JSON API 端点较少且设计不统一。现有 API 集中在选手和引导模块。

## 现有 API 端点

### 选手相关 API
| 端点 | 方法 | 认证 | 说明 |
|---|---|---|---|
| `/player/api/players` | GET | 登录 | 获取所有选手列表 |
| `/player/api/players/<int:user_id>` | GET | 登录 | 获取单个选手详情 + 比赛统计 |
| `/player/api/players/<name>` | GET | 登录 | 按用户名/昵称搜索选手 |

### 新手引导 API
| 端点 | 方法 | 认证 | 说明 |
|---|---|---|---|
| `/player/api/onboarding/status` | GET | 登录 | 获取引导状态 |
| `/player/api/onboarding/complete` | POST | 登录 | 标记引导完成 |
| `/player/api/onboarding/reset` | POST | 登录 | 重置引导状态 |

## 现有 API 响应格式

### 选手列表
```json
{
  "players": [
    {
      "id": 1,
      "username": "player1",
      "nickname": "选手一",
      "avatar": null,
      "role": "player",
      "created_at": "2026-01-01T00:00:00",
      "team": {
        "id": 1,
        "team_name": "冠军队",
        "team_slogan": "加油！"
      }
    }
  ],
  "total": 100
}
```

### 选手详情
```json
{
  "player": { /* 用户对象 */ },
  "stats": {
    "wins": 5,
    "losses": 2,
    "draws": 1,
    "total_matches": 8
  },
  "recent_matches": [ /* 最近比赛列表 */ ]
}
```

## API 设计不足

### 严重问题
1. **无统一 API 前缀**：端点散落在 `/player/api/` 下，无 `/api/v1/` 标准化前缀
2. **无统一响应格式**：缺少标准化的 `{code, message, data}` 响应信封
3. **无分页标准**：选手列表不支持分页参数（page、per_page）
4. **无错误码体系**：错误响应格式不一致
5. **无 API 文档**：无 OpenAPI/Swagger 文档
6. **缺少关键 API**：无队伍 API、比赛 API、排行榜 API、通知 API

### 建议的 API 架构

#### 统一前缀
```
/api/v1/
```

#### 统一响应格式
```json
{
  "code": 200,
  "message": "success",
  "data": { /* 响应数据 */ },
  "pagination": {
    "page": 1,
    "per_page": 20,
    "total": 100,
    "total_pages": 5
  }
}
```

#### 统一错误格式
```json
{
  "code": 400,
  "message": "参数错误：缺少 roguelike 字段",
  "errors": {
    "roguelike": "该肉鸽主题在本队已达上限"
  }
}
```

#### 建议的 API 路由表

**认证模块 `/api/v1/auth/`**
| 端点 | 方法 | 说明 |
|---|---|---|
| `/auth/login` | POST | 登录（返回 token） |
| `/auth/register` | POST | 注册 |
| `/auth/me` | GET | 获取当前用户信息 |

**选手模块 `/api/v1/players/`**
| 端点 | 方法 | 说明 |
|---|---|---|
| `/players` | GET | 选手列表（分页、搜索、筛选） |
| `/players/<id>` | GET | 选手详情 |
| `/players/<id>/matches` | GET | 选手比赛历史 |
| `/players/<id>/stats` | GET | 选手统计 |

**队伍模块 `/api/v1/teams/`**
| 端点 | 方法 | 说明 |
|---|---|---|
| `/teams` | GET | 队伍列表 |
| `/teams/<id>` | GET | 队伍详情 |
| `/teams` | POST | 创建队伍（队长） |
| `/teams/<id>` | PUT | 编辑队伍（队长） |
| `/teams/<id>/members` | GET | 队伍成员列表 |

**比赛模块 `/api/v1/matches/`**
| 端点 | 方法 | 说明 |
|---|---|---|
| `/matches` | GET | 比赛列表 |
| `/matches/<id>` | GET | 比赛详情 |
| `/matches/<id>/submit` | POST | 提交比赛结果 |
| `/matches/<id>/schedule` | POST | 提议比赛时间 |

**排行榜模块 `/api/v1/rankings/`**
| 端点 | 方法 | 说明 |
|---|---|---|
| `/rankings/teams` | GET | 队伍排名 |
| `/rankings/players` | GET | 个人排名 |

**通知模块 `/api/v1/notifications/`**
| 端点 | 方法 | 说明 |
|---|---|---|
| `/notifications` | GET | 通知列表 |
| `/notifications/<id>/read` | POST | 标记已读 |
| `/notifications/read-all` | POST | 全部已读 |

## API 改进建议

1. **建立统一 API 层**：创建 `api` 蓝图，标准化路由和响应格式
2. **完善 RESTful 设计**：遵循 REST 规范，正确使用 HTTP 方法和状态码
3. **实现认证机制**：考虑 JWT Token 或 API Key 认证（除 Cookie 外）
4. **添加 API 文档**：使用 Flasgger 或手动编写 OpenAPI 规范
5. **实现限流**：添加请求频率限制防止滥用
6. **版本化管理**：`/api/v1/` 前缀便于未来 API 升级