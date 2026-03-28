# GitHub 上传与发布流程

更新时间: 2026-03-28

本文档用于记录本仓库日常推送与版本发布流程。

## 1. 仓库信息

- 远程仓库: https://github.com/InfiniX520/ImageTool-Lab.git
- 默认分支: main
- 当前发布版本: v2.0.0

## 2. 日常推送（不发版）

在项目根目录执行:

```bash
git status -sb
git add .
git commit -m "feat/fix/docs/chore: 简要说明"
git push
```

## 3. 版本发布（v2.0.0 模板）

当需要更新版本并发布时，按顺序执行:

```bash
git status -sb
git add .
git commit -m "release: v2.0.0"
git tag -a v2.0.0 -m "Release v2.0.0"
git push origin main
git push origin v2.0.0
```

检查是否成功:

```bash
git tag -l | findstr v2.0.0
git ls-remote --tags origin | findstr v2.0.0
```

## 4. 下次发版怎么改

将下面三处替换为新版本号:

1. 提交信息中的版本号
2. `git tag -a` 的标签名
3. `git push origin` 的标签名

示例（假设下次是 v2.1.0）:

```bash
git commit -m "release: v2.1.0"
git tag -a v2.1.0 -m "Release v2.1.0"
git push origin v2.1.0
```

## 5. 常见问题

1. push 被拒绝（远程有新提交）:

```bash
git pull --rebase origin main
git push origin main
```

2. 误提交了不该追踪的本地配置:

```bash
git restore --staged pixiv_config.json
```

3. 标签已存在但内容需要重发（确认后再做）:

```bash
git tag -d v2.0.0
git push origin :refs/tags/v2.0.0
git tag -a v2.0.0 -m "Release v2.0.0"
git push origin v2.0.0
```

## 6. 相关文档

- LOCAL_RECOVERY_GUIDE.md
- RELEASE_v2.0.0.md
- RELEASE_v1.0.0.md
- README.md
