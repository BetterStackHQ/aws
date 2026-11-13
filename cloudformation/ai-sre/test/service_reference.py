"""Expands IAM action patterns against AWS's service reference (https://servicereference.us-east-1.amazonaws.com/).

A pattern `prefix:Pat` matches every action of that service prefix whose name matches `Pat` case-insensitively, `*`
standing for any run of characters and `?` for one character. The reference is read from its public URL, or from a
directory holding `index.json` and one `<prefix>.json` per service (the shape the URL serves), as the tests do.
"""

import functools
import json
import os
import re
import urllib.request

DEFAULT_SOURCE = "https://servicereference.us-east-1.amazonaws.com/"


@functools.lru_cache(maxsize=None)
def _compiled(pattern, ignore_case):
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(regex, re.IGNORECASE if ignore_case else 0)


def glob_matches(pattern, value, ignore_case=True):
    return _compiled(pattern, ignore_case).fullmatch(value) is not None


class ServiceReference:
    def __init__(self, source=DEFAULT_SOURCE, cache_dir=None):
        self.source = source
        self.cache_dir = cache_dir
        self._actions = {}
        self._index = None

    def _read(self, url_or_path):
        if os.path.isdir(self.source):
            with open(url_or_path, encoding="utf-8") as f:
                return json.load(f)
        if self.cache_dir:
            cached = os.path.join(self.cache_dir, re.sub(r"[^A-Za-z0-9._-]", "_", url_or_path))
            if os.path.exists(cached):
                with open(cached, encoding="utf-8") as f:
                    return json.load(f)
        with urllib.request.urlopen(url_or_path, timeout=60) as response:
            data = json.load(response)
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cached, "w", encoding="utf-8") as f:
                json.dump(data, f)
        return data

    def index(self):
        """{prefix: service JSON location}."""
        if self._index is None:
            if os.path.isdir(self.source):
                entries = self._read(os.path.join(self.source, "index.json"))
                self._index = {e["service"]: os.path.join(self.source, f"{e['service']}.json") for e in entries}
            else:
                self._index = {e["service"]: e["url"] for e in self._read(self.source)}
        return self._index

    def actions(self, prefix):
        """The action names of a service prefix, as the reference spells them; None for an unknown prefix."""
        if prefix not in self._actions:
            location = self.index().get(prefix)
            self._actions[prefix] = None if location is None else [a["Name"] for a in self._read(location)["Actions"]]
        return self._actions[prefix]

    def expand(self, pattern):
        """Every `prefix:Name` the pattern matches; an empty set for an unknown prefix or a pattern matching nothing."""
        if pattern == "*" or ":" not in pattern:
            raise ValueError(f"not a service-scoped action pattern: {pattern}")
        prefix, name = pattern.split(":", 1)
        names = self.actions(prefix.lower()) or []
        return {f"{prefix.lower()}:{n}" for n in names if glob_matches(name, n)}
