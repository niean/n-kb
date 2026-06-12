import hashlib

import pytest

from app.infrastructure.parsers.text_parser import SimpleTextParser, SimpleTextSplitter


def test_parser_accepts_markdown_and_text_utf8():
    parser = SimpleTextParser()

    assert parser.parse("notes.md", "# hello".encode()) == "# hello"
    assert parser.parse("notes.txt", "plain".encode()) == "plain"


@pytest.mark.parametrize("filename,content", [("notes.pdf", b"body"), ("broken.md", b"\xff\xfe")])
def test_parser_rejects_unsupported_extension_or_decode_error(filename, content):
    parser = SimpleTextParser()

    with pytest.raises(ValueError, match="unsupported_file_type"):
        parser.parse(filename, content)


def test_splitter_returns_stable_chunks_with_overlap_hash_and_metadata():
    splitter = SimpleTextSplitter(chunk_size=10, chunk_overlap=3)

    chunks = splitter.split("doc-1", "one two three four five")

    assert [chunk.id for chunk in chunks] == ["doc-1-chunk-0", "doc-1-chunk-1", "doc-1-chunk-2"]
    assert [chunk.ordinal for chunk in chunks] == [0, 1, 2]
    assert [chunk.text for chunk in chunks] == ["one two th", " three fou", "four five"]
    assert [chunk.metadata for chunk in chunks] == [{"ordinal": 0}, {"ordinal": 1}, {"ordinal": 2}]
    assert chunks[0].token_count == len(chunks[0].text.split())
    assert chunks[0].content_hash == hashlib.sha256(chunks[0].text.encode("utf-8")).hexdigest()


def test_splitter_returns_empty_list_for_empty_text():
    assert SimpleTextSplitter().split("doc-1", "") == []
