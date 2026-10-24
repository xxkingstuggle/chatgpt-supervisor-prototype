# 项目指引

## 项目定位
为 Codex 管理长期目标的轮次、协议校验、恢复状态和报告；真实浏览器动作仍由 Codex 工具执行。

## 如何运行
用 python3 skill/chatgpt-supervisor/scripts/supervisor.py --help 查看 CLI；离线测试使用 python3 -m unittest discover -s tests。

## 技术栈
Python 标准库 CLI 与 unittest；浏览器控制不由仓库代码导入或模拟为真实操作。

## 目录与约定
技能内容在 skill/chatgpt-supervisor/，离线测试在 tests/；.agent-work 保存项目运行状态，不能提交。

## 当前状态与下一步
以 SKILL.md、references/ 和 CLI 行为为准。调整状态流转时同时核对协议文档与离线测试；模拟证据必须标为 simulated。
