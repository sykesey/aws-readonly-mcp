"""Validate tool requests with real botocore models and offline Stubber responses."""

import asyncio
import base64
import inspect
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import NoCredentialsError, ReadTimeoutError
from botocore.response import StreamingBody

from aws_readonly_mcp import server

# Each tool gets a real botocore request/response validation, including optionals.
CASES = [
    (
        "list_ec2_instances",
        "ec2",
        "describe_instances",
        {"limit": 12, "next_token": "page2"},
        {"MaxResults": 12, "NextToken": "page2"},
    ),
    (
        "list_s3_buckets",
        "s3",
        "list_buckets",
        {"limit": 12, "continuation_token": "page2"},
        {"MaxBuckets": 12, "ContinuationToken": "page2"},
    ),
    (
        "get_s3_bucket_location",
        "s3",
        "get_bucket_location",
        {"bucket": "example-bucket"},
        {"Bucket": "example-bucket"},
    ),
    (
        "get_s3_bucket_versioning",
        "s3",
        "get_bucket_versioning",
        {"bucket": "example-bucket"},
        {"Bucket": "example-bucket"},
    ),
    (
        "get_s3_bucket_encryption",
        "s3",
        "get_bucket_encryption",
        {"bucket": "example-bucket"},
        {"Bucket": "example-bucket"},
    ),
    (
        "get_s3_public_access_block",
        "s3",
        "get_public_access_block",
        {"bucket": "example-bucket"},
        {"Bucket": "example-bucket"},
    ),
    (
        "list_s3_objects",
        "s3",
        "list_objects_v2",
        {
            "bucket": "example-bucket",
            "prefix": "logs/",
            "delimiter": "/",
            "limit": 12,
            "continuation_token": "page2",
        },
        {
            "Bucket": "example-bucket",
            "Prefix": "logs/",
            "Delimiter": "/",
            "MaxKeys": 12,
            "ContinuationToken": "page2",
        },
    ),
    (
        "head_s3_object",
        "s3",
        "head_object",
        {"bucket": "example-bucket", "key": "file.txt", "version_id": "v1"},
        {"Bucket": "example-bucket", "Key": "file.txt", "VersionId": "v1"},
    ),
    (
        "get_s3_object",
        "s3",
        "get_object",
        {"bucket": "example-bucket", "key": "file.txt", "version_id": "v1"},
        {"Bucket": "example-bucket", "Key": "file.txt", "VersionId": "v1"},
    ),
    (
        "list_ecs_clusters",
        "ecs",
        "list_clusters",
        {"limit": 12, "next_token": "page2"},
        {"maxResults": 12, "nextToken": "page2"},
    ),
    (
        "describe_ecs_clusters",
        "ecs",
        "describe_clusters",
        {"clusters": ["demo"]},
        {
            "clusters": ["demo"],
            "include": [
                "ATTACHMENTS",
                "CONFIGURATIONS",
                "SETTINGS",
                "STATISTICS",
                "TAGS",
            ],
        },
    ),
    (
        "list_ecs_services",
        "ecs",
        "list_services",
        {"cluster": "demo", "limit": 12, "next_token": "page2"},
        {"cluster": "demo", "maxResults": 12, "nextToken": "page2"},
    ),
    (
        "describe_ecs_services",
        "ecs",
        "describe_services",
        {"cluster": "demo", "services": ["app"]},
        {"cluster": "demo", "services": ["app"], "include": ["TAGS"]},
    ),
    (
        "list_ecs_tasks",
        "ecs",
        "list_tasks",
        {
            "cluster": "demo",
            "service_name": "app",
            "family": "app",
            "desired_status": "STOPPED",
            "limit": 12,
            "next_token": "page2",
        },
        {
            "cluster": "demo",
            "serviceName": "app",
            "family": "app",
            "desiredStatus": "STOPPED",
            "maxResults": 12,
            "nextToken": "page2",
        },
    ),
    (
        "describe_ecs_tasks",
        "ecs",
        "describe_tasks",
        {"cluster": "demo", "tasks": ["task-id"]},
        {"cluster": "demo", "tasks": ["task-id"], "include": ["TAGS"]},
    ),
    (
        "list_ecs_task_definitions",
        "ecs",
        "list_task_definitions",
        {
            "family_prefix": "app",
            "status": "INACTIVE",
            "limit": 12,
            "next_token": "page2",
        },
        {
            "familyPrefix": "app",
            "status": "INACTIVE",
            "sort": "DESC",
            "maxResults": 12,
            "nextToken": "page2",
        },
    ),
    (
        "describe_ecs_task_definition",
        "ecs",
        "describe_task_definition",
        {"task_definition": "app:3"},
        {"taskDefinition": "app:3", "include": ["TAGS"]},
    ),
    (
        "list_ecs_container_instances",
        "ecs",
        "list_container_instances",
        {"cluster": "demo", "status": "DRAINING", "limit": 12, "next_token": "page2"},
        {
            "cluster": "demo",
            "status": "DRAINING",
            "maxResults": 12,
            "nextToken": "page2",
        },
    ),
    (
        "describe_ecs_container_instances",
        "ecs",
        "describe_container_instances",
        {"cluster": "demo", "container_instances": ["instance-id"]},
        {
            "cluster": "demo",
            "containerInstances": ["instance-id"],
            "include": ["TAGS", "CONTAINER_INSTANCE_HEALTH"],
        },
    ),
    (
        "list_cloudwatch_log_groups",
        "logs",
        "describe_log_groups",
        {"name_prefix": "/ecs/", "limit": 12, "next_token": "page2"},
        {"logGroupNamePrefix": "/ecs/", "limit": 12, "nextToken": "page2"},
    ),
    (
        "list_cloudwatch_log_streams",
        "logs",
        "describe_log_streams",
        {
            "log_group": "/ecs/app",
            "name_prefix": "ecs/",
            "limit": 12,
            "next_token": "page2",
        },
        {
            "logGroupName": "/ecs/app",
            "logStreamNamePrefix": "ecs/",
            "limit": 12,
            "nextToken": "page2",
            "orderBy": "LogStreamName",
            "descending": True,
        },
    ),
    (
        "get_cloudwatch_log_events",
        "logs",
        "get_log_events",
        {
            "log_group": "/ecs/app",
            "log_stream": "ecs/app/123",
            "start_time": 0,
            "end_time": 1000,
            "start_from_head": True,
            "limit": 12,
            "next_token": "page2",
        },
        {
            "logGroupName": "/ecs/app",
            "logStreamName": "ecs/app/123",
            "startTime": 0,
            "endTime": 1000,
            "startFromHead": True,
            "limit": 12,
            "nextToken": "page2",
        },
    ),
    (
        "filter_cloudwatch_log_events",
        "logs",
        "filter_log_events",
        {
            "log_group": "/ecs/app",
            "filter_pattern": "ERROR",
            "log_stream_prefix": "ecs/",
            "start_time": 0,
            "end_time": 1000,
            "limit": 12,
            "next_token": "page2",
        },
        {
            "logGroupName": "/ecs/app",
            "filterPattern": "ERROR",
            "logStreamNamePrefix": "ecs/",
            "startTime": 0,
            "endTime": 1000,
            "limit": 12,
            "nextToken": "page2",
        },
    ),
    (
        "describe_iam_user",
        "iam",
        "get_user",
        {"user_name": "alice"},
        {"UserName": "alice"},
    ),
]


