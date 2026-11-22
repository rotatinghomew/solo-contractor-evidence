import json
from pathlib import Path
import unittest


class RecordsTest(unittest.TestCase):
    def test_known_input_contract(self):
        source = Path(__file__).resolve().parents[1] / "data" / "records.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        promises = {(item['appointment'], item['part']): item for item in payload['promises']}
        assert all((item['appointment'], item['part']) in promises for table in ('evidence', 'transfers', 'claims', 'ledger') for item in payload[table])
        assert all(item['policy'] in payload['policies'] for item in promises.values())
        assert {'已付', '审核'} == {item['status'] for item in payload['ledger']}
        assert any(item['approved_day'] is None for item in payload['claims'])
