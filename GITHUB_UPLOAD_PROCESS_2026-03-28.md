# GitHub 上传与整理过程记录

记录日期: 2026-03-28

## 目标

- 在不改动业务代码的前提下，将本地项目上传到 GitHub
- 优化仓库管理基础（忽略规则、分支命名、首次提交）
- 完成可复用的发布文档整理

## 项目初始状态

本地目录包含以下核心文件:

- pixiv_downloader.py
- pixiv_fetcher.py
- pixiv_get_token.py
- pixiv_multi_filter.py
- pixiv_utils.py
- pixiv_config.json
- requirements.txt
- 准备下载.txt

初始状态下:

- 尚未初始化 Git 仓库
- 无 .gitignore
- pixiv_config.json 存在潜在敏感信息风险（token/本地配置）

## 执行过程（按时间顺序）

### 1) 检查目录与 Git 状态

执行检查后确认:

- 当前目录为项目根目录
- 尚未处于 Git 仓库中

### 2) 新建 .gitignore

新增忽略规则，覆盖如下类别:

- Python 缓存: __pycache__/, *.pyc
- 虚拟环境: .venv/, venv/
- IDE/系统文件: .vscode/, .idea/, .DS_Store, Thumbs.db
- 日志: *.log
- 敏感配置: pixiv_config.json, .env
- 常见输出目录: images/, downloads/, output/

目的:

- 防止敏感信息误提交
- 避免无关文件污染版本历史

### 3) 初始化仓库并暂存

执行:

```bash
git init
git add .
```

结果:

- 仓库初始化成功
- 文件已进入暂存区

### 4) 处理首次提交身份配置

首次提交时报错:

- Git 未配置 user.name / user.email

处理方式:

- 采用仓库级本地配置（不影响全局）

```bash
git config user.name "JuiLiu"
git config user.email "juiliu@users.noreply.github.com"
```

### 5) 统一默认分支为 main

执行:

```bash
git branch -m main
```

目的:

- 与 GitHub 默认主分支保持一致

### 6) 完成首次提交

执行:

```bash
git commit -m "chore: initial project import"
```

结果:

- root commit 创建成功
- 核心源码与文档进入版本库

### 7) 验证敏感文件已被忽略

检查确认:

- pixiv_config.json 被忽略
- __pycache__/ 被忽略

说明:

- 敏感配置未进入 Git 历史，满足基础安全要求

### 8) 绑定远程仓库

远程仓库地址设置为:

- https://github.com/InfiniX520/ImageTool-Lab.git

执行:

```bash
git remote add origin https://github.com/InfiniX520/ImageTool-Lab.git
```

若已存在 origin，则改为:

```bash
git remote set-url origin https://github.com/InfiniX520/ImageTool-Lab.git
```

### 9) 首次推送与认证

执行:

```bash
git push -u origin main
```

过程出现认证提示:

- 终端提示需要浏览器完成 GitHub 授权

处理后结果:

- 推送完成
- main 建立与 origin/main 的跟踪关系

### 10) 远程结果核验

最终核验项:

- origin 地址正确
- branch.main.remote = origin
- branch.main.merge = refs/heads/main
- 远程可查询到 main 分支提交哈希

结论:

- 本地项目已成功上传到 GitHub

## 本次产出清单

- .gitignore
- README.md
- RELEASE_v1.0.0.md
- GITHUB_UPLOAD_PROCESS_2026-03-28.md（本文件）

## 后续建议

- 在 GitHub 页面创建首个 Release（可直接使用 RELEASE_v1.0.0.md 内容）
- 增加 issue 模板，沉淀排障流程
- 视需求补充 LICENSE 与 CONTRIBUTING 文档

## 现在怎么推送（你可以立刻实操）

当前仓库状态为 3 个未跟踪文档文件:

- README.md
- RELEASE_v1.0.0.md
- GITHUB_UPLOAD_PROCESS_2026-03-28.md

请在项目根目录依次执行:

```bash
git add README.md RELEASE_v1.0.0.md GITHUB_UPLOAD_PROCESS_2026-03-28.md
git commit -m "docs: add README, release notes and upload process log"
git push
```

说明:

- 由于 main 已经跟踪 origin/main，后续推送直接用 git push 即可
- 若弹出 GitHub 登录授权，按提示在浏览器确认即可

可选检查命令（建议养成习惯）:

```bash
git status -sb
git log --oneline -n 3
```

## 以后怎么推送（日常更新模板）

每次更新后使用以下固定流程:

```bash
git status -sb
git add .
git commit -m "feat/fix/docs: 简要说明本次改动"
git push
```

推荐提交信息规范:

- feat: 新功能
- fix: 修复问题
- docs: 文档变更
- refactor: 重构（无功能变化）
- chore: 杂项维护

## 常见失败与处理

1. push 被拒绝（远程有新提交）

```bash
git pull --rebase origin main
git push
```

2. 提交后发现漏文件

```bash
git add <漏掉的文件>
git commit -m "chore: add missed files"
git push
```

3. 误把敏感文件加入暂存

```bash
git restore --staged pixiv_config.json
```

并确认 .gitignore 仍包含该文件规则。
