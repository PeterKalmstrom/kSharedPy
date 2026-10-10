"""Test stub of google.genai.types: each type keeps its keyword arguments as attributes."""


class _Bag:
    def __init__(self, **Fields):
        self.__dict__.update(Fields)


class GenerateContentConfig(_Bag):
    pass


class ImageConfig(_Bag):
    pass


class GenerateVideosConfig(_Bag):
    pass


class GenerateVideosSource(_Bag):
    pass


class Image(_Bag):
    pass


class Part:
    @staticmethod
    def from_bytes(data, mime_type):
        return data
