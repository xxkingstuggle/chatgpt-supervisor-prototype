# ChatGPT Supervisor

这是一个配合 Codex 使用的本地监督工具原型：它把长期目标拆成有边界的轮次，维护请求、回复和报告之间的关联，并在执行前检查协议状态。真实浏览器操作由 Codex 的浏览器工具完成；仓库中的 Python CLI 不会代替浏览器，也不授予额外权限。

## 开始使用

查看 CLI 用法：

    python3 skill/chatgpt-supervisor/scripts/supervisor.py --help

先阅读 skill/chatgpt-supervisor/SKILL.md，再按任务查阅其中的 protocol、browser 和 recovery 参考文档。

## 目录

- skill/chatgpt-supervisor/：技能说明、协议参考、模板与本地状态 CLI
- tests/：离线协议与文件布局测试

真实浏览器会话及其 .agent-work 状态属于本地运行数据，不要提交 cookies、凭据、未脱敏日志或会话目录。
