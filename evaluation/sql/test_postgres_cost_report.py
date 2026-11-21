"""Offline financial model checks; no AWS clients or database queries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.sql.postgres_cost_report import (rate, volume_monthly, known_lambda_usage,
                                                workload, campaign, allocation_hourly)


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value))


def rates():
    return {k:{'usd_per_unit':v} for k,v in {'gp3_gb_month':.08, 'gp3_gibps_month':40.96,
        'gp3_iops_month':.005, 'compute_m7i-flex.large':.1, 'ipv4_hour':.005,
        'endpoint_hour':.01, 'secret_month':.4, 'lambda_gb_second':.00002,
        'lambda_request':.0000002}.items()}


def inventory():
    volume = {'VolumeId':'v','VolumeType':'gp3','Size':64,'Iops':3000,'Throughput':125,
              'CreateTime':'2026-11-01T00:00:00Z'}
    resource = {'instance':{'InstanceId':'i','InstanceType':'m7i-flex.large',
                 'LaunchTime':'2026-11-01T00:00:00Z','PublicIpAddress':'ip'},'volumes':[volume]}
    return {'captured_at':'2026-11-01T01:30:00Z',
        'resources':{'database':resource,'driver':copy.deepcopy(resource)},
        'endpoint':{'SubnetIds':['subnet'],'CreationTimestamp':'2026-11-01T00:00:00Z'},
        'secret':{'CreatedDate':'2026-11-01T00:00:00Z'}}


class CostReportTests(unittest.TestCase):
    def test_selects_paid_first_tier_and_rejects_ambiguity(self):
        def dim(begin,price):return {'beginRange':begin,'unit':'GBs','pricePerUnit':{'USD':price}}
        product = {'product':{'sku':'x','attributes':{'usagetype':'compute'}},
            'terms':{'OnDemand':{'term':{'effectiveDate':'2026-11-01','priceDimensions':{
                'first':dim('0','.02'),'later':dim('6000','.01')}}}}}
        self.assertEqual(rate({'products':[product]},'compute','GBs')['usd_per_unit'],.02)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            rate({'products':[product,copy.deepcopy(product)]},'compute','GBs')
        with self.assertRaisesRegex(ValueError,'Missing'):
            rate({'products':[product]},'compute','wrong-unit')

    def test_throughput_catalog_unit_is_gib_per_second_not_mib_per_second(self):
        volume={'VolumeType':'gp3','Size':64,'Iops':3000,'Throughput':500}
        self.assertAlmostEqual(volume_monthly(volume,rates()),64*.08+375*.04)
        volume['Throughput']=125
        self.assertAlmostEqual(volume_monthly(volume,rates()),64*.08)

    def test_native_ledger_deduplicates_warmups_processed_and_failed_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            measured={'request_id':'one','billing':{'_billed_time':2000,'_gb_seconds':1}}
            initial={'request_id':'two','billing':{'_billed_time':4000,'_gb_seconds':2}}
            data={'_invocations':{'f':{'one':measured}},'statistics':{'attempts':[{'result':initial},{'result':measured}]}}
            write(root/'postgresql-aws-cold/cold_results_512.json',data)
            write(root/'postgresql-aws-cold/results.json',data)
            write(root/'postgresql-aws-missing/results.json',{'_invocations':{'f':{'missing':{
                'request_id':'missing','billing':{'_billed_time':None,'_gb_seconds':None}}}}})
            write(root/'postgresql-aws-cloud/runs/bad/lambda-native-report-recovery.json',{'reports':[{'request_id':'three','gb_seconds':3}]})
            usage=known_lambda_usage(root,root/'postgresql-aws-cloud')
            self.assertEqual(usage['requests_with_native_billing'],3)
            self.assertEqual(usage['gb_seconds'],6)
            self.assertEqual(usage['request_ids_without_saved_billing'],['missing'])
            self.assertFalse(usage['coverage_complete'])
            initial['billing']['_gb_seconds']=20
            write(root/'postgresql-aws-cold/results.json',data)
            with self.assertRaisesRegex(ValueError,'Conflicting'):
                known_lambda_usage(root,root/'postgresql-aws-cloud')

    def test_window_includes_database_for_both_paths_and_driver_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            inv=inventory(); prices=rates(); hourly=allocation_hourly(inv,prices)
            trace={'requests':[{'query':'q6'}], 'settings':{'max-in-flight':2},'input':{'input_fingerprint':'fp'}}
            settings={'repetitions':1,'trace':trace,'allocation':{'instance_id':'i','instance_type':'m7i-flex.large',
                      'volume':inv['resources']['database']['volumes'][0]},'plan':{'lambda_code_sha256':'sha'}}
            folder=root/'runs/run/collected'
            write(folder/'settings.json',settings)
            write(folder/'status.json',{'state':'complete','completed_runs':2})
            for backend in ['postgresql','lambda']:
                target=folder/'repeat-1'/backend
                write(target/'trace.json',{'trace':trace})
                write(target/'summary.json',{'submitted':1,'valid':1,'execution_failures':0,'observation_seconds':100})
                write(target/'requests.json',[{'index':0,'valid':True,'failure':False}])
                write(target/'responses.json',{'0':{'provider_metrics':{'native_report_available':True,'billed_ms':2000,'gb_seconds':1}}})
            result=workload(root,'run',inv,prices,hourly)['backends']
            direct,indirect=result['postgresql'],result['lambda']
            self.assertGreater(direct['database_compute_disk_ipv4_usd'],0)
            self.assertAlmostEqual(direct['database_compute_disk_ipv4_usd'],indirect['database_compute_disk_ipv4_usd'])
            self.assertAlmostEqual(indirect['modeled_subtotal_usd']-direct['modeled_subtotal_usd'],.0000202)
            self.assertGreater(indirect['driver_instrumentation_usd_separate'],0)
            write(folder/'repeat-1/lambda/requests.json',[{'index':0,'valid':False,'failure':True}])
            with self.assertRaisesRegex(ValueError,'Invalid'):
                workload(root,'run',inv,prices,hourly)

    def test_campaign_rounds_endpoint_once_and_accounts_recorded_treatment(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); inv=inventory();prices=rates()
            write(folder/'apply-response.json',{'VolumeId':'v','OriginalThroughput':125,'TargetThroughput':500,'StartTime':'2026-11-01T00:30:00Z'})
            write(folder/'restore-response.json',{'VolumeId':'v','OriginalThroughput':500,'TargetThroughput':125,'StartTime':'2026-11-01T01:00:00Z'})
            report=campaign(inv,prices,allocation_hourly(inv,prices),folder)
            self.assertAlmostEqual(report['components_usd']['endpoint_provisioned_hours'],.02)
            self.assertAlmostEqual(report['components_usd']['recorded_extra_gp3_throughput'],15*.5/720)
            self.assertFalse(report['actual_invoice_complete'])


if __name__=='__main__':
    unittest.main()
