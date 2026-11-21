"""Offline driver refresh checks: source identity, rollback and strict SDK calls."""
import base64
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from evaluation.sql.postgres_cloud_refresh import (
    SOURCES, allowed_paths, exact_sources, refresh, refresh_script,
)


class CloudRefreshTests(unittest.TestCase):
    def payload(self):
        before={'lambda_code_sha256':'old','region':'region','lambda_function':'function'}
        after={**before,'lambda_code_sha256':'new'}
        files={name:{'text':'# verified '+name+'\n','sha256':hashlib.sha256(('# verified '+name+'\n').encode()).hexdigest()}
               for name in allowed_paths()}
        return {'label':'ca-fix-01','before':before,'after':after,'files':files}

    def run_remote(self,prefix,payload,check_ok=True):
        repo=prefix/'repo'
        for name in allowed_paths():
            path=repo/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('# previous '+name+'\n')
        (repo/'comparison').mkdir()
        (repo/'comparison/preflight.json').write_text(json.dumps(payload['before']))
        program=refresh_script(payload).split("<<'REFRESH'\n")[1].split('\nREFRESH\n')[0]
        program=program.replace('/opt/sebs-pg-driver/repo',str(repo)).replace('/opt/sebs-pg-driver',str(prefix))
        client=SimpleNamespace(get_function_configuration=Mock(return_value={'CodeSha256':'new'}))
        def aws_client(service_name,*,region_name):
            self.assertEqual(service_name,'lambda')
            self.assertEqual(region_name,'region')
            return client
        old=os.getcwd()
        try:
            with patch.dict('sys.modules',{'boto3':SimpleNamespace(client=aws_client)}), \
                 patch('subprocess.run',return_value=SimpleNamespace(returncode=0 if check_ok else 1,stderr='fixture')) as check, \
                 redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                if check_ok:
                    exec(compile(program,'driver-refresh','exec'),{})
                    self.assertIn('verify-local',check.call_args.args[0])
                else:
                    with self.assertRaisesRegex(RuntimeError,'readiness'):
                        exec(compile(program,'driver-refresh','exec'),{})
        finally:
            os.chdir(old)
        return repo

    def test_refresh_installs_verified_twins_and_retains_previous_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix=Path(temp)
            payload=self.payload()
            repo=self.run_remote(prefix,payload)
            for name,entry in payload['files'].items():
                self.assertEqual((repo/name).read_text(),entry['text'])
                self.assertEqual((prefix/'refreshes/ca-fix-01/before'/name).read_text(),'# previous '+name+'\n')
            self.assertEqual(json.loads((repo/'comparison/preflight.json').read_text()),payload['after'])
            report=json.loads((prefix/'refreshes/ca-fix-01/report.json').read_text())
            self.assertEqual(report['status'],'DRIVER_REFRESHED')
            self.assertEqual(report['benchmark_queries_submitted'],0)

    def test_readiness_failure_restores_all_sources_and_previous_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix=Path(temp)
            payload=self.payload()
            repo=self.run_remote(prefix,payload,check_ok=False)
            for name in allowed_paths():
                self.assertEqual((repo/name).read_text(),'# previous '+name+'\n')
            self.assertEqual(json.loads((repo/'comparison/preflight.json').read_text()),payload['before'])
            self.assertFalse((prefix/'refreshes/ca-fix-01/report.json').exists())

    def test_source_manifest_rejects_modified_zip_or_local_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            archive=root/'package.zip'
            with ZipFile(archive,'w') as package:
                for member,source in SOURCES.items():
                    path=root/source
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text('# exact '+member)
                    package.writestr(member,path.read_bytes())
                package.writestr('.python_packages/lib/site-packages/psycopg-3.3.9.dist-info/METADATA','')
            sha=base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()
            files=exact_sources(archive,sha,root)
            self.assertEqual(set(files),set(allowed_paths()))
            self.assertEqual(files['driver_function/sql.py'],files[SOURCES['function/sql.py']])
            with self.assertRaisesRegex(ValueError,'Cached package differs'):
                exact_sources(archive,'wrong',root)
            (root/SOURCES['function/sql.py']).write_text('changed')
            with self.assertRaisesRegex(ValueError,'source differs'):
                exact_sources(archive,sha,root)

    def test_running_comparison_prevents_refresh_before_any_file_or_sdk_change(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix=Path(temp)
            repo=prefix/'repo'
            repo.mkdir()
            run=prefix/'runs/active'
            run.mkdir(parents=True)
            (run/'status.json').write_text('{"state":"running"}')
            program=refresh_script(self.payload()).split("<<'REFRESH'\n")[1].split('\nREFRESH\n')[0]
            program=program.replace('/opt/sebs-pg-driver/repo',str(repo)).replace('/opt/sebs-pg-driver',str(prefix))
            aws=SimpleNamespace(client=Mock())
            old=os.getcwd()
            try:
                with patch.dict('sys.modules',{'boto3':aws}):
                    with self.assertRaisesRegex(RuntimeError,'still running'):
                        exec(compile(program,'guarded-refresh','exec'),{})
            finally:
                os.chdir(old)
            aws.client.assert_not_called()
            self.assertEqual(list(repo.iterdir()),[])

    def test_full_refresh_uses_strict_sdk_arguments_and_updates_profile_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            folder=root/'state'
            folder.mkdir()
            before={'lambda_code_sha256':'old','region':'region','lambda_function':'function',
                    'stack_id':'sql-stack','server_instance_id':'server'}
            trace={'settings':{'memory':512}}
            for name,data in {'preflight.json':before,'instance.json':{'stack_id':'driver-stack','instance_id':'driver'},
                              'trace.json':trace,'launch-request.json':{'ClientRequestToken':'owned'}}.items():
                (folder/name).write_text(json.dumps(data))
            storage=root/'storage.json'
            storage.write_text('{"sql":{}}')
            outputs={'InstanceId':'server'}
            cf=Mock()
            cf.describe_stacks.return_value={'Stacks':[{'StackStatus':'CREATE_COMPLETE','Tags':[{'Key':'sebs-driver-owner','Value':'owned'}]}]}
            live=Mock()
            live.get_function_configuration.return_value={'CodeSha256':'new','Timeout':300}
            ssm=Mock()
            files=self.payload()['files']
            def submit(**kwargs):
                self.assertEqual(json.loads((folder/'preflight.json').read_text())['lambda_code_sha256'],'old')
                self.assertIsNone(json.loads((folder/'refreshes/ca-fix-01/request.json').read_text())['command_id'])
                return {'Command':{'CommandId':'refresh-once'}}
            ssm.send_command.side_effect=submit
            report={'status':'DRIVER_REFRESHED','lambda_code_sha256':'new',
                    'source_sha256':{name:entry['sha256'] for name,entry in files.items()}}
            ssm.get_command_invocation.return_value={'Status':'Success','StandardOutputContent':json.dumps(report)}
            def aws_client(service_name,*,region_name,config=None):
                self.assertEqual(region_name,'region')
                return {'ssm':ssm,'lambda':live,'cloudformation':cf}[service_name]
            session=SimpleNamespace(client=aws_client)
            with patch('evaluation.sql.postgres_cloud_refresh.boto3.Session',return_value=session), \
                 patch('evaluation.sql.postgres_cloud_refresh.sql_config',return_value=SimpleNamespace(provider='aws',region='region')), \
                 patch('evaluation.sql.postgres_cloud_refresh.AWSPostgreSQL') as backend, \
                 patch('evaluation.sql.postgres_cloud_refresh.verify_lambda'), \
                 patch('evaluation.sql.postgres_cloud_refresh.exact_sources',return_value=files):
                backend.return_value.info.return_value=({}, {'StackId':'sql-stack'},outputs)
                refresh(folder,storage,root/'cache','ca-fix-01')
                refresh(folder,storage,root/'cache','ca-fix-01')
            self.assertEqual(ssm.send_command.call_count,1)
            self.assertEqual(json.loads((folder/'preflight.json').read_text())['lambda_code_sha256'],'new')
            self.assertEqual(json.loads((folder/'refreshes/ca-fix-01/before-preflight.json').read_text())['lambda_code_sha256'],'old')

    def test_refresh_shell_syntax_and_no_cloud_keys_in_payload(self):
        source=refresh_script(self.payload())
        result=subprocess.run(['bash','-n'],input=source,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',source)


if __name__=='__main__':
    unittest.main()
