from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import asr


class ASRCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = {'enabled': True, 'provider': 'xiemac', 'model': 'large-v3-turbo',
                       'language': 'zh', 'chunk_seconds': 15, 'cache_dir': str(self.root)}
        self.url = 'https://www.youtube.com/watch?v=example1234'
        self.downloads = 0
        self.splits = 0
        self.calls = []
        self.fail_second = True
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(asr.shutil, 'which', return_value='ffmpeg'))
        self.enterContext(patch.object(asr.time, 'sleep'))
        self.enterContext(patch.object(asr, '_download_audio', side_effect=self.download))
        self.enterContext(patch.object(asr, '_chunk_audio', side_effect=self.split))
        self.enterContext(patch.object(asr, '_transcribe_xiemac_file', side_effect=self.transcribe))

    def download(self, url, directory, **kwargs):
        self.downloads += 1
        path = directory / 'audio.mp3'
        path.write_bytes(url.encode())
        return path

    def split(self, audio, directory, seconds):
        self.splits += 1
        paths = [directory / 'chunk-001.mp3', directory / 'chunk-002.mp3']
        for index, path in enumerate(paths):
            path.write_bytes(audio.read_bytes() + str(index).encode())
        return paths

    def transcribe(self, model, path, language):
        self.calls.append((model, path.name, language))
        if path.name == 'chunk-002.mp3' and self.fail_second:
            raise RuntimeError('xiemac ASR: remote_unavailable')
        return 'first' if path.name == 'chunk-001.mp3' else 'second'

    def run_asr(self):
        return asr.transcribe_video(self.url, self.config)

    def test_transient_failure_resumes_without_download_split_or_completed_chunk(self):
        first = self.run_asr()
        self.assertEqual((first.status, first.text), ('failed', ''))
        self.fail_second = False
        second = self.run_asr()
        self.assertEqual((second.status, second.text), ('success', 'first\n\nsecond'))
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.splits, 1)
        self.assertEqual([call[1] for call in self.calls], ['chunk-001.mp3', 'chunk-002.mp3', 'chunk-002.mp3'])

    def test_corrupt_receipt_text_is_retranscribed(self):
        self.run_asr()
        manifests = list(self.root.rglob('manifest.json'))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        manifest['chunks'][0]['text'] = 'corrupt'
        manifests[0].write_text(json.dumps(manifest))
        self.fail_second = False
        result = self.run_asr()
        self.assertEqual(result.text, 'first\n\nsecond')
        self.assertEqual([call[1] for call in self.calls].count('chunk-001.mp3'), 2)

    def test_corrupt_audio_chunk_or_manifest_is_not_accepted(self):
        for target in ('audio.mp3', 'chunk-001.mp3', 'manifest.json'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as cache:
                self.config['cache_dir'] = cache
                self.fail_second = False
                self.run_asr()
                paths = list(Path(cache).rglob(target))
                self.assertEqual(len(paths), 1)
                paths[0].write_bytes(b'corrupt')
                before = len(self.calls)
                result = self.run_asr()
                self.assertEqual(result.text, 'first\n\nsecond')
                self.assertGreater(len(self.calls), before)

    def test_identity_change_never_reuses_transcript_receipts(self):
        self.fail_second = False
        self.run_asr()
        for field, value in [('model', 'large-v3'), ('language', 'en'), ('chunk_seconds', 10)]:
            with self.subTest(field=field):
                self.config[field] = value
                before = len(self.calls)
                self.assertEqual(self.run_asr().status, 'success')
                self.assertEqual(len(self.calls), before + 2)
        self.url = 'https://www.youtube.com/watch?v=another1234'
        before = len(self.calls)
        self.assertEqual(self.run_asr().status, 'success')
        self.assertEqual(len(self.calls), before + 2)

    def test_manifest_identity_mismatch_is_rebuilt(self):
        self.fail_second = False
        self.run_asr()
        manifests = list(self.root.rglob('manifest.json'))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        manifest['identity']['provider'] = 'untrusted'
        manifests[0].write_text(json.dumps(manifest))
        before = len(self.calls)
        self.assertEqual(self.run_asr().text, 'first\n\nsecond')
        self.assertEqual(len(self.calls), before + 2)

    def test_truncated_chunk_plan_cannot_be_a_complete_transcript(self):
        self.fail_second = False
        self.run_asr()
        manifests = list(self.root.rglob('manifest.json'))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        manifest['chunks'].pop()
        manifests[0].write_text(json.dumps(manifest))
        result = self.run_asr()
        self.assertEqual(result.text, 'first\n\nsecond')

    def test_video_lock_prevents_duplicate_transcription(self):
        self.run_asr()
        locks = list(self.root.rglob('*.lock'))
        self.assertEqual(len(locks), 1)
        with locks[0].open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = len(self.calls)
            result = self.run_asr()
        self.assertEqual(result.status, 'failed')
        self.assertIn('cache_busy', result.error_message)
        self.assertEqual(len(self.calls), before)


if __name__ == '__main__':
    unittest.main()
