"""Render a conversation spec to a phone screenshot, and to its own answer key.

B1's fixtures were delivered as binaries. Nothing in the repository could make
another one, the regions in `ground_truth.json` were measured by eye, and the
brief that described how to commission them was deleted once the first seven
existed. So "add a screenshot" meant "commission a screenshot", which is why
there have been seven since August 2nd.

This renders one from a YAML spec. The spec is the thing worth reviewing in a
diff -- who is in the thread, what they typed, which part of it is an address,
and how it is damaged -- and the PNG is a build artifact that happens to be
committed because the tests read pixels.

The regions come out exact. `_draw_wrapped` records the bounding box of the
runs it marked as an address, so a region is a fact about where the glyphs
landed rather than a rectangle someone dragged. That is the one thing a
generator gives that a commissioned image cannot: design 4 wants the pointer
so B3 can crop back and re-read, and a region measured by eye is a region that
is approximately right on the day and never checked again.

Deliberately not photorealistic. There is no Chromium here and no system UI
font -- DejaVu is what the container has -- so these look like a chat app
rather than like iOS. B1 reads text out of a conversation; it does not
authenticate the client. What is reproduced faithfully is the part that makes
extraction hard: addresses split across bubbles, corrected in a follow-up,
occluded by a reaction, and low-contrast in a dark thread.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

#: iPhone 12/13/14 Pro, which is what the delivered seven are. Designed in
#: points and scaled, so sizes below read like CSS rather than like pixels.
SCALE = 3
WIDTH_PT, HEIGHT_PT = 390, 844
WIDTH, HEIGHT = WIDTH_PT * SCALE, HEIGHT_PT * SCALE

FONTS = Path("/usr/share/fonts/truetype/dejavu")


@dataclass(frozen=True)
class Theme:
    """One messaging app's look, to the depth that matters for reading."""

    background: tuple[int, int, int]
    header: tuple[int, int, int]
    header_text: tuple[int, int, int]
    incoming: tuple[int, int, int]
    incoming_text: tuple[int, int, int]
    outgoing: tuple[int, int, int]
    outgoing_text: tuple[int, int, int]
    meta: tuple[int, int, int]
    status_text: tuple[int, int, int]
    #: iMessage-style tails, or WhatsApp's squarer corners.
    radius_pt: int = 18


THEMES: dict[str, Theme] = {
    "imessage": Theme(
        background=(255, 255, 255), header=(247, 247, 247), header_text=(0, 0, 0),
        incoming=(233, 233, 235), incoming_text=(0, 0, 0),
        outgoing=(10, 132, 255), outgoing_text=(255, 255, 255),
        meta=(142, 142, 147), status_text=(0, 0, 0),
    ),
    "imessage-dark": Theme(
        background=(0, 0, 0), header=(28, 28, 30), header_text=(255, 255, 255),
        incoming=(38, 38, 40), incoming_text=(235, 235, 235),
        outgoing=(10, 132, 255), outgoing_text=(255, 255, 255),
        # Deliberately close to the bubble: design's `hard` difficulty is
        # low-contrast dark-mode text, and Cody Barnes is the delivered case.
        meta=(72, 72, 74), status_text=(255, 255, 255),
    ),
    "whatsapp": Theme(
        background=(233, 221, 211), header=(0, 128, 105), header_text=(255, 255, 255),
        incoming=(255, 255, 255), incoming_text=(17, 17, 17),
        outgoing=(220, 248, 198), outgoing_text=(17, 17, 17),
        meta=(120, 120, 120), status_text=(255, 255, 255), radius_pt=10,
    ),
    "instagram": Theme(
        background=(255, 255, 255), header=(255, 255, 255), header_text=(0, 0, 0),
        incoming=(239, 239, 239), incoming_text=(0, 0, 0),
        outgoing=(58, 130, 247), outgoing_text=(255, 255, 255),
        meta=(142, 142, 142), status_text=(0, 0, 0), radius_pt=20,
    ),
}


