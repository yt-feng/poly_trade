import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from corpus_worker import detect_text_encoding


class CorpusWorkerEncodingTests(unittest.TestCase):
    def test_detects_utf8_without_loading_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'utf8.txt'
            path.write_text('BTC 五分钟策略\n', encoding='utf-8')
            self.assertEqual(detect_text_encoding(path), 'utf-8-sig')

    def test_detects_legacy_gb18030_chinese_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.txt'
            path.write_bytes('阻力支撑相对强度（RSRS）\n'.encode('gb18030'))
            self.assertEqual(detect_text_encoding(path), 'gb18030')

    def test_explicit_encoding_is_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.txt'
            path.write_bytes('文本'.encode('gb18030'))
            self.assertEqual(detect_text_encoding(path, 'gb18030'), 'gb18030')


if __name__ == '__main__':
    unittest.main()
