"""Offline exercise of the Azure native sampler, validation and replay guard."""
import io
import json
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluation.sql.postgres_azure_cold_warm import configuration, run, summarize, validate_query
from evaluation.sql.postgres_azure_baseline import ROOT, QUERIES, save
from sebs.experiments.config import Config
from sebs.experiments.perf_cost import PerfCost
from sebs.faas.function import ExecutionResult


class AzurePostgresColdWarmTests(unittest.TestCase):
    def test_native_sampling_validates_all_attempts_and_never_invents_billing(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root=Path(temp);state=root/'storage';output=root/'output'
            base=json.loads((ROOT/'configs/sql-postgresql-azure.example.json').read_text())
            subscription=base['deployment']['azure']['credentials']['subscription_id']
            save(root/'config.json',base)
            save(state/'datasets/sebs_720_fixture.json',{'namespace':'fixture','fingerprint':'a'*64,
                'metadata':{'scale_factor':1,'input_size':'small','workload':'TPC-H'}})
            save(root/'storage.json',{'sql':{'postgresql':{'provider':'azure', 'region':'swedencentral',
                'azure_subscription_id':subscription,'state_dir':str(state)}}})
            def native(command,*,cwd,env,check):
                self.assertNotIn('SEBS_SQL_SCALE_FACTOR',env)
                config=json.loads(Path(command[command.index('--config')+1]).read_text())
                settings=config['experiments']['perf-cost']
                query=config['experiments']['benchmark-inputs']['720.sql-postgresql']['queries'][0]
                path=Path(command[command.index('--output-dir')+1])/'perf-cost';path.mkdir(parents=True)
                experiment=PerfCost(Config.deserialize(config['experiments']))
                experiment._out_dir=str(path)
                sample={'cold':False,'index':0}
                def enforce(*args):sample['cold']=True
                experiment._deployment_client=SimpleNamespace(config=None,enforce_cold_start=enforce)
                experiment._function=SimpleNamespace(name='function')
                experiment._benchmark=object();experiment._benchmark_input={'queries':[query]}
                def invoke(event):
                    value=ExecutionResult.from_times(datetime.now(),datetime.now())
                    value.request_id=query+'-'+str(sample['index']);sample['index']+=1
                    value.stats.cold_start=sample['cold'];sample['cold']=False
                    value.times.benchmark=1_000_000
                    value.output={'result':{'result':{'queries':{query:[[42]]}},'measurement':{query:{
                        'query_time':500000,'credential_time':1000,'connect_time':2000}}}}
                    return value
                experiment._trigger=SimpleNamespace(sync_invoke=invoke)
                experiment.compute_statistics=Mock()
                experiment.run_configuration(settings,settings['repetitions'])
            with patch('evaluation.sql.postgres_azure_cold_warm.official_answers',return_value={q:[['42']] for q in QUERIES}), \
                 patch('evaluation.sql.postgres_azure_cold_warm.subprocess.run',side_effect=native) as command, \
                 patch('sebs.experiments.perf_cost.time.sleep'):
                run(root/'config.json',root/'storage.json',root/'cache',output,3)
                report=summarize(output)
                self.assertEqual(command.call_count,5)
                self.assertEqual(report['all_attempts'],40)
                self.assertEqual(json.loads((output/'status.json').read_text())['state'],'complete')
                for query in QUERIES:
                    for phase in ('cold','warm'):
                        row=report['queries'][query][phase]
                        self.assertEqual(row['samples'],3)
                        self.assertEqual(row['function_s'],1)
                        self.assertIsNone(row['provider_s'])
                with self.assertRaisesRegex(ValueError,'already recorded'):
                    run(root/'config.json',root/'storage.json',root/'cache',output,3)
                self.assertEqual(command.call_count,5)
            # Even the excluded initial answer must validate.
            path=output/'q6/perf-cost/cold_results.json'
            data=json.loads(path.read_text())
            data['statistics']['attempts'][0]['result']['output']['result']['result']['queries']['q6']=[[99]]
            save(path,data)
            with self.assertRaisesRegex(ValueError,'Invalid q6'):
                validate_query(output,'q6',{q:[['42']] for q in QUERIES},3)

    def test_config_avoids_republishing_and_bounds_attempts(self):
        base=json.loads((ROOT/'configs/sql-postgresql-azure.example.json').read_text())
        value=configuration(base,'q1',3)
        settings=value['experiments']['perf-cost']
        self.assertEqual(settings['trigger'],'http')
        self.assertEqual(settings['memory-sizes'],[])
        self.assertEqual(settings['max-attempts'],12)
        self.assertEqual(settings['experiments'],['cold','warm'])
        self.assertEqual(base['experiments']['type'],'sql-workload')
        self.assertEqual(Config.deserialize(value['experiments']).experiment_settings('perf-cost')['repetitions'],3)
