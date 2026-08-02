# -*- coding: utf-8 -*-
"""Parser for mailbox-specific iCloud HTML pickup pages."""
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class ICloudPickupPageError(ValueError):
    pass


def with_message_limit(url: str, limit: int = 10) -> str:
    parsed = urlsplit(str(url or "").strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "n"
    ]
    query.append(("n", str(max(1, int(limit)))))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _clean_text(parts: list[str]) -> str:
    value = " ".join(part.strip() for part in parts if part and part.strip())
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", value)


class _PickupPageParser(HTMLParser):
    _FIELD_CLASSES = {
        "fr": "from",
        "su": "subject",
        "dt": "date",
        "bd": "body",
        "to": "to",
        "rcpt": "to",
        "recipient": "to",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.current: dict[str, list[str]] | None = None
        self.cards: list[dict] = []
        self.count_parts: list[str] = []
        self.message_count: int | None = None
        self.saw_empty_state = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        is_card = "card" in classes
        if is_card:
            self.current = {name: [] for name in self._FIELD_CLASSES.values()}
        field = next((name for css, name in self._FIELD_CLASSES.items() if css in classes), None)
        frame = {
            "tag": tag,
            "is_card": is_card,
            "field": field,
            "is_count": "cnt" in classes,
            "is_empty": "no" in classes,
            "is_title": tag.lower() == "title",
        }
        if frame["is_empty"]:
            self.saw_empty_state = True
        self.stack.append(frame)
        if tag.lower() == "br":
            self._append_text(" ")

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str):
        while self.stack:
            frame = self.stack.pop()
            if frame["is_card"]:
                self._finish_card()
            if frame["tag"] == tag:
                break

    def handle_data(self, data: str):
        self._append_text(data)

    def close(self):
        super().close()
        while self.stack:
            frame = self.stack.pop()
            if frame["is_card"]:
                self._finish_card()
        count_text = _clean_text(self.count_parts)
        matched = re.search(r"\d+", count_text)
        if matched:
            self.message_count = int(matched.group(0))

    def _append_text(self, data: str):
        if not data:
            return
        if any(frame["is_count"] for frame in self.stack):
            self.count_parts.append(data)
        if any(frame["is_title"] for frame in self.stack):
            self.title_parts.append(data)
        if self.current is None:
            return
        field = next(
            (frame["field"] for frame in reversed(self.stack) if frame["field"]),
            None,
        )
        if field:
            self.current[field].append(data)

    def _finish_card(self):
        if self.current is None:
            return
        card = {key: _clean_text(parts) for key, parts in self.current.items()}
        if not card.get("to"):
            card.pop("to", None)
        self.cards.append(card)
        self.current = None


def parse_pickup_page(html_text: str, expected_email: str = "") -> list[dict]:
    parser = _PickupPageParser()
    parser.feed(str(html_text or ""))
    parser.close()
    expected = str(expected_email or "").strip().lower()
    title = _clean_text(parser.title_parts).lower()
    mailbox_match = re.search(r"[^\s<>]+@[^\s<>]+", title)
    page_mailbox = mailbox_match.group(0).strip(".,;:()[]{}") if mailbox_match else ""
    if expected and page_mailbox and page_mailbox != expected:
        raise ICloudPickupPageError(
            f"独立取件页面邮箱不匹配: expected={expected}, actual={page_mailbox}"
        )
    if expected:
        for card in parser.cards:
            recipient = str(card.get("to") or "").strip().lower()
            if recipient and expected not in recipient:
                raise ICloudPickupPageError(
                    f"独立取件邮件收件人不匹配: expected={expected}"
                )
    if parser.cards:
        return parser.cards
    if parser.saw_empty_state or parser.message_count == 0:
        return []
    raise ICloudPickupPageError("独立取件页面结构无法识别")
