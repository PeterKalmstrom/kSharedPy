"""Test stub of google.genai - no network. Behaviour:

- STUB_MODE=fail: every generate_content raises RuntimeError("stub API down") (an unexpected failure).
- STUB_MODE=bad: the vedit retake finder gets "not json".
- An image model (name contains "image"): a prompt containing REFUSE raises errors.APIError, NOIMAGE returns no
  candidates, anything else returns fake PNG bytes b"\\x89PNG\\r\\n\\x1a\\nSTUB:<prompt>".
- generate_videos: REFUSE raises errors.APIError, NOVIDEO returns a finished operation without a video, anything
  else a video whose save() writes the file KSTUB_VIDEO (a real clip) or b"STUBVIDEO:<prompt>".
- KSTUB_LOG=<file>: every call is appended as one JSON line (the tests check the key and arguments).
"""
import json
import os
import shutil

from . import errors, types

__all__ = ["Client", "errors", "types"]  # the scripts also import google.genai.errors / .types as submodules
IS_KSHAREDPY_STUB = True


def _Log(Kind, **Fields):
    LogPath = os.environ.get("KSTUB_LOG")
    if LogPath:
        with open(LogPath, "a", encoding="utf-8") as File:
            File.write(json.dumps(dict(Fields, kind=Kind), default=str) + "\n")


class _Bag:
    def __init__(self, **Fields):
        self.__dict__.update(Fields)


class _Video:
    def __init__(self, Prompt):
        self.Prompt = Prompt
        self.Downloaded = False

    def save(self, Path):
        Source = os.environ.get("KSTUB_VIDEO")
        if Source:
            shutil.copyfile(Source, Path)
        else:
            with open(Path, "wb") as File:
                File.write(b"STUBVIDEO:" + self.Prompt.encode("utf-8"))


class _Models:
    def generate_content(self, model, contents, config=None):
        _Log("generate_content", model=model, contents=contents if isinstance(contents, str) else "<parts>")
        Mode = os.environ.get("STUB_MODE", "")
        if Mode == "fail":
            raise RuntimeError("stub API down")
        if "image" in model:
            if "REFUSE" in contents:
                raise errors.APIError("400 stub refusal: blocked by safety")
            if "NOIMAGE" in contents:
                return _Bag(candidates=[], text=None)
            Data = b"\x89PNG\r\n\x1a\nSTUB:" + contents.encode("utf-8")
            Part = _Bag(inline_data=_Bag(data=Data, mime_type="image/png"))
            return _Bag(candidates=[_Bag(content=_Bag(parts=[Part]))], text=None)
        if isinstance(contents, list):
            return _Bag(text=json.dumps({"action": "cut", "why": "stub"}))
        if "subtitle" in contents:
            return _Bag(text="[1] Polished one\n[3] Polished three")
        if Mode == "bad":
            return _Bag(text="not json")
        return _Bag(text=json.dumps([{"start": 7.1, "end": 8.6, "reason": "wait, let me start over"},
                                     {"start": "x"}]))

    def generate_videos(self, model, source, config=None):
        Image = getattr(source, "image", None)
        _Log("generate_videos", model=model, prompt=source.prompt,
             image_mime=getattr(Image, "mime_type", None), image_size=len(getattr(Image, "image_bytes", b"") or b""),
             resolution=getattr(config, "resolution", None), negative_prompt=getattr(config, "negative_prompt", None))
        if "REFUSE" in source.prompt:
            raise errors.APIError("429 RESOURCE_EXHAUSTED stub refusal")
        if "NOVIDEO" in source.prompt:
            return _Bag(done=True, error=None, response=_Bag(generated_videos=[], rai_media_filtered_reasons=["stub"]))
        return _Bag(done=True, error=None, response=_Bag(generated_videos=[_Bag(video=_Video(source.prompt))]))


class _Files:
    def download(self, file):
        file.Downloaded = True


class _Operations:
    def get(self, operation):
        return operation


class Client:
    def __init__(self, api_key=None, **Options):
        _Log("client", api_key=api_key, env_key=os.environ.get("GEMINI_API_KEY"))
        self.models = _Models()
        self.files = _Files()
        self.operations = _Operations()
