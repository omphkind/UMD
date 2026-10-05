"""Backend independent normalized metadata for audio and photographs."""
from dataclasses import asdict, dataclass, field


@dataclass
class AudioMetadata:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    year: str = ""
    genre: str = ""
    composer: str = ""
    comment: str = ""
    cover: str = ""
    source: str = ""
    source_metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class PhotoMetadata:
    title: str = ""
    original_title: str = ""
    author: str = ""
    source: str = ""
    source_id: str = ""
    published_at: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    format: str = ""
    original_filename: str = ""
    gallery_name: str = ""
    gallery_index: int | None = None
    source_metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class MetadataService:
    @staticmethod
    def audio(raw: dict, source: str) -> dict:
        from app.sources.authentication import public_metadata
        def text(name, fallback=""):
            return str(raw.get(name) or fallback)
        return AudioMetadata(
            title=text("track", raw.get("title", "")), artist=text("artist", raw.get("creator") or raw.get("uploader") or ""),
            album=text("album"), album_artist=text("album_artist", raw.get("artist", "")),
            track_number=raw.get("track_number"), disc_number=raw.get("disc_number"),
            year=text("release_year", str(raw.get("release_date") or raw.get("upload_date") or "")[:4]),
            genre=text("genre"), composer=text("composer"), comment=text("description"),
            cover=text("thumbnail"), source=source, source_metadata=public_metadata(raw),
        ).to_dict()
