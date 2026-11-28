import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from job_agent.config.yaml_patch import patch_value


class BlockListPatchTest(unittest.TestCase):
    def test_block_list_replaced_inline_stays_valid(self):
        text = 'collect:\n  cities:\n  - 苏州\n  - 上海\n  task_space_name: x\n'
        out = patch_value(text, ['collect', 'cities'], ['苏州', '全国'])
        self.assertEqual(yaml.safe_load(out), {'collect': {'cities': ['苏州', '全国'], 'task_space_name': 'x'}})

    def test_block_mapping_replaced_inline_stays_valid(self):
        text = 'a:\n  b:\n    c: 1\n  d: 2\n'
        self.assertEqual(yaml.safe_load(patch_value(text, ['a', 'b'], {'e': 3})), {'a': {'b': {'e': 3}, 'd': 2}})

    def test_invalid_yaml_never_reaches_disk(self):
        from job_agent.service import settings
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, JOB_AGENT_HOME=home):
            Path(home, 'config.yaml').write_text('collect:\n  cities:\n  - 苏州\n')
            writes = []
            real = settings.atomic_write
            with patch.object(settings, 'patch_value', return_value='collect:\n  cities:\n  [x]\n  y: 1\n'), \
                 patch.object(settings, 'atomic_write', side_effect=lambda *a, **k: (writes.append(a[0]), real(*a, **k))):
                with self.assertRaises(ValueError):
                    settings.update_settings({'collect.cities': ['x']}, confirm=True, actor='t', reason='t', source='cli')
            self.assertEqual(writes, [])
            self.assertEqual(Path(home, 'config.yaml').read_text(), 'collect:\n  cities:\n  - 苏州\n')


if __name__ == '__main__':
    unittest.main()
