from __future__ import annotations

import re
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup, Tag


EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"(?![A-Za-z0-9.-])"
)
URL_RE = re.compile(
    r"https?://[A-Za-z0-9.-]+(?::\d+)?"
    r"(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.IGNORECASE,
)
APPLICATION_WORDS = "投递|简历|网申|申请|报名|应聘|招聘邮箱"
PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _extract_emails(value: str) -> list[str]:
    # A malformed mailto/URL can contain a percent-encoded sentence immediately
    # before a real address.  Since ``%`` is legal in an RFC email local part,
    # the general regex would otherwise swallow that URL fragment as part of the
    # address.  URI values are decoded at their call site; encoded fragments in
    # ordinary text are not evidence of a visible, explicit email address.
    return [email for email in EMAIL_RE.findall(value) if not PERCENT_ESCAPE_RE.search(email)]


def html_to_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    lines = []
    for line in soup.get_text("\n").splitlines():
        normalized = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def absolute_url(base: str, value: str) -> str:
    return urljoin(base, value)


def extract_application_methods(
    html_value: str,
    *,
    explicit_email: str = "",
    explicit_url: str = "",
) -> str:
    methods: list[str] = []

    def add(label: str, value: str) -> None:
        value = value.strip().rstrip(".,;，；。")
        rendered = f"{label}：{value}"
        if value and rendered not in methods:
            methods.append(rendered)

    if explicit_email:
        for email in _extract_emails(explicit_email):
            add("邮箱", email)
    if explicit_url:
        add("网申地址", explicit_url)

    text = html_to_text(html_value)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        # Recruitment pages often put "投递通道" in one paragraph and the
        # actual URL/email in the following paragraph. A short preceding-text
        # window keeps this deterministic without treating every page URL as an
        # application method.
        context = " ".join(lines[max(0, index - 5) : index + 1])
        if re.search(APPLICATION_WORDS, context, re.IGNORECASE):
            for email in _extract_emails(line):
                add("邮箱", email)
            for url in URL_RE.findall(line):
                add("网申地址", url)

    soup = BeautifulSoup(html_value or "", "lxml")
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        context = " ".join(anchor.get_text(" ", strip=True).split())
        parent_text = " ".join(anchor.parent.get_text(" ", strip=True).split()) if isinstance(anchor.parent, Tag) else context
        preceding_text = " ".join(
            reversed([" ".join(str(value).split()) for value in anchor.find_all_previous(string=True, limit=12)])
        )
        if href.lower().startswith("mailto:"):
            mailto_value = unquote(href[7:].split("?", 1)[0])
            for email in _extract_emails(mailto_value):
                add("邮箱", email)
        elif href.lower().startswith(("http://", "https://")) and re.search(
            APPLICATION_WORDS, f"{preceding_text} {context} {parent_text}"
        ):
            add("网申地址", href)
    return "\n".join(methods)
