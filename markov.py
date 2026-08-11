"""A small, dependency-free Markov chain text generator."""

from __future__ import annotations

import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable


_START = "\0START"
_END = "\0END"
_TOKEN_RE = re.compile(r"[\wЁёА-я-]+|[^\w\s]", re.UNICODE)
_NO_SPACE_BEFORE = frozenset(".,!?;:%)]}»…")
_NO_SPACE_AFTER = frozenset("([{«")


class MarkovChain:
    """Generate sentences using an n-th order word-level Markov chain."""

    def __init__(
        self,
        lines: Iterable[str],
        *,
        order: int = 2,
        rng: random.Random | None = None,
    ) -> None:
        if order < 1:
            raise ValueError("order must be at least 1")

        self.order = order
        self._rng = rng or random.Random()
        self._transitions: dict[tuple[str, ...], list[str]] = defaultdict(list)
        self._source_lines: list[str] = []

        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            tokens = self.tokenize(line)
            if not tokens:
                continue
            self._source_lines.append(line)
            state = (_START,) * self.order
            for token in [*tokens, _END]:
                self._transitions[state].append(token)
                state = (*state[1:], token)

        if not self._source_lines:
            raise ValueError("the training corpus does not contain any text")

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        order: int = 2,
        rng: random.Random | None = None,
    ) -> "MarkovChain":
        with Path(path).open("r", encoding="utf-8") as corpus:
            return cls(corpus, order=order, rng=rng)

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return _TOKEN_RE.findall(text)

    @staticmethod
    def detokenize(tokens: Iterable[str]) -> str:
        result = ""
        previous = ""
        for token in tokens:
            if not result:
                result = token
            elif token[0] in _NO_SPACE_BEFORE or previous[-1] in _NO_SPACE_AFTER:
                result += token
            else:
                result += f" {token}"
            previous = token
        return result

    def generate(self, *, max_words: int = 35, attempts: int = 20) -> str:
        """Return a generated sentence, preferring a non-verbatim result."""
        if max_words < 1:
            raise ValueError("max_words must be at least 1")

        fallback = self._source_lines[0]
        for _ in range(max(1, attempts)):
            state = (_START,) * self.order
            tokens: list[str] = []

            for _ in range(max_words):
                choices = self._transitions.get(state)
                if not choices:
                    break
                token = self._rng.choice(choices)
                if token == _END:
                    break
                tokens.append(token)
                state = (*state[1:], token)

            text = self.detokenize(tokens).strip()
            if text:
                fallback = text
                if text not in self._source_lines:
                    return text

        return fallback
