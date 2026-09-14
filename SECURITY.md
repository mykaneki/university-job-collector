# 安全政策

## 私密报告

GitHub 的 Private Vulnerability Reporting 面向公开仓库。仓库转为公开后，维护者应立即按 [GitHub 配置说明](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) 启用该功能，并确认「Report a vulnerability」入口可用。功能启用后，凭据、Cookie、Token、浏览器 profile、个人信息泄露，以及可能扩大受限页面访问范围的问题，请使用项目的 [Private Vulnerability Reporting](https://github.com/mykaneki/university-job-collector/security/advisories/new) 私密提交。

报告中请包含：

- 受影响文件、collector 或版本；
- 可复现的最小步骤；
- 影响范围；
- 已采取的临时保护措施；
- 去标识化后的必要证据。

请勿在公开 issue、Pull Request、讨论区或日志中粘贴凭据、Cookie、真实个人信息、完整 Raw 或浏览器 profile。维护者会根据报告内容评估影响和处理方式；项目不承诺固定响应时限。

## 公开问题

普通解析错误、公开页面结构变化和不含敏感信息的兼容问题可以提交公开 issue。请使用合成数据或经过最小化、去标识化的片段复现。

来源网站出现登录、验证码、访问拒绝或权利人要求停止时，应先停用对应采集路径。相关情况可在移除敏感信息后公开说明。
