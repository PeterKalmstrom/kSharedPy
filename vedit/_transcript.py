"""Transcript helpers shared by the vedit scripts (propose.py, review.py)."""
from kShared import kS


class kTranscript:
    """Read text out of a faster-whisper transcript (a list of segments with optional word timestamps)."""

    @staticmethod
    def WordsInside(TranscriptSegments, Start, End):
        """The words whose midpoints fall within [Start, End], from word-level timestamps; falls back to the whole
        text of the segments that overlap the span when the transcript has no words."""
        if kS.ErrorMode:
            return ""
        try:
            Words = []
            SawWords = False
            for Segment in TranscriptSegments:
                if Segment["end"] <= Start or Segment["start"] >= End:
                    continue
                if Segment.get("words"):
                    SawWords = True
                    for Word in Segment["words"]:
                        Middle = (Word["start"] + Word["end"]) / 2
                        if Start <= Middle <= End:
                            Words.append(Word["word"])
            if SawWords:
                return "".join(Words).strip()
            return " ".join(Segment["text"] for Segment in TranscriptSegments
                            if Segment["end"] > Start and Segment["start"] < End).strip()
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTranscript.WordsInside")
            return ""
