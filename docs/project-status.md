# ReproTrace 项目状态

更新时间：2026-08-09

状态基线：`main@57ba4d1802a587deeb1a852c2ea10b5a5437a6fe`

本文只记录当前有效状态。阶段性过程、被后续修复取代的结论和独立审计来源见 [`audit-ledger.md`](audit-ledger.md)；更早的逐阶段细节保留在 Git 历史中。

## 当前结论

| 项目 | 状态 |
| --- | --- |
| C5.0 | `ACCEPTED`，合并提交 `8746a00257528fce2d90faac07923f1c45e0bf6b` |
| C5.1a | `ACCEPTED / MERGED`，PR head `be7cee21...`，合并提交 `57ba4d1802...` |
| H1 — snapshot identity / verifier-time TOCTOU | `CLOSED` |
| H2 — command protocol authority | `CLOSED` |
| H3 — wildcard artifact membership | `CLOSED` |
| F1 — stale canonical derived outputs | `CLOSED` by C5.1a |
| F-C5.1a-01 — mutation into replacement root | `CLOSED` by root-bound authority remediation |
| 当前 merge blocker | 无 |
| 下一里程碑 | 未决定，需人工批准 |

## 当前实现边界

ReproTrace 是面向机器学习论文复现的 evidence-first 执行记录与验证工具。它记录并验证 bundle-local evidence，不把命令退出成功等同于论文复现成功。

当前 schema-1 核心语义包括：

- canonical `evidence.index.json` 与 evidence-root；
- handle-bound evidence acquisition 与 verified snapshot；
- snapshot-backed semantic verification、metric derivation 与 report rendering；
- resolved-manifest-bound command protocol 与 wildcard artifact membership；
- verification completeness、assurance、recorded execution 与 declared result 的正交表达；
- root-bound canonical derived-output lifecycle。

`verification.json` 与 `report.md` 是 unindexed、可再生成的派生输出，不进入 evidence-root 公式。write-intending refresh 在验证前失效旧 canonical outputs。POSIX mutation 使用 pinned directory fd；Windows 保留不含 delete sharing 的 directory handle，以保护所选 root 的生命周期。

`verify_bundle(write=False)` 不取得 mutation authority、不失效、不发布，成功和失败路径均只读。schema-0 仍限制为：

```text
assurance_level = recorded
result_status = not_evaluated
evidence_root_sha256 = null
```

## C5.1a 接受的 partial-publication 语义

| 结果 | `verification.json` | `report.md` |
| --- | --- | --- |
| verification publication 前失败 | absent | absent |
| verification publication 失败 | absent | absent |
| verification 成功、report publication 失败 | fresh | absent |
| verification-only 成功 | fresh | absent |
| combined verify/report 成功 | fresh | fresh |

这不是两文件 transaction。同一 bundle 的 write-intending 操作需由调用者串行化。

## 合并后验证基线

GitHub Actions push run `31299206253` 针对合并后的 `main@57ba4d1802...`：

| 平台 | 结果 |
| --- | --- |
| Ubuntu 3.10 | `427 passed, 6 skipped` |
| Ubuntu 3.12 | `427 passed, 6 skipped` |
| macOS 3.12 | `427 passed, 6 skipped` |
| Windows 3.12 | `426 passed, 7 skipped` |

四个 job 与其中 `Run pytest` step 均成功。

## 非阻塞 maintenance backlog

以下不重开 C5.0/C5.1a acceptance，也不阻塞当前 `main`：

1. 严格拒绝 manifest `schema_version` 与 `run.seed` 的 Python bool alias。
2. 决定并统一合法 dry-run bundle 的 standalone `verify` 退出码契约。
3. 将 assurance contract 中 Stage 6.1 的 verifier-time TOCTOU caveat 明确标为历史限制，并链接 Stage 6.2 closure。
4. 可选 hardening：非目标 POSIX 的 `dir_fd` capability fail-closed 处理、非目录 root 的一致异常分类，以及 `open → fstat` 窗口的额外 deterministic test。

任何 maintenance 项开始前都应单独固定 base、定义验收标准，不得顺带扩大 assurance claim。

## 明确 non-goals

当前实现不建立：

- hostile multi-user filesystem security；
- arbitrary high-frequency ABA resistance；
- concurrent same-bundle writer safety；
- filesystem-wide atomic snapshot 或 two-file transaction；
- power-loss/directory-fsync durability；
- producer/execution authenticity、签名或 attestation；
- independent replay 或 scientific reproduction。

## PEFT-ViT 研究线

`examples/peft-vit/reprotrace.yaml` 仍是 approximate adapter，不是严格论文复现配置。正式 GPU run 尚未获准；开始前必须明确：

- 显式 seed 传递；
- CIFAR-100 预置与哈希；
- DINO 权重 revision；
- precision 选择。

未经确认不启动训练、GPU 或下载型实验。

## 下一步

当前没有自动继承的下一里程碑。下一轮应先在人类确认下选择其一：

1. 小型 maintenance PR；
2. 文档与状态账本整理；
3. 新 research milestone 的设计阶段。

旧对话、旧草案和“后续候选”均不是批准记录。
