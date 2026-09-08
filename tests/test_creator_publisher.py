import tempfile
import unittest
from pathlib import Path

from request.creator.publisher import (
    IMAGE_SUFFIXES,
    _normalize_tags,
    _validate_media_paths,
    _validate_title,
)


class CreatorPublisherValidationTests(unittest.TestCase):
    def test_normalize_tags(self):
        tags = _normalize_tags(["#摄影", " 摄影 ", "", "#旅行", "旅行"])
        self.assertEqual(tags, ["摄影", "旅行"])

    def test_tags_are_limited_to_ten(self):
        self.assertEqual(len(_normalize_tags(str(i) for i in range(20))), 10)

    def test_title_validation(self):
        self.assertEqual(_validate_title("  标题  "), "标题")
        with self.assertRaises(ValueError):
            _validate_title("")
        with self.assertRaises(ValueError):
            _validate_title("长" * 21)

    def test_media_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "cover.JPG"
            image.write_bytes(b"not-a-real-image")
            result = _validate_media_paths([image], IMAGE_SUFFIXES, "图片")
            self.assertEqual(result, [image.resolve()])

            bad = Path(directory) / "cover.txt"
            bad.write_text("x")
            with self.assertRaises(ValueError):
                _validate_media_paths([bad], IMAGE_SUFFIXES, "图片")


if __name__ == "__main__":
    unittest.main()
