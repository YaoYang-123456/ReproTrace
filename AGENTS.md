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