@dataclass
class Run:
    """A stretch of text, and whether it is an address worth pointing at."""

    text: str
    address_for: str | None = None


@dataclass
class Message:
    """One bubble."""

    runs: list[Run]
    outgoing: bool = False
    sender: str | None = None
    timestamp: str | None = None
    reaction: str | None = None
    #: Sit the badge over the end of the last line rather than in the margin.
    reaction_over_text: bool = False
    #: How many trailing characters go under it. The answer key records what
    #: survives, so this is the number that decides what the key must say.
    reaction_covers: int = 1


@dataclass
class Rendered:
    """The image, and where every address landed in it."""

    image: Image.Image
    regions: dict[str, dict[str, int]] = field(default_factory=dict)


def _font(name: str, size_pt: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size_pt * SCALE)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_px: int) -> list[str]:
    """Greedy wrap on a real font metric rather than a character count."""
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if font.getlength(candidate) <= max_px or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


class Canvas:
    """A phone screen being filled top to bottom."""

    def __init__(self, theme: Theme, title: str, subtitle: str = "") -> None:
        self.theme = theme
        self.image = Image.new("RGB", (WIDTH, HEIGHT), theme.background)
        self.draw = ImageDraw.Draw(self.image)
        self.body = _font("DejaVuSans.ttf", 15)
        self.small = _font("DejaVuSans.ttf", 11)
        self.bold = _font("DejaVuSans-Bold.ttf", 16)
        self.regions: dict[str, dict[str, int]] = {}
        self.y = 0
        self._chrome(title, subtitle)

    def _chrome(self, title: str, subtitle: str) -> None:
        t = self.theme
        header_h = 96 * SCALE
        self.draw.rectangle([0, 0, WIDTH, header_h], fill=t.header)
        self.draw.text((24 * SCALE, 18 * SCALE), "9:41", font=self.bold, fill=t.status_text)
        # A battery, so the top of the frame is not conspicuously empty.
        bx, by = WIDTH - 60 * SCALE, 20 * SCALE
        self.draw.rounded_rectangle(
            [bx, by, bx + 30 * SCALE, by + 14 * SCALE], radius=4 * SCALE,
            outline=t.status_text, width=max(1, SCALE // 2),
        )
        self.draw.rectangle(
            [bx + 3 * SCALE, by + 3 * SCALE, bx + 22 * SCALE, by + 11 * SCALE],
            fill=t.status_text,
        )
        self.draw.text(
            (WIDTH // 2, 52 * SCALE), title, font=self.bold, fill=t.header_text, anchor="mm"
        )
        if subtitle:
            self.draw.text(
                (WIDTH // 2, 74 * SCALE), subtitle, font=self.small, fill=t.meta, anchor="mm"
            )
        self.y = header_h + 20 * SCALE

    def day(self, label: str) -> None:
        self.draw.text(
            (WIDTH // 2, self.y + 10 * SCALE), label,
            font=self.small, fill=self.theme.meta, anchor="mm",
        )
        self.y += 34 * SCALE

    def message(self, msg: Message) -> None:
        t = self.theme
        pad = 12 * SCALE
        max_bubble = int(WIDTH * 0.72)
        max_text = max_bubble - 2 * pad

        # Lay the runs out as lines, remembering which lines each address
        # occupies. Runs are wrapped as one paragraph so an address split
        # across a line break still yields one box.
        lines: list[str] = []
        spans: dict[str, list[int]] = {}
        for run in msg.runs:
            start = len(lines)
            wrapped = _wrap(run.text, self.body, max_text)
            if lines and not run.text.startswith("\n"):
                # Continue the paragraph: re-wrap the join so the box is tight.
                joined = _wrap(f"{lines[-1]} {run.text}".strip(), self.body, max_text)
                lines = lines[:-1] + joined
                start = max(start - 1, 0)
            else:
                lines.extend(wrapped)
            if run.address_for:
                spans.setdefault(run.address_for, []).extend(range(start, len(lines)))

        line_h = int(self.body.size * 1.35)
        text_w = max((self.body.getlength(line) for line in lines), default=0)
        bubble_w = int(min(max_bubble, text_w + 2 * pad))
        bubble_h = line_h * len(lines) + 2 * pad

        if msg.sender:
            self.draw.text(
                (28 * SCALE, self.y), msg.sender, font=self.small, fill=t.meta
            )
            self.y += 18 * SCALE

        x0 = WIDTH - 20 * SCALE - bubble_w if msg.outgoing else 20 * SCALE
        y0 = self.y
        self.draw.rounded_rectangle(
            [x0, y0, x0 + bubble_w, y0 + bubble_h],
            radius=t.radius_pt * SCALE,
            fill=t.outgoing if msg.outgoing else t.incoming,
        )

        colour = t.outgoing_text if msg.outgoing else t.incoming_text
        for n, line in enumerate(lines):
            self.draw.text((x0 + pad, y0 + pad + n * line_h), line, font=self.body, fill=colour)

        for key, indices in spans.items():
            top = y0 + pad + min(indices) * line_h
            bottom = y0 + pad + (max(indices) + 1) * line_h
            widths = [self.body.getlength(lines[i]) for i in indices]
            self.regions[key] = {
                "x": int(x0 + pad),
                "y": int(top),
                "width": int(max(widths) if widths else bubble_w - 2 * pad),
                "height": int(bottom - top),
            }

        if msg.reaction:
            self._reaction(msg, x0, y0, bubble_w, line_h, pad, len(lines), lines[-1])

        self.y = y0 + bubble_h + 10 * SCALE
        if msg.timestamp:
            tx = x0 + bubble_w - 4 * SCALE if msg.outgoing else x0 + 4 * SCALE
            self.draw.text(
                (tx, self.y), msg.timestamp, font=self.small, fill=t.meta,
                anchor="ra" if msg.outgoing else "la",
            )
            self.y += 22 * SCALE

    def _reaction(
        self, msg: Message, x0: int, y0: int, bw: int,
        line_h: int, pad: int, line_count: int, last_line: str,
    ) -> None:
        """A reaction badge, optionally sitting over the text it reacts to.

        Occlusion is the point when `reaction_over_text` is set. Design's
        `hard` difficulty is a digit *under* a reaction, and the fix has to be
        visual: a spec that types "0211?" instead has written the damage down,
        and a model reading a literal question mark is not doing the hard thing.

        So the badge is placed against the end of the last rendered line rather
        than against the bubble, which is wider than its shortest line and puts
        a margin-anchored badge in empty space. The answer key then records
        what is left visible -- that is `answer_key`'s caller's job, and the
        spec's `zip` field is where it is written down.
        """
        if msg.reaction_over_text:
            # Centred on the characters it is meant to hide, and smaller than a
            # margin badge so it hides those and not half the line. `covers`
            # says how many trailing characters go under it, because the answer
            # key has to state what is left readable and a badge sized by feel
            # makes that a guess.
            r = 13 * SCALE
            tail = last_line[-max(msg.reaction_covers, 1):]
            line_w = self.body.getlength(last_line)
            cx = int(x0 + pad + line_w - self.body.getlength(tail) / 2)
            cy = y0 + pad + (line_count - 1) * line_h + line_h // 2
        else:
            r = 17 * SCALE
            cx, cy = x0 + bw - r, y0
        self.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255),
                          outline=(220, 220, 220), width=max(1, SCALE // 2))
        if msg.reaction == "heart":
            self._heart(cx, cy, int(r * 0.55), (255, 45, 85))
        else:
            self.draw.text((cx, cy), "!", font=self.bold, fill=(60, 60, 60), anchor="mm")

    def _heart(self, cx: int, cy: int, s: int, colour: tuple[int, int, int]) -> None:
        self.draw.ellipse([cx - s, cy - s, cx, cy], fill=colour)
        self.draw.ellipse([cx, cy - s, cx + s, cy], fill=colour)
        self.draw.polygon(
            [(cx - s, cy - s // 3), (cx + s, cy - s // 3), (cx, cy + s)], fill=colour
        )


def render(spec: dict[str, Any]) -> Rendered:
    """One spec to one screenshot, plus the regions its addresses occupy."""
    theme = THEMES[spec.get("theme", "imessage")]
    canvas = Canvas(theme, spec.get("title", ""), spec.get("subtitle", ""))
    for entry in spec.get("thread", []):
        if "day" in entry:
            canvas.day(entry["day"])
            continue
        runs = [
            Run(text=r["text"], address_for=r.get("address_for"))
            if isinstance(r, dict) else Run(text=str(r))
            for r in entry["runs"]
        ]
        canvas.message(
            Message(
                runs=runs,
                outgoing=bool(entry.get("outgoing")),
                sender=entry.get("sender"),
                timestamp=entry.get("timestamp"),
                reaction=entry.get("reaction"),
                reaction_over_text=bool(entry.get("reaction_over_text")),
                reaction_covers=int(entry.get("reaction_covers", 1)),
            )
        )
        if canvas.y > HEIGHT:
            raise ValueError(
                f"{spec.get('file')}: the thread is taller than the screen "
                f"({canvas.y // SCALE}pt of {HEIGHT_PT}pt). Split it across two "
                "screenshots -- a message the frame cuts off is not a fixture, "
                "it is an accident."
            )
    return Rendered(image=canvas.image, regions=canvas.regions)


def answer_key(spec: dict[str, Any], regions: dict[str, dict[str, int]]) -> dict[str, Any]:
    """The `ground_truth.json` entry for a rendered spec.

    Derived rather than written, so the answer key cannot drift from the pixels
    it describes. `difficulty` and `notes` come from the spec because they are
    judgements about the image, not measurements of it.
    """
    recipients = []
    for person in spec.get("recipients", []):
        key = person["key"]
        if key not in regions:
            raise ValueError(
                f"{spec.get('file')}: recipient {key!r} has no run marked "
                f"`address_for: {key}`, so nothing points at their address."
            )
        row = {k: person.get(k, "") for k in ("name", "street1", "city", "state", "zip")}
        row["region"] = regions[key]
        row["difficulty"] = person.get("difficulty", "clean")
        row["notes"] = person.get("notes", "")
        recipients.append(row)
    entry = {
        "file": spec["file"],
        "source_type": spec.get("source_type", spec.get("theme", "imessage")),
        "recipients": recipients,
    }
    absent = _non_recipients(spec, regions)
    if absent:
        entry["non_recipients"] = absent
    return entry


def _non_recipients(
    spec: dict[str, Any], regions: dict[str, dict[str, int]]
) -> list[dict[str, Any]]:
    """People in the thread the answer key asserts B1 must *not* return.

    The key grades recall on its own: every address it lists has to come back.
    It grades precision only by omission, which is not the same as stating a
    case -- an address absent from the key is indistinguishable from one nobody
    noticed was in the picture. So a spec can name what is on screen and does
    not belong in the output, and `reason` says which of the two it is: someone
    who declined and gave nothing, or an address that was never a request.

    An entry with a `key` points at an `address_for` run and carries the region
    it occupies, for the same reason a recipient's does -- a precision failure
    is worth being able to look at. One without is a person who typed no
    address, so there is nothing to point at.
    """
    rows = []
    for person in spec.get("non_recipients", []):
        row: dict[str, Any] = {"name": person["name"], "reason": person["reason"]}
        key = person.get("key")
        if key is not None:
            if key not in regions:
                raise ValueError(
                    f"{spec.get('file')}: non-recipient {key!r} has no run marked "
                    f"`address_for: {key}`, so nothing points at the address it "
                    "claims is in the picture."
                )
            row.update(
                {k: person.get(k, "") for k in ("street1", "city", "state", "zip")}
            )
            row["region"] = regions[key]
        rows.append(row)
    return rows


__all__ = ["render", "answer_key", "Rendered", "THEMES", "WIDTH", "HEIGHT"]
