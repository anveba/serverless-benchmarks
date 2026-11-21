"""Offline native cold/warm configuration and reference-validation checks."""
import json
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from evaluation.sql.postgres_cold_warm import QUERIES,configuration,validate_query,run,summarize
from evaluation.sql.postgres_cloud_preflight import ROOT
from sebs.experiments.config import Config
from sebs.experiments.perf_cost import PerfCost
from sebs.faas.function import ExecutionResult


class PostgresColdWarmTests(unittest.TestCase):
    def test_complete_native_sampler_to_validation_and_usage_summary_without_cloud(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            storage_state=root/'storage'
            (storage_state/'datasets').mkdir(parents=True)
            (storage_state/'datasets/sebs_720_fixture.json').write_text(json.dumps({
                'namespace':'sebs_720_fixture','fingerprint':'f'*64,
                'metadata':{'scale_factor':1,'input_size':'small','workload':'TPC-H'}}))
            attachment=root/'storage.json'
            attachment.write_text(json.dumps({'sql':{'postgresql':{
                'provider':'aws','region':'us-east-1','state_dir':str(storage_state)}}}))
            output=root/'results'
            def native(command,*,cwd,env,check):
                self.assertNotIn('SEBS_SQL_SCALE_FACTOR',env)
                config=json.loads(Path(command[command.index('--config')+1]).read_text())
                settings=config['experiments']['perf-cost']
                query=config['experiments']['benchmark-inputs']['720.sql-postgresql']['queries'][0]
                path=Path(command[command.index('--output-dir')+1])/'perf-cost'
                path.mkdir(parents=True)
                experiment=PerfCost(Config.deserialize(config['experiments']))
                experiment._out_dir=str(path)
                state={'cold':False,'index':0}
                def enforce(*args):state['cold']=True
                experiment._deployment_client=SimpleNamespace(config=None,enforce_cold_start=enforce)
                experiment._function=SimpleNamespace(name='function')
                experiment._benchmark=object()
                experiment._benchmark_input={'queries':[query]}
                def invoke(event):
                    value=ExecutionResult.from_times(datetime.now(),datetime.now())
                    value.request_id=query+'-'+str(state['index'])
                    state['index']+=1
                    value.stats.cold_start=state['cold']
                    state['cold']=False
                    value.provider_times.execution=1_000_000
                    value.provider_times.initialization=70_000 if value.stats.cold_start else 0
                    value.billing.billed_time=1000
                    value.billing.memory=512
                    value.billing.gb_seconds=.5
                    value.output={'result':{'result':{'queries':{query:[[42]]}},'measurement':{query:{
                        'query_time':500000,'credential_time':1000,'connect_time':1000}}}}
                    return value
                experiment._trigger=SimpleNamespace(sync_invoke=invoke)
                experiment.compute_statistics=Mock()
                experiment.run_configuration(settings,settings['repetitions'],'512')
            with patch('evaluation.sql.postgres_cold_warm.official_answers',return_value={q:[['42']] for q in QUERIES}), \
                 patch('evaluation.sql.postgres_cold_warm.subprocess.run',side_effect=native) as invoke, \
                 patch('sebs.experiments.perf_cost.time.sleep'):
                run(ROOT/'configs/sql-postgresql-aws.example.json',attachment,root/'cache',output,2)
                result=summarize(output)
                self.assertEqual(invoke.call_count,5)
                self.assertEqual(result['all_attempts_gb_seconds'],15)
                for query in QUERIES:
                    self.assertEqual(result['queries'][query]['cold']['samples'],2)
                    self.assertEqual(result['queries'][query]['warm']['samples'],2)
                # A repeated top-level command may inspect state but cannot run queries again.
                run(ROOT/'configs/sql-postgresql-aws.example.json',attachment,root/'cache',output,2)
                self.assertEqual(invoke.call_count,5)
    def test_config_uses_private_sdk_sf1_fresh_and_complete_bounded_attempts(self):
        base=json.loads((ROOT/'configs/sql-postgresql-aws.example.json').read_text())
        config=configuration(base,'q12',10)
        settings=config['experiments']['perf-cost']
        self.assertEqual(settings['trigger'],'library')
        self.assertEqual(settings['input-size'],'small')
        self.assertEqual(settings['experiments'],['cold','warm'])
        self.assertEqual(settings['repetitions'],10)
        self.assertEqual(settings['memory-sizes'],[512])
        self.assertEqual(settings['max-attempts'],33)
        native=Config.deserialize(config['experiments'])
        self.assertEqual(native.experiment_settings('perf-cost')['trigger'],'library')
        self.assertEqual(config['experiments']['benchmark-inputs']['720.sql-postgresql']['queries'],['q12'])
        self.assertEqual(base['experiments']['type'],'sql-workload')

    def test_validation_checks_all_attempts_including_excluded_initial_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            directory=folder/'q6/perf-cost'
            directory.mkdir(parents=True)
            def result(cold,value):
                return {'stats':{'failure':False,'cold_start':cold},
                        'output':{'result':{'result':{'queries':{'q6':[[value]]}}}}}
            for phase in ['cold','warm']:
                retained=[result(phase=='cold',42) for _ in range(2)]
                attempts=[{'warmup':True,'result':result(True,42)}]+[{'warmup':False,'result':v} for v in retained]
                data={'_invocations':{'function':{str(i):v for i,v in enumerate(retained)}},'statistics':{'attempts':attempts}}
                (directory/(phase+'_results_512.json')).write_text(json.dumps(data))
            self.assertTrue(all(v['valid'] for v in validate_query(folder,'q6',{'q6':[['42']]},2)))
            path=directory/'cold_results_512.json'
            data=json.loads(path.read_text())
            data['statistics']['attempts'][0]['result']['output']['result']['result']['queries']['q6']=[[99]]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'Invalid q6'):
                validate_query(folder,'q6',{'q6':[['42']]},2)


if __name__=='__main__':
    unittest.main()
