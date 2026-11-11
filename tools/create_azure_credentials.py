#!/usr/bin/env python3
# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Create a subscription-scoped SeBS Azure principal through the management image."""
import argparse
import codecs
import json
import os
from pathlib import Path
import shlex
import sys
import time

import docker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sebs.config import SeBSConfig


def login(container, tenant=None):
    """Drain the complete interactive stream, then inspect the real exit status.

    Azure CLI may emit device instructions, polling warnings, and arbitrary-size
    chunks. None of those chunks is a reliable JSON login response.
    """
    command = ['az', 'login', '--use-device-code', '--output', 'none']
    if tenant:
        command += ['--tenant', tenant]
    api = container.client.api
    execution = api.exec_create(container.id, command, user='docker_user', stdout=True, stderr=True)
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    for chunk in api.exec_start(execution['Id'], stream=True):
        print(decoder.decode(chunk), end='', flush=True)
    print(decoder.decode(b'', final=True), end='', flush=True)
    for _ in range(20):
        status = api.exec_inspect(execution['Id'])
        if not status['Running'] and status['ExitCode'] is not None:
            if status['ExitCode']:
                raise RuntimeError('Azure login failed; no principal was created')
            return
        time.sleep(.1)
    raise RuntimeError('Could not confirm Azure login completion; no principal was created')


def az(container, command, expect_json=False):
    status, output = container.exec_run(['az', *command, '--only-show-errors'],
                                       user='docker_user', demux=True)
    stdout, stderr = output
    stdout, stderr = (stdout or b'').decode(), (stderr or b'').decode()
    if status:
        # Do not dump stdout: principal creation can return credential material.
        raise RuntimeError(f"Azure command {command[0]} failed (exit {status}): {stderr.strip()}")
    if stderr:
        print(stderr, end='' if stderr.endswith('\n') else '\n', file=sys.stderr)
    if expect_json:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError('Azure CLI returned an invalid JSON response; output withheld') from None
    return None


def choose_subscription(accounts, requested=None):
    accounts = [account for account in accounts if account.get('state', 'Enabled') == 'Enabled']
    if requested:
        selected = [account for account in accounts if account['id'].lower() == requested.lower()]
        if len(selected) != 1:
            raise ValueError('Requested subscription is not available to this login; no principal was created')
        return selected[0]
    selected = [account for account in accounts if account.get('isDefault')]
    if len(selected) != 1:
        raise ValueError('No unique default subscription; pass --subscription explicitly')
    return selected[0]


def create_principal(container, principal_name, subscription, tenant=None):
    if not principal_name.strip():
        raise ValueError('Principal name must not be empty')
    print('Follow the device-login instructions. Waiting for login to finish...', flush=True)
    login(container, tenant)
    accounts = az(container, ['account', 'list', '--output', 'json'], expect_json=True)
    selected = choose_subscription(accounts, subscription)
    subscription_id = selected['id']
    az(container, ['account', 'set', '--subscription', subscription_id])
    print('Selected subscription:', subscription_id, flush=True)
    credentials = az(container, ['ad', 'sp', 'create-for-rbac', '--name', principal_name,
        '--role', 'Contributor', '--scopes', f'/subscriptions/{subscription_id}', '--output', 'json'],
        expect_json=True)
    required = ['appId', 'tenant', 'password']
    if not all(credentials.get(key) for key in required):
        raise RuntimeError('Principal creation did not return all required credential fields; output withheld')
    print('Created service principal:', credentials.get('displayName', principal_name))
    print('Keep the following credentials private; do not paste them into chat.')
    for name, key in [('AZURE_SECRET_APPLICATION_ID', 'appId'), ('AZURE_SECRET_TENANT', 'tenant'),
                      ('AZURE_SECRET_PASSWORD', 'password')]:
        print(f'export {name}={shlex.quote(credentials[key])}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscription')
    parser.add_argument('--tenant')
    args = parser.parse_args()
    client = docker.from_env()
    image = SeBSConfig().docker_repository() + ':manage.azure'
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        print('Docker pull of image', image, flush=True)
        client.images.pull(image)
    container = client.containers.run(image=image, command='/bin/bash', environment={
        'CONTAINER_UID': str(os.getuid()), 'CONTAINER_GID': str(os.getgid()),
        'CONTAINER_USER': 'docker_user'}, remove=True, stdout=True, stderr=True, detach=True, tty=True)
    try:
        print('Please provide the intended principal name', flush=True)
        principal_name = input().strip()
        create_principal(container, principal_name, args.subscription, args.tenant)
    finally:
        container.stop()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
