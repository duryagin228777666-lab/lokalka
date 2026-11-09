"""«Второй мозг»: граф заметок, который живёт на флешке и переживает перезапуски.

Узел — заметка (название, текст, метки). Рёбра — связи между заметками, в обе стороны.
Агент сам записывает туда то, что стоит помнить, и достаёт это в следующих сеансах.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional

from .config import APP_DIR

BRAIN_ENV = "LOKALKA_BRAIN"
TITLE_LIMIT = 120
TEXT_LIMIT = 4000
SEARCH_LIMIT = 8
INDEX_LIMIT = 60

Note = Dict[str, Any]


def brain_path() -> str:
    return os.environ.get(BRAIN_ENV) or os.path.join(APP_DIR, "brain.json")


def _key(title: str) -> str:
    return " ".join(title.lower().split())


def _words(text: str) -> List[str]:
    return [word for word in re.findall(r"\w+", text.lower()) if len(word) > 2]


class Brain:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or brain_path()
        self.notes = self._load()  # type: Dict[str, Note]

    def _load(self) -> Dict[str, Note]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        notes = data.get("notes") if isinstance(data, dict) else None
        return {key: note for key, note in (notes or {}).items() if isinstance(note, dict)}

    def _store(self) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "notes": self.notes}, handle, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self.path)

    def _node(self, title: str) -> Note:
        key = _key(title)
        if key not in self.notes:
            self.notes[key] = {"title": title.strip()[:TITLE_LIMIT], "text": "", "tags": [], "links": []}
        return self.notes[key]

    def _link(self, first: str, second: str) -> None:
        a, b = _key(first), _key(second)
        if a == b:
            return
        for here, there in ((a, b), (b, a)):
            links = self.notes[here]["links"]
            if there not in links:
                self.notes[here]["links"] = links + [there]

    # ------------------------------------------------------------ операции

    def save(self, title: str, text: str, links: List[str], tags: List[str]) -> str:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        note = self._node(title)
        note["text"] = text.strip()[:TEXT_LIMIT]
        note["tags"] = sorted(set(note.get("tags", []) + [tag.strip().lower() for tag in tags if tag.strip()]))
        note["updated"] = datetime.date.today().isoformat()
        for other in links:
            if other.strip():
                self._node(other)
                self._link(title, other)
        self._store()
        return "Saved note '{}' ({} links).".format(note["title"], len(note["links"]))

    def read(self, title: str) -> Optional[str]:
        note = self.notes.get(_key(title))
        if note is None:
            return None
        return self._render(note, with_neighbours=True)

    def search(self, query: str) -> List[Note]:
        wanted = set(_words(query))
        scored = []
        for note in self.notes.values():
            haystack = _words(note["title"] + " " + note.get("text", "") + " " + " ".join(note.get("tags", [])))
            title_words = set(_words(note["title"]))
            score = sum(3 if word in title_words else 1 for word in wanted if word in haystack)
            if query.lower().strip() and query.lower().strip() in note["title"].lower():
                score += 5
            if score:
                scored.append((score, note))
        scored.sort(key=lambda pair: -pair[0])
        return [note for _, note in scored[:SEARCH_LIMIT]]

    def forget(self, title: str) -> bool:
        key = _key(title)
        if key not in self.notes:
            return False
        for other in self.notes[key]["links"]:
            if other in self.notes:
                self.notes[other]["links"] = [link for link in self.notes[other]["links"] if link != key]
        del self.notes[key]
        self._store()
        return True

    # ------------------------------------------------------------ вид

    def title_of(self, key: str) -> str:
        return self.notes[key]["title"] if key in self.notes else key

    def _render(self, note: Note, with_neighbours: bool = False) -> str:
        lines = ["# " + note["title"]]
        if note.get("tags"):
            lines.append("tags: " + ", ".join(note["tags"]))
        lines.append(note.get("text") or "(empty note — only a link hub)")
        if note.get("links"):
            lines.append("linked: " + ", ".join(self.title_of(key) for key in note["links"]))
        if with_neighbours:
            for key in note.get("links", []):
                other = self.notes.get(key)
                if other and other.get("text"):
                    lines.append("  - {}: {}".format(other["title"], other["text"][:200].replace("\n", " ")))
        return "\n".join(lines)

    def render_search(self, query: str) -> str:
        found = self.search(query)
        if not found:
            return "Nothing in memory matches '{}'.".format(query)
        return "\n\n".join(self._render(note) for note in found)

    def index(self) -> str:
        """Короткий список заметок для системного промпта: модель знает, что уже помнит."""
        ranked = sorted(self.notes.values(), key=lambda note: (-len(note.get("links", [])), note["title"]))
        titles = [note["title"] for note in ranked[:INDEX_LIMIT]]
        if len(self.notes) > INDEX_LIMIT:
            titles.append("... and {} more".format(len(self.notes) - INDEX_LIMIT))
        return ", ".join(titles)

    def map(self) -> str:
        """Граф для человека: каждая заметка и её связи."""
        if not self.notes:
            return "Второй мозг пока пуст. Он наполняется сам, по ходу работы."
        rows = []
        for note in sorted(self.notes.values(), key=lambda item: item["title"].lower()):
            links = ", ".join(self.title_of(key) for key in note.get("links", []))
            rows.append("  ● {}{}".format(note["title"], "  →  " + links if links else ""))
        edges = sum(len(note.get("links", [])) for note in self.notes.values()) // 2
        return "Заметок: {}, связей: {}\n{}".format(len(self.notes), edges, "\n".join(rows))
