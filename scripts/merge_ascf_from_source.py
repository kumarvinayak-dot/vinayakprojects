#!/usr/bin/env python3
"""Merge ASCF foundations/admin routes from source builtin_server into target."""

from pathlib import Path

SOURCE = Path(r"C:\Users\SVINAYAK\python_project\builtin_server.py")
TARGET = Path(r"C:\Users\SVINAYAK\Desktop\python_project\builtin_server.py")

# 1-based inclusive line ranges from source
GET_FOUNDATIONS_START = 2047
GET_FOUNDATIONS_END = 2437
POST_FOUNDATIONS_START = 5213
POST_FOUNDATIONS_END = 6635


def extract_lines(path: Path, start: int, end: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[start - 1 : end])


def insert_before_marker(content: str, marker: str, block: str, once: bool = True) -> str:
    if once and marker in block and content.count(marker) > content.count(marker) + 1:
        pass
    if once and block.strip() and block.strip()[:80] in content:
        return content
    idx = content.find(marker)
    if idx < 0:
        raise ValueError(f"Marker not found: {marker!r}")
    return content[:idx] + block + content[idx:]


def main():
    src = SOURCE.read_text(encoding="utf-8")
    tgt = TARGET.read_text(encoding="utf-8")

    get_block = extract_lines(SOURCE, GET_FOUNDATIONS_START, GET_FOUNDATIONS_END)
    post_block = extract_lines(SOURCE, POST_FOUNDATIONS_START, POST_FOUNDATIONS_END)

    if 'if path == "/foundations":' in tgt:
        print("GET foundations already present — skip")
    else:
        tgt = insert_before_marker(
            tgt,
            '        if path == "/view":',
            "\n" + get_block + "\n",
        )
        print("Inserted GET foundations block")

    if 'parsed.path == "/foundations/prepare"' in tgt:
        print("POST foundations already present — skip")
    else:
        tgt = insert_before_marker(
            tgt,
            '        if parsed.path == "/copy_async":',
            "\n" + post_block + "\n",
        )
        print("Inserted POST foundations block")

    TARGET.write_text(tgt, encoding="utf-8")
    print("Wrote", TARGET)


if __name__ == "__main__":
    main()
