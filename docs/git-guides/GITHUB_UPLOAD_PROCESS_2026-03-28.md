# GitHub 上传流程记录

记录日期: 2026-03-28

本文档用于记录本项目从本地初始化到首次推送 GitHub 的过程，并提供后续可复用的推送步骤。

## 一、已完成事项

1. 创建 .gitignore，忽略缓存、敏感配置和常见输出目录。
2. 初始化本地仓库并完成首次提交。
3. 将默认分支统一为 main。
4. 绑定远程仓库并完成首次推送。
5. 验证 main 与 origin/main 跟踪关系正常。

## 二、当前仓库信息

- 远程仓库: https://github.com/InfiniX520/ImageTool-Lab.git
- 默认分支: main
- 跟踪分支: origin/main

## 三、现在如何推送（单次操作）

在项目根目录执行:

```bash
git status -sb
git add .
git commit -m "docs: update documents"
git push
```

说明:

- 第一次推送后，main 已建立跟踪关系，后续直接使用 git push。
- 如弹出浏览器认证，按提示登录并授权即可。

## 四、以后如何推送（日常模板）

每次修改后执行:

```bash
git status -sb
git add .
git commit -m "feat/fix/docs/chore: 简要说明"
git push
```

提交类型建议:

- feat: 新功能
- fix: 问题修复
- docs: 文档更新
- refactor: 重构
- chore: 维护类改动

## 五、常见问题处理

### 1) push 被拒绝（远程有新提交）

```bash
git pull --rebase origin main
git push
```

### 2) 漏提交文件

```bash
git add <文件名>
git commit -m "chore: add missed files"
git push
```

### 3) 误暂存敏感文件

```bash
git restore --staged pixiv_config.json
```

## 六、相关文档

- LOCAL_RECOVERY_GUIDE.md
- RELEASE_v1.0.0.md
- README.md（项目总说明）

## 七、优化验收记录（2026-03-28）

已完成并验证:

1. 原图下载改造为 Pixiv Ajax `body.urls.original` 与 `body.pageCount`。
2. 多P下载、断点续传、文件大小校验、防重记录与 Markdown 记录均可工作。
3. 清空记录可同步清理 JSON/Markdown 记录文件。
4. 旧版 `success_log.md` 已降级为兼容开关，默认不写入，避免重复记录。
