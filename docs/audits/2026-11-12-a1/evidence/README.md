# A1 审计的小型证据副本

2026-11-12 为 Git 版本留档从工作区原审计批次逐字节复制；原件未改动。这里保留当时的失败和核验结果，不能视为之后修复的验收。

| 文件 | SHA-256 |
| --- | --- |
| [existing_tests.xml](existing_tests.xml) | fd3f369ae0261f9e9bed1d968ad461f31e123c3bc1158784b49be6301e03492f |
| [probe_results.json](probe_results.json) | 3c5c0fbb66ecf2f5a5cee3772021de16061341c82aff712ccfdbaf0f07cfdc0c |
| [probes.py](probes.py) | 8772784af3e54e1f14f528ac43132722ba0eba94caa1a741f4599d822694d146 |
| [audit_coverage.json](audit_coverage.json) | a6ea1123ee10a8855539b12d8d8a406885d3996402b41314d966098b209821b0 |
| [audit_validation.json](audit_validation.json) | e6cab0f75813253e301c77e13fad130e0f7a7e10f8d7075ec816c6faa63d0bb2 |

existing_tests.xml：250 通过、5 失败、3 跳过。probe_results.json：13 个已执行的审计反例。audit_validation.json 的通过只指审计材料核验，product_ready=false；其中摘要和路径属于审计完成时的快照，不随本次版本文档更新而重算。

原始位置：工作区 .runtime/jpgpng_a1_audit_20261111_01/。源码副本、完整合成目录和其他旧批次仍留本机，不重复塞入 Git。probes.py 是当时一次性调查脚本，含原工作区布局假设；不要在当前目录直接重跑或覆盖原证据，修复时按开发指导转换为新测试。

审计说明见[总览](../README.md)；恢复源码见[版本留档](../../../VERSION_CONTROL.md)。
