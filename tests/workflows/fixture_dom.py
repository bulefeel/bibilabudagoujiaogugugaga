"""Tiny async DOM used to exercise saved HTML fixtures without a browser binary."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import Any


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    @property
    def text(self) -> str:
        return " ".join(
            item if isinstance(item, str) else item.text for item in self.children
        ).strip()


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {"meta", "input", "img", "br", "hr", "link"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].children.append(data)


class Locator:
    def __init__(self, nodes: list[Node]) -> None:
        self.nodes = nodes

    @property
    def first(self) -> "Locator":
        return Locator(self.nodes[:1])

    def nth(self, index: int) -> "Locator":
        return Locator([self.nodes[index]])

    async def count(self) -> int:
        return len(self.nodes)

    async def inner_text(self, **kwargs: Any) -> str:
        return self.nodes[0].text if self.nodes else ""

    async def get_attribute(self, name: str) -> str | None:
        return self.nodes[0].attrs.get(name) if self.nodes else None

    async def is_enabled(self) -> bool:
        return bool(self.nodes) and "disabled" not in self.nodes[0].attrs

    async def click(self, **kwargs: Any) -> None:
        if not self.nodes:
            raise RuntimeError("empty locator")

    async def evaluate(self, expression: str) -> str:
        if "childNodes" in expression:
            return " ".join(
                item for item in self.nodes[0].children if isinstance(item, str)
            )
        return render(self.nodes[0])

    def locator(self, selector: str) -> "Locator":
        result: list[Node] = []
        for node in self.nodes:
            result.extend(select(node, selector))
        return Locator(_unique(result))


class FixturePage:
    def __init__(self, html: str, url: str) -> None:
        parser = Parser()
        parser.feed(html)
        self.root = parser.root
        self.url = url

    def locator(self, selector: str) -> Locator:
        return Locator(select(self.root, selector))

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None


def select(root: Node, selector: str) -> list[Node]:
    selectors = [item.strip() for item in selector.split(",") if item.strip()]
    nodes = list(descendants(root))
    result: list[Node] = []
    for expression in selectors:
        if expression.startswith("xpath="):
            continue
        if ">" in expression:
            # The production balance parser deliberately requires direct
            # children so a nested/recent-payout row cannot be index-aligned
            # with the three balance cards.  Support that small CSS subset in
            # this browser-free fixture DOM as well.
            left, right = expression.rsplit(">", 1)
            parent_parts = left.strip().split()
            child = right.strip()
            for node in nodes:
                parent = node.parent
                if (
                    parent is not None
                    and parent_parts
                    and _matches(node, child)
                    and _matches(parent, parent_parts[-1])
                    and _ancestors_match(parent.parent, parent_parts[:-1])
                ):
                    result.append(node)
            continue
        parts = expression.split()
        for node in nodes:
            if _matches(node, parts[-1]) and _ancestors_match(node.parent, parts[:-1]):
                result.append(node)
    return _unique(result)


def descendants(node: Node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from descendants(child)


def _ancestors_match(parent: Node | None, parts: list[str]) -> bool:
    for part in reversed(parts):
        while parent is not None and not _matches(parent, part):
            parent = parent.parent
        if parent is None:
            return False
        parent = parent.parent
    return True


def _matches(node: Node, expression: str) -> bool:
    if expression == "*":
        return True
    tag_match = re.match(r"^[A-Za-z0-9_-]+", expression)
    if tag_match and node.tag != tag_match.group(0).lower():
        return False
    class_match = re.search(r"\.([A-Za-z0-9_-]+)", expression)
    if class_match and class_match.group(1) not in (node.attrs.get("class") or "").split():
        return False
    for attr, operator, value in re.findall(
        r'''\[([A-Za-z0-9_-]+)(?:(\*=|=)["']?([^\]"']+)["']?)?\]''',
        expression,
    ):
        actual = node.attrs.get(attr)
        if actual is None:
            return False
        if operator == "=" and str(actual) != value:
            return False
        if operator == "*=" and value not in str(actual):
            return False
    return True


def _unique(nodes: list[Node]) -> list[Node]:
    seen: set[int] = set()
    return [node for node in nodes if not (id(node) in seen or seen.add(id(node)))]


def render(node: Node) -> str:
    attrs = " ".join(
        key if value is None else f'{key}="{value}"' for key, value in node.attrs.items()
    )
    body = "".join(item if isinstance(item, str) else render(item) for item in node.children)
    return f"<{node.tag}{' ' + attrs if attrs else ''}>{body}</{node.tag}>"
