# 本地改错未推送恢复手册

适用范围:

- 错误发生在本地
- 尚未 push 到远程

## 0. 先判断当前状态

```bash
git status -sb
git log --oneline -n 5
```

根据输出判断进入哪个场景。

## 1. 未暂存（改错但未 git add）

### 1.1 恢复单个文件

```bash
git restore <文件名>
```

### 1.2 恢复全部未暂存改动

```bash
git restore .
```

## 2. 已暂存（git add 了）但未提交

### 2.1 撤销暂存，不丢改动

```bash
git restore --staged .
```

### 2.2 撤销暂存并丢弃改动

```bash
git restore --staged .
git restore .
```

## 3. 已本地提交，但未推送

### 3.1 撤销提交，保留改动到工作区（常用）

```bash
git reset --mixed HEAD~1
```

### 3.2 撤销提交，保留改动在暂存区

```bash
git reset --soft HEAD~1
```

### 3.3 撤销提交并丢弃改动（谨慎）

```bash
git reset --hard HEAD~1
```

## 4. 误删文件恢复

### 4.1 恢复单个文件

```bash
git restore <文件名>
```

### 4.2 恢复全部已跟踪文件

```bash
git restore .
```

## 5. 误操作后的反悔恢复

查看操作历史:

```bash
git reflog -n 20
```

回到指定历史点（示例）:

```bash
git reset --hard HEAD@{3}
```

## 6. 快速自救模板

```bash
git status -sb
git log --oneline -n 5
git restore --staged .
git restore .
git status -sb
```

如果已经本地 commit 但未 push，改用:

```bash
git reset --mixed HEAD~1
git status -sb
```

## 7. 风险提示

1. git reset --hard 会丢失当前改动，请确认后再执行。
2. git clean -fd 会删除未跟踪文件，建议先用 git clean -nd 预览。
3. 不确定时优先使用 restore、--soft、--mixed。