@pytest.mark.parametrize("name,service,operation,kwargs,expected", CASES)
def test_tool_requests(aws_stub, name, service, operation, kwargs, expected):
    function = getattr(server, name)
    region = (
        "ap-southeast-2"
        if "region" in inspect.signature(function).parameters
        else "us-east-1"
    )
    if "region" in inspect.signature(function).parameters:
        kwargs = {**kwargs, "region": region}
    stubber = aws_stub(service, region)
    response = {"ResponseMetadata": {"RequestId": "omit-me"}}
    if operation == "get_object":
        response.update(Body=StreamingBody(io.BytesIO(b""), 0), ContentLength=0)
    if operation == "get_user":
        response["User"] = {
            "Path": "/",
            "UserName": "alice",
            "UserId": "AIDATESTING1234567890",
            "Arn": "arn:aws:iam::123456789012:user/alice",
            "CreateDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    stubber.add_response(operation, response, expected)
    result = json.loads(function(**kwargs))
    assert "ResponseMetadata" not in result
    stubber.assert_no_pending_responses()


def test_cost_dates_and_pagination(aws_stub):
    end = datetime.now(timezone.utc).date()
    stubber = aws_stub("ce")
    stubber.add_response(
        "get_cost_and_usage",
        {"ResultsByTime": [], "NextPageToken": "page3"},
        {
            "TimePeriod": {
                "Start": (end - timedelta(days=30)).isoformat(),
                "End": end.isoformat(),
            },
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            "NextPageToken": "page2",
        },
    )
    assert json.loads(server.get_cost_last_30_days("page2"))["NextPageToken"] == "page3"
    stubber.assert_no_pending_responses()


def test_every_mcp_tool_has_request_coverage():
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == {case[0] for case in CASES} | {
        "get_cost_last_30_days"
    }
    assert len(tools) == 25


def test_ec2_summary_preserves_token_and_tags(aws_stub):
    stubber = aws_stub("ec2")
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-123",
                            "State": {"Name": "running"},
                            "InstanceType": "t3.micro",
                            "Placement": {"AvailabilityZone": "us-east-1a"},
                            "Tags": [{"Key": "Name", "Value": "app"}],
                        }
                    ]
                }
            ],
            "NextToken": "page2",
        },
        {"MaxResults": 100},
    )
    result = json.loads(server.list_ec2_instances())
    assert result["NextToken"] == "page2"
    assert result["Instances"][0]["Tags"] == [{"Key": "Name", "Value": "app"}]


