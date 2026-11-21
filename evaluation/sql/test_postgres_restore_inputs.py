"""Exact source restoration, verification and local rollback without cloud calls."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from evaluation.sql.postgres_restore_inputs import METADATA, ROOT, restore_cache
from sebs.benchmark import load_benchmark_input
from sebs.storage.postgresql import build_dataset


class RestoreInputTests(unittest.TestCase):
    def fixture(self,root):
        declaration=load_benchmark_input(str(ROOT/'benchmarks/700.analytics/720.sql-postgresql')).allocate_sql()
        sources, tables={},{}
        for table,keys in declaration.items():
            paths=[]
            # 13 fragments exercise lexical 1/10/2 ordering without relying on names.
            for index in range(13 if table=='lineitem' else 1):
                data=(table+' original '+str(index)).encode()
                path=root/(table+'-'+str(index)+'.parquet')
                path.write_bytes(data)
                paths.append(path)
                sources[(table,hashlib.sha256(data).hexdigest())]=data
            tables[table]={'files':paths,'primary_key':keys}
        dataset=build_dataset('fixture','720.sql-postgresql','small',tables,METADATA)
        report={'namespace':dataset['namespace'],'fingerprint':dataset['fingerprint'],'metadata':METADATA,
                'tables':{name:{'primary_key':spec['primary_key'],'sha256':[v['sha256'] for v in spec['files']]}
                          for name,spec in dataset['tables'].items()}}
        return report,sources

    def test_restore_preserves_exact_manifest_order_and_backs_up_current_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report,sources=self.fixture(root)
            destination=root/'cache/small'
            (destination/'customer').mkdir(parents=True)
            (destination/'customer/new.parquet').write_bytes(b'changed local bytes')
            def fetch(table,digest,target):target.write_bytes(sources[(table,digest)])
            result=restore_cache(report,destination,fetch)
            self.assertTrue((Path(result['backup'])/'customer/new.parquet').exists())
            keys=load_benchmark_input(str(ROOT/'benchmarks/700.analytics/720.sql-postgresql')).allocate_sql()
            actual=build_dataset('fixture','720.sql-postgresql','small',
                {t:{'files':sorted((destination/t).glob('*.parquet')),'primary_key':k} for t,k in keys.items()},METADATA)
            self.assertEqual(actual['fingerprint'],report['fingerprint'])
            again=restore_cache(report,destination,lambda *args:self.fail('Must reuse identical local bytes'))
            self.assertEqual(again['downloaded_files'],0)

    def test_corrupt_download_leaves_existing_cache_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report,sources=self.fixture(root)
            destination=root/'cache/small'
            destination.mkdir(parents=True)
            sentinel=destination/'keep.txt'
            sentinel.write_text('unchanged')
            with self.assertRaisesRegex(ValueError,'SHA256'):
                restore_cache(report,destination,lambda table,digest,path:path.write_bytes(b'corrupted'))
            self.assertEqual(sentinel.read_text(),'unchanged')

    def test_inconsistent_recorded_fingerprint_prevents_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            report,sources=self.fixture(root)
            report['fingerprint']='0'*64
            with self.assertRaisesRegex(ValueError,'inconsistent'):
                restore_cache(report,root/'cache',lambda *args:self.fail('No fetch permitted'))


if __name__=='__main__':
    unittest.main()
