# Contributing to Satrap

感谢你愿意参与 Satrap 的开发。

Satrap 目前仍处于早期开发阶段，项目结构、功能设计和 API 都可能继续调整。

## 开始之前

对于小型修复、文档修改和明确的问题，可以直接提交 Pull Request。

对于以下类型的修改，建议先创建 Issue 或在已有 Issue 中讨论：

- 新增较大的功能
- 修改核心架构或执行流程
- 修改 Edictum / Session / Plugin 等核心抽象
- 新增或大幅修改平台适配器
- 大规模重构
- 可能影响现有行为的设计更改
- 具体代码规范参见[开发规范](docs/development/development-guidelines.md)

如果已有相关 Issue，请尽量在 PR 中关联它。

## 开发环境

### Backend

Satrap 需要 Python 3.10 或更高版本。

克隆仓库后安装开发版本：

```bash
git clone https://github.com/liminalselves/Satrap.git
cd Satrap
pip install -e .[all]
```

### Frontend

前端位于 `satrap-ui/` 目录下：

```bash
cd satrap-ui
npm install
npm run dev
```

## 开发原则

请尽量遵循现有代码结构和命名风格。

修改代码时建议：

- 保持改动范围明确，避免在同一个 PR 中混入无关修改
- 修复 Bug 时尽可能补充对应测试
- 新增功能时考虑同步更新相关文档
- 新功能优先复用现有 Session、Plugin、Edictum、Platform 等抽象，而不是建立平行实现

如果现有设计无法合理支持需求，可以提出新的设计，不要求为了兼容现有结构而强行绕过问题。

### Pull Request

建议从 `main` 创建独立分支。<br>
完成修改后提交 Pull Request。

PR 描述应尽量说明：

1. 修改了什么
2. 为什么需要修改
3. 如何验证修改
4. 是否存在已知限制或后续工作
5. 关联的 Issue（如果有）

对于 UI 修改，建议附上截图或录屏。<br>
对于行为变化较大的修改，建议同时给出简单的使用示例。

### Commit

目前不强制使用特定的 Commit Message 规范。<br>
一个 PR 可以包含多个 commit，不要求为了形式强行 squash。<br>
在合并前维护者可能根据情况整理提交历史。

### Tests

在提交 PR 之前，建议运行所有测试。<br>
需要真实 API、模型或第三方服务的测试，请不要把凭据提交到仓库。

### Docs

如果修改会影响用户行为或 API 设计，建议同时更新相关文档。<br>
纯内部实现调整通常不要求修改文档。

### Issues

对 Issues 没有强制性的格式要求。<br>
对于任何 Issues，建议在在 label 中选择合适的标签。<br>
建议分成问题、目标、现状、需求等部分分别阐述。

### License

向 Satrap 提交代码即表示你同意你的贡献按照项目当前的[GPL-3.0](LICENSE)许可证进行分发。