def test_ecs_partial_failures_are_preserved(aws_stub):
    stubber = aws_stub("ecs")
    response = {
        "services": [{"serviceName": "app", "desiredCount": 2, "runningCount": 1}],
        "failures": [{"arn": "missing", "reason": "MISSING"}],
    }
    stubber.add_response(
        "describe_services",
        response,
        {
            "cluster": "demo",
            "services": ["app", "missing"],
            "include": ["TAGS"],
        },
    )
    assert (
        json.loads(server.describe_ecs_services("demo", ["app", "missing"])) == response
    )


@pytest.mark.parametrize(
    "tool,response,expected",
    [
        (
            server.list_ecs_clusters,
            {"clusterArns": [], "nextToken": "more"},
            {"maxResults": 100},
        ),
        (
            server.list_s3_buckets,
            {"Buckets": [], "ContinuationToken": "more"},
            {"MaxBuckets": 100},
        ),
        (
            server.list_cloudwatch_log_groups,
            {"logGroups": [], "nextToken": "more"},
            {"limit": 50},
        ),
    ],
)
def test_empty_pages_preserve_continuation(aws_stub, tool, response, expected):
    case = next(case for case in CASES if case[0] == tool.__name__)
    stubber = aws_stub(case[1])
    stubber.add_response(case[2], response, expected)
    assert json.loads(tool()) == response


def test_s3_folder_page_preserves_continuation(aws_stub):
    stubber = aws_stub("s3")
    response = {
        "Contents": [],
        "CommonPrefixes": [{"Prefix": "logs/"}],
        "IsTruncated": True,
        "NextContinuationToken": "more",
    }
    stubber.add_response(
        "list_objects_v2",
        response,
        {
            "Bucket": "example-bucket",
            "Prefix": "",
            "Delimiter": "/",
            "MaxKeys": 100,
        },
    )
    assert (
        json.loads(server.list_s3_objects("example-bucket", delimiter="/")) == response
    )


def test_log_search_empty_pages_still_have_more(aws_stub):
    stubber = aws_stub("logs")
    params = {"logGroupName": "/ecs/app", "filterPattern": "ERROR", "limit": 100}
    stubber.add_response(
        "filter_log_events", {"events": [], "nextToken": "more"}, params
    )
    stubber.add_response(
        "filter_log_events",
        {"events": [{"message": "ERROR late match"}]},
        {**params, "nextToken": "more"},
    )
    first = json.loads(server.filter_cloudwatch_log_events("/ecs/app", "ERROR"))
    second = json.loads(
        server.filter_cloudwatch_log_events(
            "/ecs/app", "ERROR", next_token=first["nextToken"]
        )
    )
    assert second["events"][0]["message"] == "ERROR late match"
    assert "nextToken" not in second
    stubber.assert_no_pending_responses()


def test_log_forward_end_token_is_preserved(aws_stub):
    stubber = aws_stub("logs")
    response = {
        "events": [],
        "nextForwardToken": "end",
        "nextBackwardToken": "previous",
    }
    stubber.add_response(
        "get_log_events",
        response,
        {
            "logGroupName": "/ecs/app",
            "logStreamName": "ecs/app/123",
            "startFromHead": True,
            "limit": 100,
            "nextToken": "end",
        },
    )
    assert (
        json.loads(
            server.get_cloudwatch_log_events(
                "/ecs/app", "ecs/app/123", start_from_head=True, next_token="end"
            )
        )
        == response
    )


