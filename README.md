# 高校就业网招聘信息采集

本项目只采集高校就业网公开招聘信息，同时保存网站原始响应 Raw 和严格六字段 Simple JSON。当前已为导航材料中的 46 所高校建立独立 collector；受公开权限、校园网、WAF 或当前网络出口限制的学校会明确标记为部分覆盖或运行失败。

本项目是独立的开源工具，与文中提及的高校、就业平台和招聘单位没有隶属、授权、合作或背书关系。高校、平台和企业名称及商标归各自权利人所有，仅用于说明数据来源和采集器适配范围。

## 环境

代码要求 Python 3.10 或更高版本，当前验收环境为 Python 3.14。使用项目内虚拟环境，不安装全局依赖：

```bash
cd university-job-collector
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

大部分生产采集不依赖浏览器。探索新高校，或运行已确认必须由浏览器渲染的 collector 时，安装 Playwright：

```bash
.venv/bin/python -m pip install -r requirements-browser.txt
.venv/bin/playwright install chromium
```

中科大、川大、西南石油、重大、西工大和兰大使用专用的可见 Google Chrome profile，由 Playwright 通过 CDP 接管。该路径当前在 macOS 验证，依赖 Unix 的 `fcntl` 文件锁；运行机需安装 Google Chrome 并提供图形环境。profile 有排他锁，同一时刻只允许一个采集任务使用。其他系统需通过 `UJC_CHROME_EXECUTABLE` 指定 Chrome 路径，并自行验证兼容性；其余参数可通过 `UJC_CHROME_PROFILE_DIR`、`UJC_CHROME_PROFILE_NAME`、`UJC_CHROME_DEBUG_HOST`、`UJC_CHROME_START_TIMEOUT` 调整。`UJC_CHROME_DEBUG_HOST` 只接受 `localhost` 或 `127.0.0.0/8` 回环地址，CDP 端口由 Chrome 自动分配，不接受固定端口。`UJC_CHROME_PROFILE_DIR` 必须是采集专用目录，`UJC_CHROME_PROFILE_NAME` 只能是单个目录名；程序会拒绝常见的日常 Chrome 用户数据目录及指向它们的软链接。

## 采集

```bash
.venv/bin/python collect.py \
  --schools pku,tsinghua,ruc,buaa,bit,bnu,ustb,cau,uibe,bjfu,cup,nankai,tju,sjtu,fudan,tongji,ecnu,nju,seu,suda,zju,ustc,sdu,ouc,zzu,hust,whu,wut,csu,hnu,sysu,scut,xmu,uestc,scu,swpu,cqu,xjtu,xidian,nwpu,chd,jlu,dlut,hit,hrbeu,lzu \
  --start "2026-09-06 00:00:00" \
  --end "2026-09-08 23:59:59"
```

筛选参数可重复传入：

```bash
.venv/bin/python collect.py \
  --schools pku,tsinghua \
  --start "2026-09-01" \
  --end "2026-09-08 23:59:59" \
  --filter company="中国移动"
