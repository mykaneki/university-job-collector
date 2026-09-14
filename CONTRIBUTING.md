# 贡献指南

感谢你改进高校就业网招聘信息采集器。提交贡献即表示你有权提交相关代码、文档和测试材料，并同意贡献按本仓库的 Apache License 2.0 发布。

## 开始之前

1. 先查看现有 issue 和 collector，避免重复适配。
2. 核对目标网站的服务条款、robots.txt、版权声明和自动访问规则。
3. 只处理匿名公开页面。登录、验证码、校园网专属内容和其他明确受限资源不在采集范围内。
4. 新网站优先使用普通 HTTP。浏览器只用于 HTTP 无法稳定取得匿名公开数据的场景。

## Collector 约定

- 一所高校一个 `collectors/<school>.py`，对外提供 `collect(start, end, filters, raw_dir)`。
- 网站字段、分页、筛选编码、selector 和访问策略封装在学校自己的 collector 中。
- 保存网站返回的完整 Raw；Simple JSON 严格只含“公司、岗位、岗位上新时间、JD、投递方式、原始链接”六个字段。
- 筛选优先使用网站原生能力，其次使用明确字段做本地确定性筛选。无法可靠判断的字段声明为 `unsupported`。
- 详情失败不应终止整校列表采集。登录或权限受限应显式记录，相关字段保持空字符串。
- 保持串行和低频请求。新增重试需要有明确上限。

## 测试数据

- 测试 fixture 使用合成名称、`example.com` 域名和虚构联系方式。
- 不提交真实 Raw、Simple 输出、Cookie、Token、浏览器 profile、截图中的个人信息或来源网站完整正文。
- 复现问题所需的真实响应应先最小化和去标识化，只保留证明问题所需的结构。

## 本地验证

在项目虚拟环境中安装依赖并运行测试：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-browser.txt
.venv/bin/python -m unittest discover -s tests -v
```

涉及真实网站的验证应使用明确的短时间范围和低请求频率。请在 PR 中写明验证日期、网络条件、列表与详情的公开访问状态，以及未验证内容。不要附上采集产物。

## Pull Request 清单

- [ ] 改动范围聚焦，一校逻辑保留在对应 collector。
- [ ] 新增或更新了单元测试，fixture 不含真实个人信息和完整招聘正文。
- [ ] 已运行受影响测试并记录结果。
- [ ] 已说明公开访问边界、筛选能力和失败行为。
- [ ] 未提交 `output/`、`.diagnostics/`、`.env*`、Cookie、Token 或浏览器 profile。
- [ ] 引入的新依赖已记录许可证，并更新 `THIRD_PARTY_NOTICES.md`。

安全问题和可能泄露的凭据或个人信息请按 [SECURITY.md](SECURITY.md) 私密报告。
