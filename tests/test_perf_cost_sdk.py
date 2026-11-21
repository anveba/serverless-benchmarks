"""Native perf-cost SDK selection, complete attempt accounting and bounded failures."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sebs.experiments.config import Config
from sebs.experiments.perf_cost import PerfCost
from sebs.faas.function import ExecutionResult, Trigger, Runtime
from sebs.sebs_types import Language


class PerfCostSDKTests(unittest.TestCase):
    def test_sdk_and_default_http_trigger_selection(self):
        for name,kind in [('library',Trigger.TriggerType.LIBRARY),(None,Trigger.TriggerType.HTTP)]:
            with self.subTest(trigger=name),tempfile.TemporaryDirectory() as folder:
                settings={'benchmark':'720.sql-postgresql','input-size':'small'}
                if name:settings['trigger']=name
                config=Mock(update_storage=False)
                config.experiment_settings.return_value=settings
                experiment=PerfCost(config)
                client=Mock(output_dir=folder)
                deployment=Mock()
                trigger=Mock()
                deployment.get_function.return_value.triggers.return_value=[trigger]
                experiment.prepare(client,deployment)
                deployment.get_function.return_value.triggers.assert_called_once_with(kind)
                deployment.create_trigger.assert_not_called()
                self.assertIs(experiment._trigger,trigger)

    def invocation(self,index,cold=False,failure=False):
        result=ExecutionResult.from_times(datetime.now(),datetime.now())
        result.request_id='request-'+str(index)
        result.stats.cold_start=cold
        result.stats.failure=failure
        result.billing.billed_time=10
        result.billing.memory=512
        result.billing.gb_seconds=.005
        result.output={'result':{'result':{'queries':{'q6':[[42]]}}}}
        return result

    def experiment(self,folder,results):
        config=Config()
        config._runtime=Runtime(Language.PYTHON,'3.12')
        experiment=PerfCost(config)
        experiment._out_dir=str(folder)
        experiment._deployment_client=SimpleNamespace(config=None,enforce_cold_start=Mock())
        experiment._function=SimpleNamespace(name='function')
        experiment._benchmark=object()
        experiment._benchmark_input={'queries':['q6']}
        experiment._trigger=Mock()
        experiment._trigger.sync_invoke.side_effect=results
        experiment.compute_statistics=Mock()
        return experiment

    def test_warmups_and_excluded_cold_attempts_remain_in_billing_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment=self.experiment(folder,[self.invocation(0,True),self.invocation(1,True),
                                               self.invocation(2),self.invocation(3)])
            with patch('sebs.experiments.perf_cost.time.sleep'):
                experiment._run_configuration(PerfCost.RunType.WARM,
                    {'record-attempts':True,'fail-on-error':True,'max-attempts':8},1,2,'512')
            data=json.loads((Path(folder)/'warm_results_512.json').read_text())
            self.assertEqual(len(data['_invocations']['function']),2)
            attempts=data['statistics']['attempts']
            self.assertEqual(len(attempts),4)
            self.assertTrue(attempts[0]['warmup'])
            self.assertEqual(sum(v['result']['billing']['_gb_seconds'] for v in attempts),.02)
            experiment._trigger.sync_invoke.assert_called()
            self.assertEqual(experiment._trigger.sync_invoke.call_count,4)

    def test_execution_failure_is_saved_and_not_retried_even_during_warmup(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment=self.experiment(folder,[self.invocation(0,True,True)])
            with patch('sebs.experiments.perf_cost.time.sleep'):
                with self.assertRaisesRegex(RuntimeError,'without retry'):
                    experiment._run_configuration(PerfCost.RunType.COLD,
                        {'record-attempts':True,'fail-on-error':True,'max-attempts':8},1,2,'512')
            data=json.loads((Path(folder)/'cold_results_512.json').read_text())
            self.assertEqual(data['statistics']['invocation_attempts'],1)
            self.assertEqual(data['statistics']['failures_count'],1)
            self.assertTrue(data['statistics']['attempts'][0]['result']['stats']['failure'])
            experiment._trigger.sync_invoke.assert_called_once()

    def test_default_sampling_retains_original_result_shape_without_attempt_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment=self.experiment(folder,[self.invocation(i) for i in range(3)])
            with patch('sebs.experiments.perf_cost.time.sleep'):
                experiment._run_configuration(PerfCost.RunType.WARM,{},1,2,'512')
            data=json.loads((Path(folder)/'warm_results_512.json').read_text())
            self.assertEqual(len(data['_invocations']['function']),2)
            self.assertNotIn('attempts',data['statistics'])
            self.assertEqual(data['statistics']['samples_generated'],2)

    def test_attempt_limit_prevents_unbounded_wrong_classification_sampling(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment=self.experiment(folder,[self.invocation(i) for i in range(3)])
            with patch('sebs.experiments.perf_cost.time.sleep'):
                with self.assertRaisesRegex(RuntimeError,'max-attempts'):
                    experiment._run_configuration(PerfCost.RunType.COLD,
                        {'record-attempts':True,'fail-on-error':True,'max-attempts':3},1,2,'512')
            self.assertEqual(experiment._trigger.sync_invoke.call_count,3)
            data=json.loads((Path(folder)/'cold_results_512.json').read_text())
            self.assertEqual(len(data['statistics']['attempts']),3)


if __name__=='__main__':
    unittest.main()
