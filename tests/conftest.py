"""Offline AWS clients: explicit dummy credentials and stubbed requests only."""

from contextlib import ExitStack

import boto3
import pytest
from botocore.stub import Stubber

from aws_readonly_mcp import server


@pytest.fixture
def aws_stub(monkeypatch):
    clients = {}
    with ExitStack() as stack:

        def stub(service, region="us-east-1"):
            client = boto3.Session().client(
                service,
                region_name=region,
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
                aws_session_token="testing",
            )
            clients[(service, region)] = client
            stubber = stack.enter_context(Stubber(client))
            return stubber

        def client_factory(service, *, region_name=None, config=None):
            assert config is server.AWS_CONFIG
            return clients[(service, region_name or "us-east-1")]

        monkeypatch.setattr(server.boto3, "client", client_factory)
        yield stub
        for client in clients.values():
            client.close()
