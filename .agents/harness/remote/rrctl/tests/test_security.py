"""Unit coverage for secret detection/redaction and path confinement helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control.errors import RRCError
from remote_run_control.security import (
    StreamRedactor,
    confined_relative_path,
    contains_secret,
    contains_secret_data,
    ensure_within,
    redact,
    redact_data,
)


class ContainsSecretTests(unittest.TestCase):
    def test_detects_assignment_flag_and_private_key(self):
        self.assertTrue(contains_secret("connect with password=hunter2 now"))
        self.assertTrue(contains_secret("API_KEY: abcdef123"))
        self.assertTrue(contains_secret("--password s3cret"))
        self.assertTrue(contains_secret("-----BEGIN OPENSSH PRIVATE KEY-----"))

    def test_benign_text_is_accepted(self):
        self.assertFalse(contains_secret("log level set to info"))
        self.assertFalse(contains_secret("--workers 4 --lr 0.01"))


class ContainsSecretDataTests(unittest.TestCase):
    def test_nested_secret_key_match_is_rejected(self):
        payload = {"outer": [{"api_key": "value-that-is-not-itself-a-secret"}]}
        self.assertTrue(contains_secret_data(payload))

    def test_secret_marker_in_string_value_is_rejected(self):
        payload = {"notes": ["ok", {"command": "run --token abc"}]}
        self.assertTrue(contains_secret_data(payload))

    def test_benign_structure_is_accepted(self):
        payload = {"config": {"tokeniser": "bpe", "passwordless": True}, "count": 3}
        self.assertFalse(contains_secret_data(payload))

    def test_non_string_scalars_are_accepted(self):
        self.assertFalse(contains_secret_data({"limit": 42, "enabled": False, "ratio": 0.5}))


class RedactTests(unittest.TestCase):
    def test_assignment_value_is_redacted(self):
        result = redact("connect with password=hunter2 now")
        self.assertNotIn("hunter2", result)
        self.assertIn("[REDACTED]", result)

    def test_flag_value_is_redacted(self):
        result = redact("cmd --token secret123 --verbose")
        self.assertNotIn("secret123", result)

    def test_private_key_block_is_redacted(self):
        result = redact(
            "pre\n-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----\npost"
        )
        self.assertNotIn("abc123", result)
        self.assertIn("post", result)

    def test_explicit_value_and_json_escaped_form_are_redacted(self):
        self.assertNotIn("hunter2", redact("the word hunter2 appears", ["hunter2"]))
        result = redact('json fragment password\\twithtab inside', ["password\twithtab"])
        self.assertNotIn("password\\twithtab", result)

    def test_redact_data_masks_credential_keys_and_string_values(self):
        payload = {
            "auth_token": "kept-as-key-but-value-hidden",
            "nested": ["echo password=hunter2", 7],
        }
        result = redact_data(payload)
        self.assertEqual(result["auth_token"], "[REDACTED]")
        self.assertEqual(result["nested"][1], 7)
        self.assertNotIn("hunter2", result["nested"][0])


class StreamRedactorTests(unittest.TestCase):
    def _feed(self, chunks, final=True):
        redactor = StreamRedactor()
        output = b""
        encoded = [chunk.encode() if isinstance(chunk, str) else chunk for chunk in chunks]
        for index, chunk in enumerate(encoded):
            output += redactor.feed(chunk, final=final and index == len(encoded) - 1)
        return output.decode("utf-8")

    def test_credential_split_across_chunks_is_redacted(self):
        result = self._feed(["export password=hun", "ter2x more"])
        self.assertNotIn("hunter2x", result)

    def test_small_final_flush_still_redacts(self):
        # keep=128 的非 final 缓冲在 final=True 时必须仍然脱敏，不得原样落盘。
        result = self._feed(["password=hunter2"])
        self.assertNotIn("hunter2", result)

    def test_private_key_across_multiple_chunks_is_redacted(self):
        result = self._feed(
            [
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                "x" * 200,
                "-----END OPENSSH PRIVATE KEY-----after",
            ]
        )
        self.assertNotIn("x" * 50, result)
        self.assertIn("after", result)

    def test_invalid_utf8_is_replaced_without_crashing(self):
        result = self._feed([b"\xffpassword=hunter2\xfe"])
        self.assertNotIn("hunter2", result)


class ConfinedRelativePathTests(unittest.TestCase):
    def test_nested_relative_path_is_accepted(self):
        result = confined_relative_path("a/b.txt", field="f")
        self.assertIsInstance(result, PurePosixPath)
        self.assertEqual(result.as_posix(), "a/b.txt")

    def test_absolute_dot_dotdot_and_empty_are_rejected(self):
        for value in ("/etc/passwd", "../out", "a/../../out", ".", ""):
            with self.subTest(value=value):
                with self.assertRaises(RRCError) as caught:
                    confined_relative_path(value, field="f")
                self.assertEqual(caught.exception.code, "path_not_confined")


class EnsureWithinTests(unittest.TestCase):
    def test_inside_path_is_accepted_and_resolved(self):
        base = Path(__file__).resolve().parent
        result = ensure_within(base, base / "sub" / "file.txt", field="artifact")
        self.assertEqual(result, (base / "sub" / "file.txt").resolve())

    def test_escaping_path_is_rejected(self):
        base = Path(__file__).resolve().parent
        with self.assertRaises(RRCError) as caught:
            ensure_within(base, base / ".." / "outside", field="artifact")
        self.assertEqual(caught.exception.code, "path_escape")


if __name__ == "__main__":
    unittest.main()
