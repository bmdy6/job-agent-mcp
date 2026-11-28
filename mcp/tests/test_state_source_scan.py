"""Keep runtime JSON filenames confined to the store/migration boundary."""
import ast
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]/'job_agent'
# These are configuration, immutable deployment metadata and user-authored documents.
ALLOWED={'prompts/vocabulary.json','vocabulary.json','daily.env.json','deployment.json','~/.config/ai-vault/vault.json'}



def violations():
    result=[]
    for path in ROOT.rglob('*.py'):
        if path.relative_to(ROOT).parts[0]=='store':continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node,ast.Constant) or not isinstance(node.value,str):continue
            value=node.value
            # JS response.json(), JSON MIME types and embedded scripts are not filenames.
            if '\n' in value or value in ALLOWED:continue
            if path.relative_to(ROOT).as_posix()=='service/settings_history.py' and value=='changes.jsonl':continue  # config-history audit stays on disk (#106)
            if path.name=='user_data.py' and value=='.json':continue  # company research documents
            if re.fullmatch(r'[\w.*~/-]*\.jsonl?',value):
                result.append((str(path.relative_to(ROOT)),node.lineno,value))
    return result


class StateSourceScanTest(unittest.TestCase):
    def test_all_runtime_files_are_store_owned(self):
        self.assertEqual(violations(),[])
