"""Normalize the existing SeBS Azure HTTP trigger for the shared comparison runner."""
import copy

from evaluation.sql.service import manifest_fingerprint
from sebs.azure.triggers import HTTPTrigger


class AzureFunctionsClient:
    backend = 'azure-functions'
    cold_start_semantics = 'SeBS execution-environment marker; retain observed cold and warm requests'

    def __init__(self, state, input_config):
        self.state = state
        self.input = copy.deepcopy(input_config)
        self.endpoint = state['function_endpoint']
        self.trigger = HTTPTrigger(self.endpoint)

    def health(self):
        # Recorded deployment metadata; the measured invocation validates output.
        return {'provider': 'azure', 'function': self.state['function_name'],
                'region': self.state['region'], 'code_hash': self.state['function_code_hash'],
                'input_fingerprint': self.state['input_fingerprint'],
                'source': 'recorded SeBS deployment; not a live control-plane query'}

    def verify(self, health, trace):
        if health['input_fingerprint'] != manifest_fingerprint(trace['input']):
            raise ValueError('Function comparison state and trace input differ')
        if trace['input'].get('access_mode') != 'azure':
            raise ValueError('Azure functions comparison requires native Blob access')

    def invoke(self, query):
        payload = copy.deepcopy(self.input)
        payload['queries'] = [query]
        result = self.trigger.sync_invoke(payload)
        body = result.output
        result.output = {
            'result': body['result']['result'], 'measurement': body['result']['measurement'],
            'request_id': result.request_id, 'query': query,
            'provider_metrics': {'handler_execution_us': result.times.benchmark,
                                 'billed_gb_seconds': None},
            'execution_environment': {'container_id': body.get('container_id'),
                                      'is_cold_worker': body.get('is_cold_worker')},
        }
        # Azure Consumption bills observed-memory meters; do not use the memory
        # label or peak RSS as an AWS-style GB-second allocation.
        return result
