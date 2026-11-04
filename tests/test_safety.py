"""Prove that unapproved calls cannot reach boto3, regardless of verb."""

import ast
import inspect
from unittest.mock import Mock

import botocore.session
import pytest
from botocore import xform_name

from aws_readonly_mcp import server


@pytest.mark.parametrize(
    "service,operation",
    [
        ("ec2", "run_instances"),
        ("ec2", "terminate_instances"),
        ("s3", "delete_bucket"),
        ("s3", "put_object"),
        ("ecs", "execute_command"),
        ("ecs", "update_service"),
        ("ecs", "stop_task"),
        ("ecs", "run_task"),
        ("logs", "start_query"),
        ("logs", "put_log_events"),
        ("logs", "delete_log_group"),
        ("iam", "create_user"),
        ("sts", "get_session_token"),
        ("sts", "get_federation_token"),
        ("secretsmanager", "get_secret_value"),
        ("s3", "list_unknown"),
        ("ec2", "list_buckets"),
        ("s3", "ListBuckets"),
        ("unknown", "describe_instances"),
        ("s3", "__getattribute__"),
    ],
)
def test_unapproved_calls_never_create_a_client(monkeypatch, service, operation):
    client = Mock(side_effect=AssertionError("AWS must not be contacted"))
    monkeypatch.setattr(server.boto3, "client", client)
    with pytest.raises(ValueError, match="read-only"):
        server._aws_call(service, operation)
    client.assert_not_called()


@pytest.mark.parametrize("service", server.READONLY_OPERATIONS)
def test_every_unlisted_sdk_operation_is_denied(monkeypatch, service):
    client = Mock(side_effect=AssertionError("AWS must not be contacted"))
    monkeypatch.setattr(server.boto3, "client", client)
    model = botocore.session.get_session().get_service_model(service)
    methods = {xform_name(operation) for operation in model.operation_names}
    assert server.READONLY_OPERATIONS[service] <= methods
    for operation in methods - server.READONLY_OPERATIONS[service]:
        with pytest.raises(ValueError, match="read-only"):
            server._aws_call(service, operation)
    client.assert_not_called()


def test_boto3_client_creation_has_one_boundary():
    tree = ast.parse(inspect.getsource(server))
    owners = []
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "boto3"
                ):
                    owners.append((function.name, node.func.attr))
    assert owners == [("_aws_call", "client")]
