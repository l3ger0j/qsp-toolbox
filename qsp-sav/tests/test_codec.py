import random
import unittest

from qspsav import tables
from qspsav.codec import (Encoding, RecordReader, SaveFormatError, decode_records, decode_text, detect_encoding,
                          encode_text, join_records, parse_int, split_records)


class ShiftTest(unittest.TestCase):
    def test_ucs2_round_trip_all_units(self):
        text = "".join(chr(u) for u in range(1, 0x10000) if not 0xD800 <= u <= 0xDFFF)
        self.assertEqual(decode_text(encode_text(text, Encoding.UCS2), Encoding.UCS2), text)

    def test_codremov_special_case(self):
        # The unit 5 is written as -5 (0xFFFB), everything else is shifted by -5.
        self.assertEqual(encode_text("\x05", Encoding.UCS2), "￻")
        self.assertEqual(encode_text("\x0a", Encoding.UCS2), "\x05")
        self.assertEqual(decode_text("￻", Encoding.UCS2), "\x05")
        self.assertEqual(encode_text("\x05", Encoding.CP1251), "\xfb")

    def test_nul_is_rejected(self):
        # The engines encode NUL as -5, like 5, so it would come back as 5.
        with self.assertRaises(SaveFormatError):
            encode_text("a\0b", Encoding.UCS2)

    def test_surrogates_survive(self):
        for text in ("😀", "\ud800", "a\udc00b", "😀\ud800"):
            with self.subTest(text=text):
                self.assertEqual(decode_text(encode_text(text, Encoding.UCS2), Encoding.UCS2), text)

    def test_cp1251(self):
        text = "Привет, мир! №"
        self.assertEqual(decode_text(encode_text(text, Encoding.CP1251), Encoding.CP1251), text)
        with self.assertRaises(SaveFormatError):
            encode_text("😀", Encoding.CP1251)

    def test_records(self):
        for enc in Encoding:
            records = ["QSPSAVEDGAME", encode_text("x\r\ny", enc), ""]
            data = join_records(records, enc)
            self.assertIs(detect_encoding(data), enc)
            self.assertEqual(split_records(data, enc), records)

    def test_records_keep_code_units(self):
        # A surrogate pair in the file stays two units (the shift applies to
        # units); lone surrogates stay as they are.
        units = [0x41, 0xD83D, 0xDE00, 0x0D, 0x0A, 0xDC00, 0xD800, 0x0D, 0x0D, 0x0A, 0x0A]
        data = "".join(map(chr, units)).encode("utf-16-le", "surrogatepass")
        records = split_records(data, Encoding.UCS2)
        self.assertEqual(records, ["A\ud83d\ude00", "\udc00\ud800\r", "\n"])
        self.assertEqual(join_records(records, Encoding.UCS2), data)

    def test_decode_records_matches_decode_text(self):
        # decode_records() decodes all records in one go; it must split them
        # exactly where the records are, whatever units they hold.
        rng = random.Random(1)
        units = [0, 5, 10, 13, 0x0F, 0x12, 0xFFFB, 0xD83D, 0xDE00, 0xDC00, 0xD800, 0x41, 0x430, 0x98, 0xE9]
        for _ in range(3000):
            enc = rng.choice(list(Encoding))
            mask = 0xFFFF if enc is Encoding.UCS2 else 0xFF
            records = ["".join(chr(rng.choice(units) & mask) for _ in range(rng.randrange(8)))
                       for _ in range(rng.randrange(1, 6))]
            records = "\r\n".join(records).split("\r\n")  # as split_records would give them
            self.assertEqual(decode_records(records, enc), [decode_text(r, enc) for r in records])


class IntTest(unittest.TestCase):
    def test_engine_semantics(self):
        self.assertEqual(parse_int("42"), (42, True))
        self.assertEqual(parse_int("-7"), (-7, True))
        self.assertEqual(parse_int(""), (0, False))
        self.assertEqual(parse_int(" 12 "), (12, False))
        self.assertEqual(parse_int("+3"), (3, False))
        self.assertEqual(parse_int("12a"), (0, False))
        self.assertEqual(parse_int("٣"), (0, False))  # only ASCII digits count

    def test_reader_notes_non_canonical(self):
        r = RecordReader([encode_text(" 5", Encoding.UCS2)], Encoding.UCS2)
        self.assertEqual(r.int("value"), 5)
        self.assertEqual(len(r.issues), 1)

    def test_reader_truncated(self):
        r = RecordReader([], Encoding.UCS2)
        with self.assertRaises(SaveFormatError):
            r.int("value")


class TablesTest(unittest.TestCase):
    def test_crc_59_is_crc32(self):
        self.assertEqual(tables.crc_59(b"123456789"), tables._to_int32(0xCBF43926))

    def test_crc_570_known_values(self):
        # Computed by the engine for the fixtures (see test_formats); here just
        # pin the arithmetic-shift behaviour on a negative intermediate value.
        self.assertEqual(tables.crc_570(b""), 0)
        self.assertEqual(tables.crc_570(b"\x00"), tables._to_int32(0x00000000 ^ 0xD202EF8D))

    def test_crc_570_matches_byte_by_byte(self):
        # crc_570() works eight bytes at a time; check it against the engine's
        # loop, one byte per step, on every tail length and on long data.
        def reference(data: bytes) -> int:
            crc = 0
            for byte in data:
                crc = tables._crc_570_step(crc, byte)
            return tables._to_int32(crc)

        rng = random.Random(2)
        for length in list(range(40)) + [1000, 4099]:
            data = bytes(rng.randrange(256) for _ in range(length))
            self.assertEqual(tables.crc_570(data), reference(data), length)
        self.assertEqual(tables.crc_570(b"\xff" * 64), reference(b"\xff" * 64))

    def test_upper(self):
        self.assertEqual(tables.upper_59("абвёxyz"), "АБВЁXYZ")
        self.assertEqual(tables.upper_570("абвёxyz"), "АБВЁXYZ")
        self.assertEqual(tables.upper_59("😀"), "😀")

    def test_hashes_are_in_range(self):
        for name in ("A", "NAME", "ПЕРЕМЕННАЯ", "V300"):
            self.assertTrue(0 <= tables.var_bucket_59(name) < tables.VARS_GLOBAL_BUCKETS_59)
            self.assertEqual(tables.var_window_570(name) % tables.VARS_SEEK_570, 0)


if __name__ == "__main__":
    unittest.main()
