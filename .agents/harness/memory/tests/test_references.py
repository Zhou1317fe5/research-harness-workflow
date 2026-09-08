"""证据引用与来源采集的边界回归；全部使用隔离目录。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from harness.memory.research_memory import Memory, MemoryError, source_name


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='memory-reference-test-')
        self.root = Path(self.temporary.name)
        self.memory = Memory(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def make_file(self, relative, text='evidence'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_structured_evidence_is_referenceable(self):
        for relative in ('research_workspace/analysis/comparison.json',
                         'issues/example/validation/metrics.json',
                         'remote_artifacts/EXAMPLE/run/summary.json',
                         'research_workspace/figures/plot.svg',
                         'docs/reviews/checks.csv'):
            self.make_file(relative)
            self.assertEqual(self.memory.reference(relative), relative)

    def test_reference_does_not_read_or_register_contents(self):
        relative = 'research_workspace/analysis/comparison.json'
        self.make_file(relative)
        sources = list(self.memory.config['sources'])
        with patch.object(Path, 'read_text', side_effect=AssertionError('unexpected read')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected read')):
            self.memory.reference(relative)
        self.assertEqual(self.memory.config['sources'], sources)
        self.assertEqual(self.memory.scan(), [])

    def test_automatic_sources_keep_their_format_boundary(self):
        for relative in ('research_workspace/analysis/comparison.json',
                         'issues/example/check.md',
                         'remote_artifacts/EXAMPLE/run/summary.json'):
            with self.assertRaises(MemoryError):
                source_name(relative)
        self.assertEqual(source_name('research_workspace/analysis/note.md'),
                         'research_workspace/analysis/note.md')

    def test_existing_markdown_fragments_are_preserved(self):
        self.make_file('research_workspace/CONCLUSIONS.md')
        value = 'research_workspace/CONCLUSIONS.md#c001'
        self.assertEqual(self.memory.reference(value), value)

    def test_rejects_missing_unsafe_and_internal_paths(self):
        for relative in ('research_workspace/.env', 'issues/example/.env.local',
                         'research_workspace/.git/config'):
            self.make_file(relative)
        for value in ('/tmp/example.json', '../outside.json',
                      'issues/example/../metrics.json', 'issues\\example.json',
                      'issues/example\n.json', 'issues/missing.json',
                      'research_workspace/.env', 'issues/example/.env.local',
                      'research_workspace/.git/config'):
            with self.assertRaises(MemoryError):
                self.memory.reference(value)

    def test_symlinks_cannot_escape_evidence_roots(self):
        target = self.make_file('.agents/private.json')
        link = self.root / 'issues/example/link.json'
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        with self.assertRaises(MemoryError):
            self.memory.reference('issues/example/link.json')
        with tempfile.TemporaryDirectory(prefix='external-reference-test-') as outside:
            external = Path(outside) / 'result.json'
            external.write_text('{}')
            link.unlink()
            link.symlink_to(external)
            with self.assertRaises(MemoryError):
                self.memory.reference('issues/example/link.json')

    def test_in_project_artifact_alias_remains_supported(self):
        target = self.make_file('remote_artifacts/EXAMPLE/run/model.pt')
        link = self.root / 'remote_artifacts/EXAMPLE/run/best.pt'
        link.symlink_to(target)
        self.assertEqual(self.memory.reference('remote_artifacts/EXAMPLE/run/best.pt'),
                         'remote_artifacts/EXAMPLE/run/best.pt')

    def test_supported_result_accepts_json_and_updates_state(self):
        first = 'issues/example/validation/result.json'
        second = 'research_workspace/analysis/comparison.json'
        self.make_file(first, json.dumps({'complete': True}))
        self.make_file(second, json.dumps({'delta': 0.0}))
        event = self.memory.capture('assistant', '评估产物已生成。', session_id='fixture')
        ids = self.memory.process({'event_id': event, 'disposition': 'recorded', 'records': [{
            'kind': 'finding', 'status': 'SUPPORTED', 'scope': 'validation.result',
            'summary': '结果有可核对的结构化证据。', 'protocol': 'fixture-protocol',
            'state_slot': 'verified_result', 'evidence': [first, second],
        }]})
        self.assertEqual(ids, ['C001'])
        self.assertIn('C001', (self.root / 'research_workspace/STATE.md').read_text())
        conclusions = (self.root / 'research_workspace/CONCLUSIONS.md').read_text()
        self.assertIn(first, conclusions)
        self.assertIn(second, conclusions)


if __name__ == '__main__':
    unittest.main()