def test_log_streams_default_to_recent_activity(aws_stub):
    stubber = aws_stub("logs")
    stubber.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {
            "logGroupName": "/ecs/app",
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": 50,
        },
    )
    assert json.loads(server.list_cloudwatch_log_streams("/ecs/app")) == {
        "logStreams": []
    }


@pytest.mark.parametrize(
    "data,max_bytes,encoding,truncated",
    [
        (b"hello", 10, "utf-8", False),
        (b"hello", 5, "utf-8", False),
        (b"hello world", 5, "utf-8", True),
        (b"", 10, "utf-8", False),
        (b"\xff\x00\xfe", 10, "base64", False),
        ("a\u00e9".encode(), 2, "base64", True),
    ],
)
def test_s3_content_is_bounded_and_stream_closed(
    aws_stub, data, max_bytes, encoding, truncated
):
    stubber = aws_stub("s3")
    raw = io.BytesIO(data)
    body = StreamingBody(raw, len(data))
    stubber.add_response(
        "get_object",
        {"Body": body, "ContentLength": len(data)},
        {
            "Bucket": "example-bucket",
            "Key": "file.txt",
        },
    )
    result = json.loads(
        server.get_s3_object("example-bucket", "file.txt", max_bytes=max_bytes)
    )
    assert result["Encoding"] == encoding
    content = (
        result["Content"].encode()
        if encoding == "utf-8"
        else base64.b64decode(result["Content"])
    )
    assert content == data[:max_bytes]
    assert result["BytesRead"] == min(len(data), max_bytes)
    assert result["Truncated"] is truncated
    assert raw.closed
    assert "Body" not in result


def test_s3_stream_closed_on_read_failure(aws_stub):
    stubber = aws_stub("s3")
    body = Mock()
    body.read.side_effect = ReadTimeoutError(endpoint_url="https://s3.amazonaws.com")
    stubber.add_response(
        "get_object", {"Body": body}, {"Bucket": "example-bucket", "Key": "file.txt"}
    )
    assert "AWS error calling s3.get_object" in server.get_s3_object(
        "example-bucket", "file.txt"
    )
    body.close.assert_called_once()
    body.read.assert_called_once_with(65537)


def test_access_denied_is_an_error_not_an_empty_result(aws_stub):
    stubber = aws_stub("s3")
    stubber.add_client_error(
        "list_buckets",
        service_error_code="AccessDenied",
        service_message="Denied",
        expected_params={"MaxBuckets": 100},
    )
    result = server.list_s3_buckets()
    assert "AWS error calling s3.list_buckets" in result and "AccessDenied" in result


def test_missing_credentials_are_reported(monkeypatch):
    monkeypatch.setattr(server.boto3, "client", Mock(side_effect=NoCredentialsError()))
    assert "Unable to locate credentials" in server.list_ecs_clusters()


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (server.list_ecs_clusters, {"limit": 0}),
        (server.list_ec2_instances, {"limit": 4}),
        (server.list_ecs_services, {"cluster": "demo", "limit": 101}),
        (server.list_cloudwatch_log_groups, {"limit": 51}),
        (server.list_s3_objects, {"bucket": "example-bucket", "limit": 1001}),
        (
            server.get_s3_object,
            {"bucket": "example-bucket", "key": "file", "max_bytes": 262145},
        ),
        (
            server.get_s3_object,
            {"bucket": "example-bucket", "key": "file", "max_bytes": 0},
        ),
        (server.describe_ecs_clusters, {"clusters": []}),
        (server.describe_ecs_services, {"cluster": "demo", "services": ["app"] * 11}),
        (server.describe_ecs_tasks, {"cluster": "demo", "tasks": [" "]}),
        (
            server.get_cloudwatch_log_events,
            {"log_group": "g", "log_stream": "s", "start_time": -1},
        ),
        (
            server.filter_cloudwatch_log_events,
            {"log_group": "g", "start_time": 20, "end_time": 10},
        ),
    ],
)
def test_invalid_input_rejected_before_aws(monkeypatch, tool, kwargs):
    client = Mock(side_effect=AssertionError("AWS must not be contacted"))
    monkeypatch.setattr(server.boto3, "client", client)
    with pytest.raises(ValueError):
        tool(**kwargs)
    client.assert_not_called()
