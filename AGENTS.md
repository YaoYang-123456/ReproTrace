# ReproTrace 项目协作约定

## 项目定位与 v0 范围

ReproTrace 是面向机器学习论文复现的 evidence-first 执行记录与验证工具。它记录可检查的证据链，不把进程成功退出等同于论文复现成功。

v0 只覆盖：

- 公开代码与公开数据；
- PyTorch 图像分类与 ViT PEFT 实验；
- 单机运行，先完成 CPU 验证，再进入 GPU 实验；
- 顺序执行、声明式输入与产物、CSV 或日志正则指标提取。

除非明确确认，不扩大上述范围。

## 核心原则

- **Evidence-first**：优先记录 source、environment、inputs、commands、logs、artifacts、metrics、verification 和 report 证据。
- 运行成功不等于论文复现成功；结论必须受 manifest 中声明的 protocol、指标和验证结果约束。
- 命令必须使用 argv 数组执行，不使用 `shell=True`。
- 通用指标提取不加载不可信的 pickle/PTH 文件。
- 不自动修复论文环境冲突；冲突必须被记录并交由人工决定。
- 敏感环境变量不得进入未脱敏的证据记录；大文件默认记录路径、大小和 SHA-256，不自动复制。

## 工作规则

每个阶段都遵循以下顺序：

1. 先检查工作树、分支、HEAD 和相关远程状态。
2. 只做小步、明确范围内的修改。
3. 修改后运行与改动相称的验证命令。
4. 记录实际证据、验证结果和已知问题。
5. 每个阶段独立提交并推送；提交范围必须经过 `git diff` 和 `git status` 核对。

涉及长时间实验时，先完成 dry-run 和环境/输入预检，再等待明确确认。

## 禁止事项

- 未经确认不启动长时间训练或 GPU 实验。
- 未经确认不扩大 v0 范围。
- 不把用户的 PEFT-ViT 审计分支 `7e5039ca0ed63ec196cb438b6ea33b7d3778c362` 当作论文官方实现；它只作为证据语料。
- 不把 `approximate` protocol 写成 `strict` reproduction。
- 不绕过验证、不删除失败证据，也不以单次成功运行宣称论文结论成立。

## 标准验证命令

Windows PowerShell 下优先使用仓库内被 `.gitignore` 忽略的隔离环境：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\reprotrace.exe --version
.\.venv\Scripts\reprotrace.exe run examples/tiny/reprotrace.yaml
```

对已有 evidence bundle 继续执行：

```powershell
.\.venv\Scripts\reprotrace.exe verify <run-dir>
.\.venv\Scripts\reprotrace.exe report <run-dir>
.\.venv\Scripts\reprotrace.exe diff <run-a> <run-b>
```

提交前至少检查：

```powershell
git diff --check
git status --short
git diff --stat
```

真实实验必须先使用 `run --dry-run` 完成预检；未经确认不得启动 PEFT-ViT 或 GPU 训练。

## 项目会话与信息同步

### 单一主会话

- ReproTrace 默认只保留一个项目主会话。主会话维护当前事实、已批准决策、实施进度与最终交接。
- 独立审计、对抗复核或隔离实验可以开卫星会话，但卫星会话不承载项目主线，也不直接改变项目状态。
- 新聊天中的旧计划、模型建议或草案不是批准记录；只有经主会话核对并写入项目状态的事项才可作为当前工作。

### Web 与 Codex App 分工

- Web 主会话负责需求澄清、设计、计划、任务书、独立审查、证据裁决与最终决策；默认不直接修改项目文件，也不执行 commit、push、PR 或 CI 操作。
- Codex App 负责仓库检查、文件修改、测试、commit、push、PR 创建与 CI 跟进；所有实现工作应在同一可访问本地仓库与 GitHub 凭据的环境中完成。
- Web 向 Codex App 交付自包含任务书，明确仓库、base、分支、允许修改的文件、验收标准、禁止事项和停止条件。
- Codex App 完成后必须返回 exact commit、实际 diff、验证命令与结果、远端分支、PR/CI 链接、工作树状态以及任何未完成项；Web 再据此独立审查和决定是否合并。

### 权威状态文件

- `docs/project-status.md`：只记录当前有效状态、活动 blocker、已批准下一步与最新验证基线。
- `docs/audit-ledger.md`：记录历史审计对象、证据等级、当时结论、后续 remediation 与最终裁决。
- `AGENTS.md`：记录长期稳定的协作、验证、安全边界与交接规则。
- 被后续提交取代的 detailed chronology 留在 Git 历史或专门 acceptance 文档中，不继续堆叠到 current-status 顶层。

### 会话启动顺序

每次开始 ReproTrace 工作时：

1. 读取 `AGENTS.md`；
2. 读取 `docs/project-status.md`；
3. 读取 `docs/audit-ledger.md`；
4. 核对工作树、分支、HEAD、远程 `main` 与目标 PR；
5. 明确本轮 exact review/change object、授权范围与非目标；
6. 再开始审查、设计或修改。

若仓库动态事实与文档不一致，以核实后的 Git/GitHub 状态为准，并先修正同步记录。

### 卫星会话返回包

独立审计或隔离实验结束时，必须向主会话返回：

1. exact repository / base / head / merge object；
2. 实际读取的 production files 与调用链；
3. 实际执行的 tests、probes、CI jobs/logs；
4. production-direct、test-supported、CI-supported、evidence-only 的明确区分；
5. findings、severity、status、verdict 与 non-goals；
6. 环境失败、skip、未验证项与 fallback；
7. 生成的报告或附件的稳定文件名。

主会话必须独立核对该返回包，再更新 audit ledger。多个模型的结论不按票数决定，也不因措辞更长而自动获得更高权重；权重取决于 exact object、production proof 与可复核 evidence。

### 上下文接近上限时

- 不在未同步状态下直接换新会话。
- 先更新 current status、audit ledger 与本轮 handoff。
- handoff 至少包含：当前 HEAD、clean/dirty 状态、已完成、未完成、活动 findings、最后验证结果、下一条安全动作。
- 新主会话按“会话启动顺序”恢复，不依赖模型对旧聊天的隐式记忆。

### 远程写入边界

- 状态同步、审计与规划默认只读。
- 创建分支、commit、push、修改 PR、合并或启动长时间/GPU 实验，均需与本轮明确授权一致。
- 独立审计会话不得修改被审对象；必要的 probes 只在隔离 checkout 或临时目录中执行。

### 文件命名

- 审计报告使用稳定、可区分的名称，至少包含 milestone 与 review type；避免继续积累“粘贴的 markdown”或仅带 `(1)` 的副本。
- 推荐格式：`ReproTrace_<milestone>_<review-type>_<date>.md`。
- 同一报告的 addendum 使用明确序号，并引用 companion report 与 exact reviewed SHA。
