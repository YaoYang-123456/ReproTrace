# ReproTrace 审计账本

更新时间：2026-08-09

用途：记录审计对象、证据等级、当时结论及其后续裁决。本文不是新的 acceptance，也不以多数投票替代 production reasoning。

## 证据等级

| 等级 | 含义 |
| --- | --- |
| Production-direct | 从固定 SHA 的 production source、调用链与平台 API 合同直接推导 |
| Test-supported | 检查测试结构并在固定对象上执行真实 production primitive |
| CI-supported | 独立读取 workflow、job、step 与日志 |
| Evidence-only | 只能核对作者或其他环境提供的结果 |
| Inconclusive | 关键源码或执行环境不可达，无法签发结论 |

## C5.0 账本

| 轮次 | Exact object | 主要证据 | 当时结论 | 当前解释 |
| --- | --- | --- | --- | --- |
| C5.0 independent final audit | `921943155f04da7839a43837b8961bdc5b5f1dc0` | source、CI、独立 mutation fixtures | `REJECT`；H1/H2/H3 为 HIGH | 历史有效发现；触发 Stage 6.2 remediation |
| Stage 6.2f final acceptance | accepted pre-closure `d8836ae0076274b12d81be2d6ea3324d8ea5ffcc`；Actions `31269321204` | production + adversarial tests + CI | `H1/H2/H3 CLOSED`，C5.0 accepted | 当前 closure 的来源之一 |
| PR #2 merged state | accepted PR head `9adaf60ed1802670cb9ef883c07dda3eb4834ba6`；merge `8746a00257528fce2d90faac07923f1c45e0bf6b` | tree comparison、production call graph、定向 probes、post-merge CI `31287570651` | 核心 acceptance 保持；无 rollback | 权威 C5.0 merged baseline |
| C5.0 post-merge independent audit | `8746a00257528fce2d90faac07923f1c45e0bf6b` | production + probes + CI status | H1/H2/H3 remain closed；新开 F1 MEDIUM，另有 F2–F4 LOW | F1 由 C5.1a 关闭；F2–F4 保留为 non-blocking maintenance |

## C5.1a 账本

| 轮次 | Exact object | 证据与限制 | 当时结论 | 当前裁决 |
| --- | --- | --- | --- | --- |
| v1 independent implementation review | `1a26c1541133dc2a7d7c4c4428fcac6b68617d26` | production call graph + independent root-replacement probes | `F-C5.1a-01 HIGH`；`F1 NOT CLOSED`；`NO-GO` | 历史有效 finding；由 `77a7ae59...` remediation 处理 |
| M3 remediation verification | `be7cee21bfab1bf69749d6a07b9f6499c10a7952` | source、platform semantics、定向执行 | `PASS`；F-C/F1 closed；H1–H3 remain closed；GO | 支持最终 acceptance |
| M3 Pro blocker-only re-review | `be7cee21...` | blocker-only source review + Linux 定向检查 + CI job status；按用户要求压缩输出 | `PASS`；F-C closed；H1–H3 remain closed；GO | 支持最终 acceptance；篇幅短不等于证据弱，但报告应保留实际执行清单 |
| Work final independent acceptance | `be7cee21...` | full production call graph、9-window tests、integration/adversarial tests、push/PR CI logs | `PASS`；F-C/F1 closed；H1–H3 remain closed；GO | 主要权威 acceptance 之一 |
| Opus 5 first attempt | `be7cee21...` | 能固定 SHA/CI，但 production source 完全不可读、tests 未执行 | `CHANGES REQUIRED`，实质为 `INSUFFICIENT EVIDENCE — ACCEPTANCE NOT ISSUED` | 记录为环境受阻；不是代码反证，不参与 closure 逆转 |
| Opus 5 full review | `be7cee21...` | authoritative clone/source、full Linux suite `427/6`、lifecycle `31/2`、独立 probes、CI metadata | `PASS WITH NON-BLOCKING FINDINGS`；F-C/F1 closed；GO | 支持最终 acceptance；LOW/INFO 单独归档 |
| Opus addendum 01 | `be7cee21...` | archive 对 git tree：56/56、0 缺失/额外/content mismatch；追加 call-graph 阅读 | verdict unchanged，GO | 增强来源一致性；CRLF 转换仅为 INFO |
| PR #3 merged state | head `be7cee21...`；merge `57ba4d1802a587deeb1a852c2ea10b5a5437a6fe` | GitHub PR/commit、post-merge Actions `31299206253` logs | merged；四平台 full pytest success | 当前 `main`；`main` 与 merge commit identical |

## 最终 finding 状态

| ID | 最终状态 | 依据 |
| --- | --- | --- |
| H1 | `CLOSED` / remains closed | Stage 6.2 handle-bound acquisition、snapshot-backed semantics、final adversarial acceptance、C5.1a regression check |
| H2 | `CLOSED` / remains closed | resolved-manifest-bound command protocol 与 adversarial closure |
| H3 | `CLOSED` / remains closed | canonical wildcard membership validation 与 adversarial closure |
| F1 | `CLOSED` | C5.1a invalidate-before-attempt + root-bound mutation authority |
| F-C5.1a-01 | `CLOSED` | POSIX pinned directory fd；Windows retained directory handle without delete sharing；session binding；exact-window tests |

## 当前非阻塞 maintenance

| 来源 | 项目 | 状态 |
| --- | --- | --- |
| C5.0 post-merge audit F2 | manifest `schema_version` / `run.seed` bool alias | Open LOW |
| C5.0 post-merge audit F3 | standalone dry-run `verify` exit policy | Open LOW，需先决定契约 |
| C5.0 post-merge audit F4 | Stage 6.1 TOCTOU caveat 的历史措辞 | Open LOW documentation |
| Opus F-C5.1a-02 | 非目标 POSIX `dir_fd` capability fail-closed | Optional LOW；若探测，不能用 `os.replace in os.supports_dir_fd` |
| Opus F-C5.1a-03 | non-directory root 的异常分类 | Optional LOW |
| Opus F-C5.1a-07 | `os.open → os.fstat` 精确窗口无独立 hook | Optional LOW test coverage |

Opus 的 Windows pathname/share-mode、failed refresh 删除旧输出、generic writer 的非 canonical 用途等记录为架构说明或 INFO，不进入 active defect queue。

## 统一接受结果

```text
F-C5.1a-01: CLOSED
F1: CLOSED
H1: REMAINS CLOSED
H2: REMAINS CLOSED
H3: REMAINS CLOSED
Merge recommendation at be7cee21: GO
Latest production-bearing commit: 57ba4d1802a587deeb1a852c2ea10b5a5437a6fe
Current merge blockers: NONE
```

## 新审计的登记规则

每份新审计至少登记：reviewer/context、exact SHA、是否读到完整 production source、实际执行的 tests/probes、CI 读取层级、findings、verdict、限制，以及该报告是“新裁决”还是“仅提供证据”。环境受阻报告不得伪装为代码 failure；绿色 CI 也不得替代 production proof。