```

仅传日期时，`start` 解释为当日 `00:00:00`，`end` 解释为当日 `23:59:59`；时区统一为 `Asia/Shanghai`。任意学校不支持的筛选会在发起网络请求前明确报错，不会静默忽略。

## 输出

每次运行都会创建独立时间戳目录：

```text
output/YYYYMMDD_HHMMSS/
├── raw/
│   └── <school>/
├── simple/
│   ├── <school>.json
│   └── all.json
└── run_summary.json
```

Raw 列表和详情保存网站返回的完整响应体；每个 Raw 文件有同名 `.meta.json`，记录 URL、方法、状态码、Content-Type 和采集时间。Simple JSON 始终严格只含：

```text
公司、岗位、岗位上新时间、JD、投递方式、原始链接
```

Raw 和 Simple 都可能包含第三方招聘正文、图片、姓名、邮箱、电话或其他公开联系信息。它们是运行时生成物，不属于本项目 Apache-2.0 代码许可证的授权范围，也不随仓库发布。保存、使用、共享和删除生成数据时，请遵守来源网站规则和适用法律。详细边界见 [数据使用政策](DATA_POLICY.md)。

## 公开示例

`examples/simple.synthetic.json` 是一条完全合成的严格六字段 Simple JSON，用于演示输出格式和下游解析。其公司、岗位、正文和 `.invalid` 域名都不对应真实招聘记录。

`examples/run_summary.sanitized.json` 从一次验收运行中只保留时间范围、学校策略、页数、数量和状态。文件已移除本机路径、错误正文、联系方式、token、Raw 内容和招聘正文。

`output/` 继续由 `.gitignore` 排除。该目录包含完整 Raw、JD、招聘联系方式、页面图片和运行环境信息，仅用于本地验证和排查。

## 当前策略与筛选能力

- 北京大学：列表和详情均为匿名 JSON API。`position`、`keyword`、`company_nature`、`industry` 为原生筛选，`company` 为列表明确字段的本地筛选。`position` 与 `keyword` 共用站内 `title` 参数，不能同时传不同值。
- 清华大学：列表为匿名 POST HTML，详情为匿名 GET HTML。除 `keyword` 在标题+公司上本地确定性过滤外，其余已声明字段均是站内原生筛选。人类可读选项由清华采集器转为站内编码。
- 北航、北理、北师大、北科大、中国农大、北林：各自独立的匿名 JSON API 采集器；`position`/`keyword`、`company_nature`、`industry` 为原生筛选，`company` 为本地筛选。编码映射每次从本校公开筛选字典 API 读取。
- 中国人民大学：匿名 JSON API 提供职位列表和明确字段，已声明的七项筛选均为本地确定性筛选。详情路由进入学生登录态，不绕过权限，JD 与投递方式留空。
- 对外经贸：列表为匿名 POST，响应按站内公开 JavaScript 规则做 AES-CBC 解密；详情为公开 HTML。页面的单位搜索字段当前对实际单位名返回 0 条，因此 `company`、`position`、`keyword`、`location` 均使用列表明确字段做本地筛选。
- 中国石大（北京）：列表和详情均为 GET HTML，页面内容按站内公开 JavaScript 规则解开 zlib/Base64 嵌入块。`company`、`position`、`keyword` 为本地筛选。
- 南开大学：列表为匿名 POST HTML，详情为匿名 GET HTML。`keyword`、`location`、`position_type` 使用站内原生筛选，`company`、`position`、`education` 使用列表明确字段本地筛选。
- 天津大学：完整招聘列表和详情需登录，不绕过权限。生产脚本只保存公开首页“招聘简章”记录，并在运行统计中明确标记 `coverage=partial`；`company`、`position`、`keyword` 为本地筛选。
- 上海交通大学：完整招聘列表 API 匿名访问返回 403；生产脚本采集公开首页招聘公告，并调用匿名详情 JSON API，统计中明确标记 `coverage=partial`。已声明筛选均基于首页或详情明确字段做本地确定性过滤。
- 复旦大学：当前本机网络访问站点返回 HTTP 502，浏览器错误页明确显示当前 IPv6 出口没有对应发布资源。采集器保存完整错误响应并让本校运行失败，不输出伪造的空成功结果；网络恢复后须重新探索列表与详情协议。
- 同济大学：列表和详情均为匿名 JSON API。`position`、`keyword`、`company_nature`、`industry` 为原生筛选，`company`、`education` 为明确字段的本地筛选。
- 华东师范大学：完整职位列表 API 匿名访问返回 403；生产脚本采集公开首页当前职位区块，并调用匿名详情 JSON API，统计中明确标记 `coverage=partial`。除 `student_type` 外，已声明筛选均基于明确字段本地过滤。
- 南京大学：列表和详情均为匿名 JSON API。API 数据混有迁移记录，不能依赖逐页时间单调性，采集器按 `totalPages` 遍历后过滤。`company`、`position`、`keyword`、`location`、`company_nature`、`industry`、`education`、`employment_type` 均基于明确字段本地过滤。
- 东南大学、苏州大学：当前本机境内 IPv4 出口访问 91job 分站不稳定；多数请求被创宇盾规则 `80001` 返回 HTTP 403，偶尔只能取得 200 的 SPA 外壳，而后续静态资源仍被拦截。采集器保存实际响应并明确失败；稳定访问后须重新探索列表、分页、筛选和详情协议。
- 浙大使用匿名 JSON API；山大使用状态化 GET HTML；中国海大使用 jqGrid POST JSON 列表和 GET HTML 详情；郑大使用 Playwright 渲染兜底。中科大裸 HTTP 调用列表 API 返回 `error`，但专用可见 Chrome profile 中的同站请求可正常分页；生产使用外部 Chrome + CDP，保存真实 XHR 响应及渲染 DOM。
- 华中科大使用 GET HTML；武大使用匿名 JSON API；武汉理工使用公开前端的 Session lock + JSON API；中南、中山、厦大使用服务端 HTML 中的 zlib/Base64 内嵌内容；湖大使用 JSON 列表 + HTML 详情。华南理工公开入口强制跳转 CAS，不绕过。
- 电子科大使用 JSON API；西交使用云就业 JSON API；西电使用内嵌 HTML；长安大学列表无发布时间，因此逐条请求详情后按发布日期过滤。川大、西南石油、重大和西工大使用专用可见 Chrome + CDP 保存渲染 DOM；西工大首导航可能报 412，但仅在完整列表 DOM 实际出现时才继续采集。
- 吉大使用 GET HTML，兼容站点实际返回的中英文日期；大工使用云就业 JSON API；哈工大使用独立的匿名列表/详情 JSON API。哈工程使用正确的云就业列表 API，公开列表可采，详情跳转学生登录时保留列表数据并将 JD/投递方式留空；兰大使用专用可见 Chrome + CDP 采集公开列表和详情。

配置统一保守标记 `requires_domestic_ip=true`。复旦、东南、苏州和华南理工仍明确受网络或登录限制；哈工程详情受登录限制。使用专用 Chrome profile 的站点对出口和浏览器环境敏感，部署到其他机器时应重新验证。

## 探索新高校

```bash
.venv/bin/python -m discovery.inspect_http URL
.venv/bin/python -m discovery.inspect_browser URL --seconds 60
```

`discovery/` 只用于接入时确认真实请求，不被定期采集引用。浏览器探针默认只输出 POST 请求体是否存在及字节数，URL 中的常见凭据和个人信息查询参数会自动遮蔽。`--show-post-data` 会输出原始请求体，仅应用于已确认不含凭据或个人信息的公开页面。

## 合规与访问边界

- 只访问无需账号即可浏览的招聘页面和接口。遇到登录、验证码、明确权限提示或访问拒绝时，采集器应停止对应路径并记录受限状态。
- 使用前检查目标网站当前的服务条款、robots.txt、版权声明和自动访问规则。公开可见只说明页面的访问状态，不构成复制、汇编或再分发授权。
- HTTP 请求默认按单客户端串行发送，请求间隔为 0.3～0.6 秒；429、部分 5xx 和网络错误最多重试两次。浏览器采集器同样按页和详情顺序执行。部署方应结合站点要求进一步降低频率，并在站点要求停止时立即停用对应采集器。
- 项目不提供登录凭据，不处理验证码，不绕过账号权限。专用 Chrome/CDP 仅用于加载匿名浏览器可见的页面；它不赋予额外访问权限。
- 学校网站、接口、访问策略和网络条件会变化。README 与导航资料记录的是最近一次验证结果，运行结果以当次 Raw、metadata 和 `run_summary.json` 为准。
- 请勿把本项目用于批量发送营销信息、建立个人联系信息库、干扰网站正常运行或其他超出招聘信息公开目的的用途。

生成数据的最小化、留存和披露要求见 [DATA_POLICY.md](DATA_POLICY.md)。发现凭据、个人信息或访问控制相关问题，请按 [SECURITY.md](SECURITY.md) 私密报告。

## 测试

完整测试集会导入浏览器公共模块，因此先安装包含 Playwright Python 包的浏览器依赖；运行单元测试无需下载 Chromium：

```bash
.venv/bin/python -m pip install -r requirements-browser.txt
.venv/bin/python -m unittest discover -s tests -v
```

## 贡献

提交新高校适配或修复前，请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。第三方依赖及其许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

本项目原创代码和文档采用 [Apache License 2.0](LICENSE) 许可。该许可证不覆盖运行时取得的高校网页、API 响应、招聘正文、图片、个人信息、商标或其他第三方内容。第三方内容的权利和使用条件由其各自权利人及来源网站决定。
