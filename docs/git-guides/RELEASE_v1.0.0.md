# Release v1.0.0

发布日期: 2026-03-28

## 版本定位

首个公开版本，覆盖基础下载流程、GUI 操作和仓库文档。

## 本版本内容

1. 主程序: pixiv_downloader.py
2. 获取模块: pixiv_fetcher.py
3. 工具模块: pixiv_utils.py
4. 交集模式: pixiv_multi_filter.py
5. Token 获取: pixiv_get_token.py
6. 依赖清单: requirements.txt

## Git 与文档

1. 初始化 Git 仓库
2. 默认分支切换为 main
3. 配置 .gitignore 忽略敏感文件与缓存
4. 增加项目说明与操作文档

## 依赖

- pixivpy3>=3.7.0
- requests>=2.28.0

## 已知限制

1. 接口限流时可能出现空数据或速度下降。
2. 网络不稳定会影响下载成功率。
3. 交集模式在来源差异较大时可能无结果。

## 后续计划

1. 优化错误提示和排障指引。
2. 增加参数预设模板。
3. 增加批量任务状态导出。
