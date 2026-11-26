# mydemo — 建筑 AI 预审工作台（前端）

Angular 21 Standalone + Signals + Tailwind CSS 3 + lucide-angular 的前端工作区。
当前对应 D2：动态菜单、工作台首页、项目列表与新建项目、403 空状态页。

## 快速开始

```bash
npm install
npm start            # http://localhost:3000
npm run build        # 产物在 dist/mydemo
```

开发服务器监听 `3000`（与后端 `CORS_ORIGINS` 一致），并把 `/api` 代理到
`http://127.0.0.1:8000`（见 `proxy.conf.json`，后端启动端口不同时改这里或
用 `ng serve --proxy-config`）。

接口前缀 `API_BASE_URL = '/api/v1'` 定义在 `src/app/core/api.ts`，与后端
`API_V1_PREFIX` 保持同源，不散落在各处。

## 目录约定

```text
src/app/
├── core/                  全局单例：服务、拦截器、守卫、接口模型、字典
│   ├── api.ts             API 前缀与重试标记（HttpContextToken）
│   ├── api-error.ts       统一错误体解析（code / message / trace_id）
│   ├── dictionaries.ts    全站唯一字典：业态 / 设计阶段 / 图纸状态 / 严重度 / 项目角色
│   ├── upload-rules.ts    上传前端校验：扩展名、DWG 特判、体积、文件名字符清洗
│   ├── guards/            authGuard：未登录 → /login?redirect=，无权限 → /403
│   ├── interceptors/      注入 Bearer、401 静默续期、GET 403 跳 /403
│   ├── models/            prd-07 §6.2–§6.5 接口模型（snake_case 直传）
│   └── services/          auth / jwt / toast / layout / dashboard / project / drawing / upload
├── layout/                220px 侧边栏（后端菜单驱动）+ 56px 顶栏 + 内容区
├── pages/                 login、dashboard、projects（列表 / 新建弹窗 / 详情 / 上传）、forbidden
└── shared/                app-icon、app-toast-host、confirm-dialog、时间与字节格式化
```

## 登录态规则

- `rememberMe = true` → token 落 `localStorage`；否则落 `sessionStorage`，
  两者互斥，切换时清空另一边。
- access 过期（401 `TOKEN_EXPIRED`）→ 单飞 `POST /auth/refresh` 并重放原请求一次；
  多标签页同时 401 只发一次续期。
- 续期失败或其它 401 → 清本地态并回 `/login?redirect={原路径}`。
- 403 `FORBIDDEN` → 读请求跳 `/403` 空状态页；写请求不换页，错误交回调用方
  在表单内联展示（避免提交中丢内容）。
- 多标签页登出通过 `storage` 事件同步。

## 页面与数据

- 侧边栏菜单来自 `GET /auth/me` 的 `menus`，两级结构；尚未落地的模块统一置灰并提示
  「该模块正在开发中」（判断依据见 `layout/menu.ts` 的 `IMPLEMENTED_PATHS`）。
- 工作台统计卡由 `GET /dashboard/overview` 单接口驱动，三态：骨架 / `—`+重试 / 真实值；
  最近项目取 `GET /projects?sort=recent&page_size=3`。
- 项目列表 `GET /projects?sort=recent`，卡片网格 1 / 2 / 3 列；新建走
  `POST /projects`，成功后重拉列表并以 Toast 反馈。
- 统计卡可下钻：`?filter=pending-review` / `?status=open` / `?closed_this_week=1`，
  列表页读取这些 query 后展示「已筛选：xxx · N 个项目」标签，可一键清除。
- 项目详情 `/projects/:id`：图纸表格（类型 / 大小 / 状态 / 问题数 / 上传时间 / 查看·重新预审·下载·删除）
  + 成员列表；路由参数通过 `withComponentInputBinding()` 映射到组件 `input()`。
- 上传 `POST /drawings/upload-sessions` → 5MB 分片 `PUT .../chunks/{index}`（**3 并发**、单片重试 1/2/4s）
  → `POST .../complete`。队列串行、文件内并发；`.dwg` 不建会话、只弹转换引导；
  `beforeunload` 仅在有活跃任务时拦截。

## UI 约定

- 图标只用 `<app-icon name="…">`（`lucide-angular`，1.75 线宽），业务代码不写内联 SVG。
- 主色 `#1890FF`、错误 `#F5222D`；语义色与状态色（批注 / 严重度）统一在
  `tailwind.config.js` 定义，页面不写十六进制色值。
- 卡片 `rounded-card`（8px）+ `shadow-card`（极浅阴影），输入框 focus 蓝色描边 + 4px 柔光环。
