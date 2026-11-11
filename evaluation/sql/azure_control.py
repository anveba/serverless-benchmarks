"""Use SeBS's Azure CLI image for comparison control-plane operations."""
import io
import json
import os
import tarfile

import docker

from sebs.azure.cli import AzureCLI
from sebs.config import SeBSConfig


class AzureControl:
    def __init__(self, subscription):
        self.secrets = [os.environ.get('AZURE_SECRET_PASSWORD', '')]
        self.cli = AzureCLI(SeBSConfig(), docker.from_env())
        try:
            self.run(['login', '--service-principal', '--username', os.environ['AZURE_SECRET_APPLICATION_ID'],
                '--tenant', os.environ['AZURE_SECRET_TENANT'],
                '--password=' + os.environ['AZURE_SECRET_PASSWORD']], json_output=False)
            self.run(['account', 'set', '--subscription', subscription], json_output=False)
        except BaseException:
            self.close()
            raise

    def redact(self, text):
        for value in self.secrets:
            if value:
                text = text.replace(value, '[REDACTED]')
        return text

    def run(self, args, json_output=True, missing_ok=False):
        status, (stdout, stderr) = self.cli.docker_instance.exec_run(
            ['az', *args, '--only-show-errors', '--output', 'json' if json_output else 'none'],
            user='docker_user', demux=True)
        if status:
            error = self.redact((stderr or b'').decode(errors='replace'))
            if missing_ok and any(code in error for code in ['ResourceNotFound', 'ResourceGroupNotFound',
                                                              'ParentResourceNotFound', 'NotFound']):
                return None
            # Never echo argv/stdout: commands may carry SQL or storage credentials.
            raise RuntimeError('Azure command failed: ' + error)
        return json.loads(stdout) if json_output and stdout and stdout.strip() else None

    def put_text(self, name, text):
        directory = '/tmp/sebs-azure-comparison'
        self.cli.docker_instance.exec_run(['mkdir', '-p', directory], user='docker_user')
        data = text.encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            info.uid, info.gid = os.getuid(), os.getgid()
            archive.addfile(info, io.BytesIO(data))
        if not self.cli.docker_instance.put_archive(directory, stream.getvalue()):
            raise RuntimeError('Could not copy control script into CLI container')
        return directory + '/' + name

    def close(self):
        self.cli.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
