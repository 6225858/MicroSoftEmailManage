# 极简版 Hotmail 邮箱管理系统开发文档

## 一、项目目标

开发一个：

```text
本地运行
单文件部署
简单易维护
```

的 Hotmail/Outlook 邮箱管理系统。

功能：

* 批量导入邮箱
* 管理邮箱
* 读取收件箱
* 读取垃圾箱
* 标签管理
* 邮件自动刷新
* Web后台管理

---

# 二、技术方案

全部使用 Python。

## 技术栈

| 模块    | 技术         |
| ----- | ---------- |
| Web框架 | FastAPI    |
| 页面模板  | Jinja2     |
| 数据库   | SQLite     |
| ORM   | SQLAlchemy |
| 前端UI  | Bootstrap  |
| 邮件协议  | IMAP       |
| OAuth | requests   |

---

# 三、项目结构

```text
project/
│
├── main.py                 # 主程序
├── database.py             # 数据库初始化
├── models.py               # 数据模型
├── mail_service.py         # 邮件读取
├── oauth_service.py        # token刷新
│
├── templates/
│   ├── login.html
│   └── index.html
│
├── static/
│   ├── app.js
│   └── app.css
│
└── mail.db
```

---

# 四、数据库设计

## 1. 邮箱表

```sql
CREATE TABLE mail_account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    email TEXT UNIQUE,
    password TEXT,

    client_id TEXT,
    refresh_token TEXT,

    cached_access_token TEXT,
    access_token_expire_time INTEGER,

    tags TEXT,

    created_at INTEGER
);
```

---

## 字段说明

| 字段                       | 说明             |
| ------------------------ | -------------- |
| email                    | 邮箱             |
| password                 | 邮箱密码           |
| client_id                | 微软client_id    |
| refresh_token            | 授权码            |
| cached_access_token      | access_token缓存 |
| access_token_expire_time | token过期时间      |
| tags                     | 标签，逗号分隔        |
| created_at               | 创建时间           |

---

# 五、后台登录方案

系统只需要：

```text
一个后台密码
```

---

## 登录接口

```http
POST /login
```

请求：

```json
{
  "password": "admin123"
}
```

---

## 返回

```json
{
  "token": "admin123"
}
```

这里：

```text
token = 后台密码
```

不使用：

* JWT
* Session
* Redis

---

## 后续接口校验

请求头：

```text
X-Token: admin123
```

后台统一校验：

```python
if request.headers.get("X-Token") != ADMIN_PASSWORD:
    return {"error": "unauthorized"}
```

---

# 六、批量导入邮箱

## 支持格式

商家格式：

```text
邮箱----密码----clientid----授权码
```

示例：

```text
abc@hotmail.com----123456----clientidxxxx----refresh_token_xxx
abc2@hotmail.com----123456----clientidxxxx----refresh_token_xxx
```

---

## 导入方式

后台页面：

```text
textarea 多行文本框
```

直接粘贴。

点击：

```text
导入
```

即可。

---

## 导入逻辑

```python
lines = text.splitlines()

for line in lines:
    arr = line.split("----")

    if len(arr) != 4:
        continue
```

---

# 七、后台页面布局

```text
┌────────────────────────────────────┐
│ 顶部栏                              │
├───────────────┬────────────────────┤
│ 左侧邮箱列表   │ 右侧邮件列表        │
│                │                    │
│ 搜索           │ 收件箱             │
│ 标签           │ 垃圾箱             │
│ 分页           │ 邮件正文           │
└───────────────┴────────────────────┘
```

---

# 八、左侧邮箱列表

功能：

* 分页
* 搜索
* 标签显示
* 标签筛选

---

## 搜索

支持：

```text
邮箱搜索
标签搜索
```

---

## 默认状态

进入后台：

```text
不默认选中邮箱
```

右侧显示：

```text
请选择邮箱
```

---

# 九、标签系统

## 标签存储方式

直接存字符串：

```text
已使用,密码错误
```

不单独建标签表。

保持简单。

---

## 默认标签

建议：

```text
未使用
已使用
密码错误
令牌失效
正常
```

---

## 标签功能

支持：

* 添加标签
* 删除标签
* 修改标签

---

# 十、access_token 缓存方案

## 目的

避免频繁请求微软接口。

---

## 缓存时间

```text
30分钟
```

---

## 获取流程

```text
1. 查询数据库
2. 判断 token 是否过期
3. 未过期直接返回
4. 已过期刷新 token
5. 保存新 token
```

---

## 微软刷新接口

```text
https://login.microsoftonline.com/common/oauth2/v2.0/token
```

---

# 十一、邮件读取方案

## IMAP服务器

```text
outlook.live.com
```

---

## OAuth2 登录

```python
mail.authenticate('XOAUTH2', auth_string)
```

---

## 收件箱

```python
mail.select('Inbox')
```

---

## 垃圾箱

动态查找：

```python
mail.list()
```

自动匹配：

```text
Junk
Junk Email
垃圾邮件
```

---

# 十二、邮件自动刷新

## 刷新规则

只有：

```text
当前选中了邮箱
```

才自动刷新。

---

## 刷新频率

```text
5秒一次
```

---

## 前端实现

```javascript
setInterval(loadMail, 5000)
```

---

## 停止刷新条件

* 切换邮箱
* 取消选中
* 页面关闭

---

# 十三、邮件列表展示

## Tabs

```text
收件箱
垃圾箱
```

---

## 邮件列表

显示：

```text
主题
发件人
时间
```

---

## 点击邮件

右侧显示：

```text
HTML正文
```

---

# 十四、异常处理

## refresh_token失效

自动添加标签：

```text
令牌失效
```

---

## IMAP登录失败

自动添加标签：

```text
密码错误
```

---

## 微软风控

记录日志即可。

---

# 十五、推荐开发顺序

## 第一阶段

* SQLite
* 后台登录
* 批量导入
* 邮箱列表

---

## 第二阶段

* OAuth2刷新
* access_token缓存
* IMAP读取

---

## 第三阶段

* 收件箱
* 垃圾箱
* 邮件详情

---

## 第四阶段

* 标签系统
* 搜索
* 自动刷新

---

# 十六、最终方案

最终项目：

```text
一个 Python 项目
一个 SQLite 数据库
一个 Web 页面
```

特点：

```text
简单
轻量
易维护
本地运行
无需前后端分离
```
