"""Argument-safe native Azure control operations, with secret-bearing output hidden."""
import io
import json
import os
import tarfile
import uuid

from sebs.azure.cli import AzureCLI
from sebs.config import SeBSConfig


class AzureControl:
    def __init__(self, subscription, cli=None):
        self.subscription = subscription
        self.cli = cli
        self.owned = cli is None
        self.secrets = [os.environ.get('AZURE_SECRET_PASSWORD', '')]

    def __enter__(self):
        if self.owned:
            missing=[name for name in ['AZURE_SECRET_APPLICATION_ID','AZURE_SECRET_TENANT','AZURE_SECRET_PASSWORD']
                     if not os.environ.get(name)]
            if missing:raise RuntimeError('Missing Azure credential environment variables: '+', '.join(missing))
            import docker
            self.cli = AzureCLI(SeBSConfig(), docker.from_env())
            try:
                self.cli.login(os.environ['AZURE_SECRET_APPLICATION_ID'], os.environ['AZURE_SECRET_TENANT'],
                               os.environ['AZURE_SECRET_PASSWORD'])
                self.run(['account','set','--subscription',self.subscription], json_output=False)
            except BaseException:
                self.cli.shutdown()
                raise
        try:
            account = self.run(['account','show'])
            if account['id'].lower() != self.subscription.lower():
                raise ValueError('Azure CLI subscription differs from the SQL storage attachment')
        except BaseException:
            if self.owned:
                self.cli.shutdown()
            raise
        self.tenant = account['tenantId']
        return self

    def __exit__(self, *args):
        if self.owned and self.cli is not None:
            self.cli.shutdown()

    def redact(self, value):
        for secret in self.secrets:
            if secret:
                value = value.replace(secret,'[REDACTED]')
        return value

    def run(self, args, json_output=True, missing_ok=False):
        code,(stdout,stderr) = self.cli.docker_instance.exec_run(
            ['az',*args,'--only-show-errors','--output','json' if json_output else 'none'],
            user='docker_user',demux=True)
        if code:
            error = self.redact((stderr or b'').decode(errors='replace'))
            if missing_ok and any(t in error for t in ['ResourceNotFound','ResourceGroupNotFound',
                                                       'ParentResourceNotFound','(NotFound)','"code": "NotFound"']):
                return None
            # Never print argv or stdout: either may contain secret values.
            raise RuntimeError('Azure control operation failed: '+error)
        return json.loads(stdout) if json_output and stdout and stdout.strip() else None

    def put_text(self, text):
        directory = '/tmp/sebs-native-control'
        name = uuid.uuid4().hex+'.json'
        code,_ = self.cli.docker_instance.exec_run(['mkdir','-p',directory],user='docker_user')
        if code:
            raise RuntimeError('Cannot create Azure control directory')
        data=text.encode();stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            entry=tarfile.TarInfo(name);entry.size=len(data);entry.mode=0o600
            entry.uid,entry.gid=os.getuid(),os.getgid()
            archive.addfile(entry,io.BytesIO(data))
        if not self.cli.docker_instance.put_archive(directory,stream.getvalue()):
            raise RuntimeError('Cannot transfer Azure control document')
        return directory+'/'+name

    def document(self, args, value):
        path=self.put_text(json.dumps(value))
        try:
            return self.run([*args,'--body','@'+path])
        finally:
            self.cli.docker_instance.exec_run(['rm','-f',path],user='docker_user')

    def deploy(self, group, name, template):
        path=self.put_text(json.dumps(template))
        try:
            return self.run(['deployment','group','create','--resource-group',group,'--name',name,
                             '--template-file',path,'--mode','Incremental'])
        finally:
            self.cli.docker_instance.exec_run(['rm','-f',path],user='docker_user')
