from dataclasses import dataclass


@dataclass(frozen=True)
class Tag:
    key: str
    value: str

    def __post_init__(self) -> None:
        if not self.key or not self.value or "=" in self.key:
            raise ValueError("invalid tag")


def parse_tags(value: str | None) -> list[Tag]:
    if not value:
        return []

    tags: list[Tag] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("invalid tag")
        key, tag_value = item.split("=", 1)
        tags.append(Tag(key=key.strip(), value=tag_value.strip()))
    return tags
