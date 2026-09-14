# 第三方依赖与许可说明

本项目通过 `requirements.txt` 和 `requirements-browser.txt` 安装第三方 Python 包。仓库当前不内置这些包、Google Chrome 或 Playwright 下载的浏览器二进制。

## 直接依赖

| 依赖 | 用途 | 许可证 | 上游项目 |
| --- | --- | --- | --- |
| httpx | HTTP 客户端 | BSD-3-Clause | https://github.com/encode/httpx |
| beautifulsoup4 | HTML 解析 | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| lxml | HTML/XML 解析器 | BSD-3-Clause；发行包还包含上游 `LICENSES.txt` 所列组件许可 | https://github.com/lxml/lxml |
| pycryptodome | 解析站点公开客户端使用的加密响应格式 | BSD-2-Clause / Public Domain 组成 | https://github.com/Legrandin/pycryptodome |
| playwright | 浏览器自动化与 CDP 接管 | Apache-2.0 | https://github.com/microsoft/playwright-python |

每个依赖的实际版本由安装时的版本约束解析。许可证信息以所安装版本自带的 metadata 和上游许可证文件为准。

## 传递依赖和浏览器

上述包会安装传递依赖，其中可能包含 MIT、BSD、PSF、MPL-2.0 等许可证。部分 wheel 还会内置原生组件，例如 lxml wheel 中的 libxml2、libxslt、libexslt、zlib 或 iconv；实际组成以所安装制品为准。源码仓库只引用依赖名称。发布容器、可执行文件、离线依赖包或其他含第三方代码的制品时，发布者需要基于实际安装版本重新生成完整依赖与内置组件清单，保留相应许可证和通知，并履行对应义务。

Google Chrome 是外部安装的软件，受其自身条款约束。本项目只通过配置引用本机可执行文件，不随仓库分发 Chrome。Playwright 安装的 Chromium 及其组件带有各自许可证和第三方通知；将这些二进制打包进制品时，应同时提供随该版本附带的许可证材料。

本项目的 [Apache-2.0 许可证](LICENSE) 不替代任何第三方许可证，也不授权高校网站、招聘单位或平台的内容和商标。
