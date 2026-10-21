"""UIAutomator hierarchy parsing and element normalization (PRD 9.3)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from phonepilot.core.coords import px_to_norm

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
PASSWORD_MASK = "••••"
MAX_ELEMENTS_DEFAULT = 150


@dataclass
class RawNode:
    cls: str
    text: str
    desc: str
    resource_id: str
    package: str
    hint: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    long_clickable: bool
    checkable: bool
    checked: bool
    enabled: bool
    focusable: bool
    focused: bool
    scrollable: bool
    selected: bool
    password: bool
    visible: bool
    depth: int
    children: list[RawNode] = field(default_factory=list)
    parent: RawNode | None = field(default=None, repr=False)

    @property
    def area(self) -> int:
        x0, y0, x1, y1 = self.bounds
        return max(x1 - x0, 0) * max(y1 - y0, 0)

    @property
    def interactive(self) -> bool:
        return self.clickable or self.long_clickable or self.checkable or self.scrollable or self.is_text_field

    @property
    def is_text_field(self) -> bool:
        return "EditText" in self.cls or "AutoCompleteTextView" in self.cls


@dataclass
class UIElement:
    id: int
    stable_key: str
    role: str
    label: str
    text: str
    desc: str
    resource_id: str
    cls: str
    package: str
    bounds: tuple[int, int, int, int]
    bounds_norm: tuple[int, int, int, int]
    clickable: bool
    enabled: bool
    focused: bool
    checked: bool | None
    scrollable: bool
    password: bool
    hint: str = ""

    @property
    def center(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.bounds
        return (x0 + x1) // 2, (y0 + y1) // 2

    def state_words(self) -> str:
        words = []
        if self.focused:
            words.append("focused")
        if self.checked is True:
            words.append("checked")
        elif self.checked is False:
            words.append("unchecked")
        if not self.enabled:
            words.append("disabled")
        if self.password:
            words.append("password")
        if self.scrollable:
            words.append("scrollable")
        return " ".join(words)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "label": self.label,
            "state": self.state_words(),
            "resource_id": self.resource_id,
            "bounds_norm": list(self.bounds_norm),
        }


@dataclass
class ParsedTree:
    root: RawNode | None
    nodes: list[RawNode]
    width: int
    height: int


def _bool(v: str | None) -> bool:
    return v == "true"


def parse_xml(xml: str) -> ParsedTree:
    xml = xml.strip()
    start = xml.find("<?xml")
    if start < 0:
        start = xml.find("<hierarchy")
    end = xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise ValueError("not a uiautomator hierarchy")
    xml = xml[start : end + len("</hierarchy>")]
    root_el = ET.fromstring(xml.encode("utf-8"))
    nodes: list[RawNode] = []

    def walk(el: ET.Element, depth: int, parent: RawNode | None) -> RawNode | None:
        node: RawNode | None = None
        if el.tag == "node":
            m = _BOUNDS.match(el.get("bounds", ""))
            bounds = tuple(int(g) for g in m.groups()) if m else (0, 0, 0, 0)
            node = RawNode(
                cls=el.get("class", ""),
                text=el.get("text", "") or "",
                desc=el.get("content-desc", "") or "",
                resource_id=el.get("resource-id", "") or "",
                package=el.get("package", "") or "",
                hint=el.get("hint", "") or "",
                bounds=bounds,  # type: ignore[arg-type]
                clickable=_bool(el.get("clickable")),
                long_clickable=_bool(el.get("long-clickable")),
                checkable=_bool(el.get("checkable")),
                checked=_bool(el.get("checked")),
                enabled=el.get("enabled", "true") == "true",
                focusable=_bool(el.get("focusable")),
                focused=_bool(el.get("focused")),
                scrollable=_bool(el.get("scrollable")),
                selected=_bool(el.get("selected")),
                password=_bool(el.get("password")),
                visible=el.get("visible-to-user", "true") == "true",
                depth=depth,
                parent=parent,
            )
            nodes.append(node)
            if parent is not None:
                parent.children.append(node)
        for child in el:
            walk(child, depth + 1, node if node is not None else parent)
        return node

    walk(root_el, 0, None)
    width = max((n.bounds[2] for n in nodes), default=0)
    height = max((n.bounds[3] for n in nodes), default=0)
    root = nodes[0] if nodes else None
    return ParsedTree(root, nodes, width, height)


def _role(n: RawNode) -> str:
    c = n.cls
    if n.is_text_field:
        return "text_field"
    if "Switch" in c:
        return "switch"
    if "CheckBox" in c:
        return "checkbox"
    if "RadioButton" in c:
        return "radio"
    if "ImageButton" in c:
        return "icon_button"
    if "Button" in c:
        return "button"
    if "Tab" in c and n.clickable:
        return "tab"
    if c.endswith(("RecyclerView", "ListView", "ScrollView", "GridView", "ViewPager")) or n.scrollable:
        return "list"
    if "ProgressBar" in c:
        return "progress"
    if "Image" in c:
        return "image" if not n.clickable else "icon_button"
    if "WebView" in c:
        return "web"
    if n.checkable:
        return "checkbox"
    if n.clickable or n.long_clickable:
        return "button"
    return "text"


def _descendant_text(n: RawNode, limit: int = 3) -> list[str]:
    out: list[str] = []

    def walk(x: RawNode) -> None:
        for c in x.children:
            if len(out) >= limit:
                return
            if c.interactive and c is not n:
                continue
            t = (c.text or c.desc).strip()
            if t and not c.password and t not in out:
                out.append(t)
            walk(c)

    walk(n)
    return out


def _rid_hint(resource_id: str) -> str:
    if not resource_id:
        return ""
    token = resource_id.rsplit("/", 1)[-1]
    token = re.sub(r"^(btn|button|iv|img|ic|icon|tv|text)_", "", token)
    return token.replace("_", " ")


def _in_screen(b: tuple[int, int, int, int], w: int, h: int) -> bool:
    x0, y0, x1, y1 = b
    return x1 - x0 >= 4 and y1 - y0 >= 4 and x1 > 0 and y1 > 0 and x0 < w and y0 < h


def normalize(
    tree: ParsedTree, screen_w: int | None = None, screen_h: int | None = None, max_elements: int = MAX_ELEMENTS_DEFAULT
) -> tuple[list[UIElement], list[dict[str, object]], int]:
    """Return (elements, text_blocks, truncated_count)."""
    w = screen_w or tree.width or 1
    h = screen_h or tree.height or 1
    elements: list[UIElement] = []
    blocks: list[dict[str, object]] = []
    absorbed: set[int] = set()

    candidates = [n for n in tree.nodes if n.visible and _in_screen(n.bounds, w, h)]
    for n in candidates:
        if not n.interactive:
            continue
        own = (n.text or n.desc).strip()
        if n.password:
            own = (n.desc or n.hint).strip() or (PASSWORD_MASK if n.text else "")
        merged = _descendant_text(n) if not own else []
        for c in n.children:
            if not c.interactive:
                absorbed.add(id(c))
                for gc in c.children:
                    if not gc.interactive:
                        absorbed.add(id(gc))
        label = own or " / ".join(merged) or n.hint or _rid_hint(n.resource_id) or "unlabeled"
        text = PASSWORD_MASK if (n.password and n.text) else n.text
        bn = px_to_norm(n.bounds[0], n.bounds[1], w, h) + px_to_norm(n.bounds[2], n.bounds[3], w, h)
        elements.append(
            UIElement(
                id=0,
                stable_key="",
                role=_role(n),
                label=label[:120],
                text=text[:500],
                desc=n.desc[:200],
                resource_id=n.resource_id,
                cls=n.cls,
                package=n.package,
                bounds=n.bounds,
                bounds_norm=bn,  # type: ignore[arg-type]
                clickable=n.clickable or n.long_clickable,
                enabled=n.enabled,
                focused=n.focused,
                checked=n.checked if n.checkable else None,
                scrollable=n.scrollable,
                password=n.password,
                hint=n.hint,
            )
        )
    for n in candidates:
        if n.interactive or id(n) in absorbed:
            continue
        t = (n.text or n.desc).strip()
        if not t or n.password:
            continue
        # skip text that sits inside an interactive element (already merged into its label)
        anc = n.parent
        inside = False
        while anc is not None:
            if anc.interactive and anc.visible:
                inside = True
                break
            anc = anc.parent
        if inside:
            continue
        bn = px_to_norm(n.bounds[0], n.bounds[1], w, h) + px_to_norm(n.bounds[2], n.bounds[3], w, h)
        blocks.append({"text": t[:300], "bounds_norm": list(bn)})

    # reading order: top-to-bottom, then left-to-right (row bucket of 1/100 screen height)
    elements.sort(key=lambda e: (e.bounds[1] * 100 // max(h, 1), e.bounds[0]))
    blocks.sort(key=lambda b: (b["bounds_norm"][1], b["bounds_norm"][0]))  # type: ignore[index]

    truncated = 0
    if len(elements) > max_elements:
        truncated = len(elements) - max_elements
        elements = elements[:max_elements]
    for i, e in enumerate(elements, start=1):
        e.id = i
        e.stable_key = f"{e.resource_id}|{e.label}|{e.role}|{e.bounds_norm[0] // 50},{e.bounds_norm[1] // 50}"
    return elements, blocks, truncated


def serialize_elements(elements: list[UIElement]) -> str:
    """Compact text form handed to the model (one element per line)."""
    lines = ["id,role,label,state,bounds_norm"]
    for e in elements:
        label = e.label.replace('"', "'")
        b = ",".join(str(v) for v in e.bounds_norm)
        lines.append(f'{e.id},{e.role},"{label}",{e.state_words()},"{b}"')
    return "\n".join(lines)


def element_at(elements: list[UIElement], x: int, y: int) -> UIElement | None:
    """Smallest element whose bounds contain the point (used to classify coordinate taps)."""
    hits = [e for e in elements if e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3]]
    if not hits:
        return None
    return min(hits, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))


def has_progress(tree: ParsedTree) -> bool:
    return any("ProgressBar" in n.cls and n.visible for n in tree.nodes)


def match_selector(e: UIElement, sel: dict[str, object]) -> bool:
    """Selector grammar from PRD 11.2 (except near/within/index which the caller resolves)."""
    label_or_text = e.text or e.label
    if "text" in sel and label_or_text != sel["text"] and e.label != sel["text"]:
        return False
    if "text_any" in sel:
        opts = sel["text_any"]
        assert isinstance(opts, list)
        if label_or_text not in opts and e.label not in opts and e.desc not in opts:
            return False
    if "text_contains" in sel:
        needle = str(sel["text_contains"]).lower()
        if needle not in (e.text + " " + e.label + " " + e.desc).lower():
            return False
    if "text_regex" in sel and not re.search(str(sel["text_regex"]), e.text + " " + e.label):
        return False
    if "desc" in sel and e.desc != sel["desc"]:
        return False
    if "desc_contains" in sel and str(sel["desc_contains"]).lower() not in e.desc.lower():
        return False
    if "resource_id" in sel and e.resource_id != sel["resource_id"]:
        return False
    if "role" in sel and e.role != sel["role"]:
        return False
    return True


def find(elements: list[UIElement], sel: dict[str, object]) -> list[UIElement]:
    base = {k: v for k, v in sel.items() if k not in {"index", "near", "within"}}
    hits = [e for e in elements if match_selector(e, base)]
    if "within" in sel and isinstance(sel["within"], dict):
        parents = find(elements, sel["within"])
        hits = [
            e
            for e in hits
            if any(p.bounds[0] <= e.bounds[0] and p.bounds[1] <= e.bounds[1] and e.bounds[2] <= p.bounds[2] and e.bounds[3] <= p.bounds[3] for p in parents)
        ]
    if "near" in sel and isinstance(sel["near"], dict):
        anchors = find(elements, sel["near"])
        if anchors:
            ax, ay = anchors[0].center
            hits = sorted((e for e in hits if e is not anchors[0]), key=lambda e: (e.center[0] - ax) ** 2 + (e.center[1] - ay) ** 2)
    if "index" in sel:
        idx = int(sel["index"])  # type: ignore[arg-type]
        return hits[idx : idx + 1]
    return hits
