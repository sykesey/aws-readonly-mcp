"""Read-only AWS inspection over MCP, with an explicit API allow-list.

Every AWS request passes through `_aws_call`. List and log tools return one
page at a time, preserving continuation tokens for caller-controlled browsing.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("aws-readonly")

# Prefixes alone are unsafe: GetSessionToken issues credentials, for example.
# Add a new operation only after reviewing its read-only semantics.
READONLY_OPERATIONS = {
    "ec2": frozenset({"describe_instances"}),
    "ce": frozenset({"get_cost_and_usage"}),
    "iam": frozenset({"get_user"}),
    "ecs": frozenset(
        {
            "list_clusters",
            "describe_clusters",
            "list_services",
            "describe_services",
            "list_tasks",
            "describe_tasks",
            "list_task_definitions",
            "describe_task_definition",
            "list_container_instances",
            "describe_container_instances",
        }
    ),
    "lambda": frozenset(
        {
            "list_functions",
            "get_function_configuration",
            "list_event_source_mappings",
        }
    ),
    "cloudformation": frozenset(
        {
            "list_stacks",
            "describe_stacks",
            "list_stack_resources",
            "describe_stack_events",
            "get_template",
            "list_exports",
        }
    ),
    "rds": frozenset({"describe_db_instances", "describe_db_clusters"}),
    "sqs": frozenset(
        {"list_queues", "get_queue_attributes", "list_dead_letter_source_queues"}
    ),
    "logs": frozenset(
        {
            "describe_log_groups",
            "describe_log_streams",
            "get_log_events",
            "filter_log_events",
        }
    ),
    "s3": frozenset(
        {
            "list_buckets",
            "get_bucket_location",
            "get_bucket_versioning",
            "get_bucket_encryption",
            "get_public_access_block",
            "list_objects_v2",
            "head_object",
            "get_object",
        }
    ),
}
AWS_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"mode": "standard", "total_max_attempts": 3},
)
MAX_OBJECT_BYTES = 256 * 1024


def _aws_call(
    service: str,
    operation: str,
    *,
    region: str | None = None,
    transform: Callable[[dict[str, Any]], Any] | None = None,
    **kwargs: Any,
) -> str:
    """The sole AWS invocation boundary; deny unknown pairs before client creation."""
    if operation not in READONLY_OPERATIONS.get(service, ()):
        raise ValueError(
            f"Refusing to run '{service}.{operation}'. This server is read-only; "
            "the operation is not explicitly allowed."
        )
    try:
        with closing(
            boto3.client(service, region_name=region, config=AWS_CONFIG)
        ) as client:
            result = getattr(client, operation)(**kwargs)
            result.pop("ResponseMetadata", None)
            if transform is not None:
                result = transform(result)
        return json.dumps(result, indent=2, default=str)
    except (BotoCoreError, ClientError) as exc:
        return f"AWS error calling {service}.{operation}: {exc}"


def _limit(value: int, maximum: int, name: str = "limit", minimum: int = 1) -> int:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _identifiers(values: list[str], maximum: int, name: str) -> list[str]:
    _limit(len(values), maximum, f"Number of {name}")
    if any(not value.strip() for value in values):
        raise ValueError(f"{name} must not contain empty identifiers.")
    return values


def _optional(**kwargs: Any) -> dict[str, Any]:
    """Omit unset parameters while preserving zero and False."""
    return {key: value for key, value in kwargs.items() if value is not None}


def _time_range(start_time: int | None, end_time: int | None) -> dict[str, int]:
    for value in (start_time, end_time):
        if value is not None and value < 0:
            raise ValueError("Log timestamps must be non-negative Unix milliseconds.")
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time.")
    return _optional(startTime=start_time, endTime=end_time)


def _ec2_summary(response: dict[str, Any]) -> dict[str, Any]:
    result = {
        "Instances": [
            {
                "InstanceId": instance.get("InstanceId"),
                "State": instance.get("State", {}).get("Name"),
                "Type": instance.get("InstanceType"),
                "AZ": instance.get("Placement", {}).get("AvailabilityZone"),
                "Tags": instance.get("Tags", []),
            }
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]
    }
    if "NextToken" in response:
        result["NextToken"] = response["NextToken"]
    return result


@mcp.tool()
def list_ec2_instances(
    region: str = "us-east-1",
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List one page of EC2 summaries; pass NextToken as next_token to continue."""
    return _aws_call(
        "ec2",
        "describe_instances",
        region=region,
        transform=_ec2_summary,
        MaxResults=_limit(limit, 1000, minimum=5),
        **_optional(NextToken=next_token),
    )


@mcp.tool()
def list_s3_buckets(limit: int = 100, continuation_token: str | None = None) -> str:
    """List one page of general purpose S3 buckets. Continue with ContinuationToken."""
    return _aws_call(
        "s3",
        "list_buckets",
        MaxBuckets=_limit(limit, 1000),
        **_optional(ContinuationToken=continuation_token),
    )


@mcp.tool()
def get_s3_bucket_location(bucket: str) -> str:
    """Inspect a bucket's region. Null LocationConstraint means us-east-1; EU means eu-west-1."""
    return _aws_call("s3", "get_bucket_location", Bucket=bucket)


@mcp.tool()
def get_s3_bucket_versioning(bucket: str, region: str = "us-east-1") -> str:
    """Inspect versioning/MFA delete settings. Absent Status means versioning was never enabled."""
    return _aws_call("s3", "get_bucket_versioning", region=region, Bucket=bucket)


@mcp.tool()
def get_s3_bucket_encryption(bucket: str, region: str = "us-east-1") -> str:
    """Inspect a bucket's default server-side encryption configuration."""
    return _aws_call("s3", "get_bucket_encryption", region=region, Bucket=bucket)


@mcp.tool()
def get_s3_public_access_block(bucket: str, region: str = "us-east-1") -> str:
    """Inspect bucket-level Block Public Access; this alone does not determine effective public access."""
    return _aws_call("s3", "get_public_access_block", region=region, Bucket=bucket)


@mcp.tool()
def list_s3_objects(
    bucket: str,
    prefix: str = "",
    delimiter: str | None = None,
    region: str = "us-east-1",
    limit: int = 100,
    continuation_token: str | None = None,
) -> str:
    """List one S3 object page with sizes/dates; delimiter='/' browses folders.

    Pass NextContinuationToken as continuation_token while IsTruncated is true.
    CommonPrefixes contains folder-like prefixes; Contents contains objects.
    """
    return _aws_call(
        "s3",
        "list_objects_v2",
        region=region,
        Bucket=bucket,
        Prefix=prefix,
        MaxKeys=_limit(limit, 1000),
        **_optional(Delimiter=delimiter, ContinuationToken=continuation_token),
    )


@mcp.tool()
def head_s3_object(
    bucket: str,
    key: str,
    region: str = "us-east-1",
    version_id: str | None = None,
) -> str:
    """Inspect size, content type, metadata, ETag, and encryption without reading the body."""
    return _aws_call(
        "s3",
        "head_object",
        region=region,
        Bucket=bucket,
        Key=key,
        **_optional(VersionId=version_id),
    )


def _object_content(response: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    body = response.pop("Body")
    try:
        data = body.read(max_bytes + 1)
    finally:
        body.close()
    truncated = len(data) > max_bytes or response.get("ContentLength", 0) > len(data)
    data = data[:max_bytes]
    try:
        content = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(data).decode("ascii")
        encoding = "base64"
    return {
        **response,
        "Content": content,
        "Encoding": encoding,
        "BytesRead": len(data),
        "Truncated": truncated,
    }


@mcp.tool()
def get_s3_object(
    bucket: str,
    key: str,
    region: str = "us-east-1",
    max_bytes: int = 64 * 1024,
    version_id: str | None = None,
) -> str:
    """Read an S3 object's beginning, up to max_bytes (1..262144; default 65536).

    Returns Content, Encoding (utf-8 or base64), BytesRead, and Truncated plus
    metadata. Binary or split UTF-8 content uses base64. Does not decompress,
    execute, or interpret content. Always closes the response stream.
    """
    _limit(max_bytes, MAX_OBJECT_BYTES, "max_bytes")
    return _aws_call(
        "s3",
        "get_object",
        region=region,
        transform=lambda response: _object_content(response, max_bytes),
        Bucket=bucket,
        Key=key,
        **_optional(VersionId=version_id),
    )


@mcp.tool()
def list_ecs_clusters(
    region: str = "us-east-1",
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List a page of ECS cluster ARNs. Pass nextToken as next_token to continue."""
    return _aws_call(
        "ecs",
        "list_clusters",
        region=region,
        maxResults=_limit(limit, 100),
        **_optional(nextToken=next_token),
    )


@mcp.tool()
def describe_ecs_clusters(clusters: list[str], region: str = "us-east-1") -> str:
    """Inspect 1..100 cluster names/ARNs: counts, capacity, settings, and tags.

    Check failures as well as clusters; AWS can return partial results.
    """
    return _aws_call(
        "ecs",
        "describe_clusters",
        region=region,
        clusters=_identifiers(clusters, 100, "clusters"),
        include=["ATTACHMENTS", "CONFIGURATIONS", "SETTINGS", "STATISTICS", "TAGS"],
    )


@mcp.tool()
def list_ecs_services(
    cluster: str,
    region: str = "us-east-1",
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List a page of service ARNs in an ECS cluster. Continue with nextToken."""
    return _aws_call(
        "ecs",
        "list_services",
        region=region,
        cluster=cluster,
        maxResults=_limit(limit, 100),
        **_optional(nextToken=next_token),
    )


@mcp.tool()
def describe_ecs_services(
    cluster: str,
    services: list[str],
    region: str = "us-east-1",
) -> str:
    """Inspect 1..10 service names/ARNs: counts, deployments, events, networking, tags.

    Check failures for missing or inaccessible services.
    """
    return _aws_call(
        "ecs",
        "describe_services",
        region=region,
        cluster=cluster,
        services=_identifiers(services, 10, "services"),
        include=["TAGS"],
    )


@mcp.tool()
def list_ecs_tasks(
    cluster: str,
    region: str = "us-east-1",
    service_name: str | None = None,
    desired_status: Literal["RUNNING", "STOPPED"] = "RUNNING",
    family: str | None = None,
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List task ARNs by cluster, optionally service/family/desired status.

    Use STOPPED to investigate recent exits. Continue with nextToken.
    """
    return _aws_call(
        "ecs",
        "list_tasks",
        region=region,
        cluster=cluster,
        desiredStatus=desired_status,
        maxResults=_limit(limit, 100),
        **_optional(serviceName=service_name, family=family, nextToken=next_token),
    )


@mcp.tool()
def describe_ecs_tasks(
    cluster: str, tasks: list[str], region: str = "us-east-1"
) -> str:
    """Inspect 1..100 task IDs/ARNs: containers, exit codes, stopped reasons, ENIs, tags.

    Check failures for missing tasks. Task definitions contain log configuration.
    """
    return _aws_call(
        "ecs",
        "describe_tasks",
        region=region,
        cluster=cluster,
        tasks=_identifiers(tasks, 100, "tasks"),
        include=["TAGS"],
    )


@mcp.tool()
def list_ecs_task_definitions(
    region: str = "us-east-1",
    family_prefix: str | None = None,
    status: Literal["ACTIVE", "INACTIVE", "DELETE_IN_PROGRESS"] = "ACTIVE",
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List definition ARNs, newest first, optionally by family prefix. Continue with nextToken."""
    return _aws_call(
        "ecs",
        "list_task_definitions",
        region=region,
        status=status,
        sort="DESC",
        maxResults=_limit(limit, 100),
        **_optional(familyPrefix=family_prefix, nextToken=next_token),
    )


@mcp.tool()
def describe_ecs_task_definition(
    task_definition: str, region: str = "us-east-1"
) -> str:
    """Inspect an ARN or family[:revision]: images, CPU/memory, roles, log configuration.

    Plaintext environment values are included. Secret references are returned
    as configured; their values are never fetched from a secrets service.
    """
    return _aws_call(
        "ecs",
        "describe_task_definition",
        region=region,
        taskDefinition=task_definition,
        include=["TAGS"],
    )


@mcp.tool()
def list_ecs_container_instances(
    cluster: str,
    region: str = "us-east-1",
    status: Literal[
        "ACTIVE", "DRAINING", "REGISTERING", "DEREGISTERING", "REGISTRATION_FAILED"
    ] = "ACTIVE",
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List container instance ARNs for EC2-backed ECS capacity. Continue with nextToken."""
    return _aws_call(
        "ecs",
        "list_container_instances",
        region=region,
        cluster=cluster,
        status=status,
        maxResults=_limit(limit, 100),
        **_optional(nextToken=next_token),
    )


@mcp.tool()
def describe_ecs_container_instances(
    cluster: str,
    container_instances: list[str],
    region: str = "us-east-1",
) -> str:
    """Inspect 1..100 container instance IDs/ARNs: EC2 IDs, resources, agent state, health.

    Check failures as well as containerInstances for partial results.
    """
    return _aws_call(
        "ecs",
        "describe_container_instances",
        region=region,
        cluster=cluster,
        containerInstances=_identifiers(
            container_instances, 100, "container instances"
        ),
        include=["TAGS", "CONTAINER_INSTANCE_HEALTH"],
    )


@mcp.tool()
def list_lambda_functions(
    region: str = "us-east-1",
    limit: int = 50,
    marker: str | None = None,
) -> str:
    """List a page of Lambda function configurations (1..50). Continue with NextMarker as marker."""
    return _aws_call(
        "lambda",
        "list_functions",
        region=region,
        MaxItems=_limit(limit, 50),
        **_optional(Marker=marker),
    )


@mcp.tool()
def get_lambda_function(
    function_name: str, region: str = "us-east-1", qualifier: str | None = None
) -> str:
    """Inspect a function's runtime, role, limits, VPC, layers, and environment.

    Accepts a name or ARN; qualifier is a version or alias. Plaintext environment
    variables are included. Code is not downloaded.
    """
    return _aws_call(
        "lambda",
        "get_function_configuration",
        region=region,
        FunctionName=function_name,
        **_optional(Qualifier=qualifier),
    )


@mcp.tool()
def list_lambda_event_source_mappings(
    region: str = "us-east-1",
    function_name: str | None = None,
    limit: int = 100,
    marker: str | None = None,
) -> str:
    """List SQS/Kinesis/DynamoDB/etc. triggers, optionally for one function (1..100).

    Continue with NextMarker as marker.
    """
    return _aws_call(
        "lambda",
        "list_event_source_mappings",
        region=region,
        MaxItems=_limit(limit, 100),
        **_optional(FunctionName=function_name, Marker=marker),
    )


@mcp.tool()
def describe_rds_instances(
    region: str = "us-east-1",
    db_instance_identifier: str | None = None,
    limit: int = 100,
    marker: str | None = None,
) -> str:
    """Inspect RDS/Aurora DB instances (page size 20..100), optionally one by identifier.

    Continue with Marker as marker. Master passwords are never returned.
    """
    return _aws_call(
        "rds",
        "describe_db_instances",
        region=region,
        MaxRecords=_limit(limit, 100, minimum=20),
        **_optional(DBInstanceIdentifier=db_instance_identifier, Marker=marker),
    )


@mcp.tool()
def describe_rds_clusters(
    region: str = "us-east-1",
    db_cluster_identifier: str | None = None,
    limit: int = 100,
    marker: str | None = None,
) -> str:
    """Inspect Aurora/Multi-AZ DB clusters (page size 20..100), optionally one by identifier.

    Includes members, endpoints, engine, and status. Continue with Marker as marker.
    """
    return _aws_call(
        "rds",
        "describe_db_clusters",
        region=region,
        MaxRecords=_limit(limit, 100, minimum=20),
        **_optional(DBClusterIdentifier=db_cluster_identifier, Marker=marker),
    )


@mcp.tool()
def list_sqs_queues(
    region: str = "us-east-1",
    name_prefix: str | None = None,
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List a page of SQS queue URLs (1..1000), optionally by name prefix. Continue with NextToken."""
    return _aws_call(
        "sqs",
        "list_queues",
        region=region,
        MaxResults=_limit(limit, 1000),
        **_optional(QueueNamePrefix=name_prefix, NextToken=next_token),
    )


@mcp.tool()
def get_sqs_queue_attributes(queue_url: str, region: str = "us-east-1") -> str:
    """Inspect a queue by URL: approximate message counts, timeouts, redrive policy, access policy.

    Message bodies are never read.
    """
    return _aws_call(
        "sqs",
        "get_queue_attributes",
        region=region,
        QueueUrl=queue_url,
        AttributeNames=["All"],
    )


@mcp.tool()
def list_sqs_dead_letter_source_queues(
    queue_url: str,
    region: str = "us-east-1",
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """List queue URLs whose redrive policy targets this dead-letter queue. Continue with NextToken."""
    return _aws_call(
        "sqs",
        "list_dead_letter_source_queues",
        region=region,
        QueueUrl=queue_url,
        MaxResults=_limit(limit, 1000),
        **_optional(NextToken=next_token),
    )


@mcp.tool()
def list_cloudformation_stacks(
    region: str = "us-east-1",
    status_filter: list[str] | None = None,
    next_token: str | None = None,
) -> str:
    """List one page of stack summaries (including deleted stacks for 90 days).

    status_filter takes statuses such as CREATE_COMPLETE or UPDATE_ROLLBACK_COMPLETE.
    Continue with NextToken as next_token.
    """
    return _aws_call(
        "cloudformation",
        "list_stacks",
        region=region,
        **_optional(StackStatusFilter=status_filter, NextToken=next_token),
    )


@mcp.tool()
def describe_cloudformation_stacks(
    region: str = "us-east-1",
    stack_name: str | None = None,
    next_token: str | None = None,
) -> str:
    """Inspect stacks (status, parameters, outputs, tags, drift), optionally one by name/ID.

    NoEcho parameter values are masked by AWS. Continue with NextToken.
    """
    return _aws_call(
        "cloudformation",
        "describe_stacks",
        region=region,
        **_optional(StackName=stack_name, NextToken=next_token),
    )


@mcp.tool()
def list_cloudformation_stack_resources(
    stack_name: str, region: str = "us-east-1", next_token: str | None = None
) -> str:
    """List a stack's resources with logical/physical IDs, types, and status. Continue with NextToken."""
    return _aws_call(
        "cloudformation",
        "list_stack_resources",
        region=region,
        StackName=stack_name,
        **_optional(NextToken=next_token),
    )


@mcp.tool()
def describe_cloudformation_stack_events(
    stack_name: str, region: str = "us-east-1", next_token: str | None = None
) -> str:
    """List a stack's events, newest first, with failure reasons. Continue with NextToken."""
    return _aws_call(
        "cloudformation",
        "describe_stack_events",
        region=region,
        StackName=stack_name,
        **_optional(NextToken=next_token),
    )


@mcp.tool()
def get_cloudformation_template(
    stack_name: str,
    region: str = "us-east-1",
    stage: Literal["Original", "Processed"] = "Original",
) -> str:
    """Read a stack's template as submitted (Original) or after transforms (Processed)."""
    return _aws_call(
        "cloudformation",
        "get_template",
        region=region,
        StackName=stack_name,
        TemplateStage=stage,
    )


@mcp.tool()
def list_cloudformation_exports(
    region: str = "us-east-1", next_token: str | None = None
) -> str:
    """List exported stack output values available for cross-stack imports. Continue with NextToken."""
    return _aws_call(
        "cloudformation",
        "list_exports",
        region=region,
        **_optional(NextToken=next_token),
    )


@mcp.tool()
def list_cloudwatch_log_groups(
    region: str = "us-east-1",
    name_prefix: str | None = None,
    limit: int = 50,
    next_token: str | None = None,
) -> str:
    """List a page of log groups, retention, and stored bytes. Continue with nextToken."""
    return _aws_call(
        "logs",
        "describe_log_groups",
        region=region,
        limit=_limit(limit, 50),
        **_optional(logGroupNamePrefix=name_prefix, nextToken=next_token),
    )


@mcp.tool()
def list_cloudwatch_log_streams(
    log_group: str,
    region: str = "us-east-1",
    name_prefix: str | None = None,
    limit: int = 50,
    next_token: str | None = None,
) -> str:
    """List streams, most recently active first, or by name when name_prefix is supplied.

    Last-event timestamps can lag ingestion. Continue with nextToken.
    """
    return _aws_call(
        "logs",
        "describe_log_streams",
        region=region,
        logGroupName=log_group,
        orderBy="LastEventTime" if name_prefix is None else "LogStreamName",
        descending=True,
        limit=_limit(limit, 50),
        **_optional(logStreamNamePrefix=name_prefix, nextToken=next_token),
    )


@mcp.tool()
def get_cloudwatch_log_events(
    log_group: str,
    log_stream: str,
    region: str = "us-east-1",
    start_time: int | None = None,
    end_time: int | None = None,
    start_from_head: bool = False,
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """Read one stream's events; timestamps use Unix milliseconds. Defaults to latest events.

    Pass nextBackwardToken as next_token to go backwards, or nextForwardToken
    with start_from_head=true to go forwards. Pagination ends when the returned
    directional token equals the input token, even on empty pages. Set end_time
    for a fixed window on active streams.
    """
    return _aws_call(
        "logs",
        "get_log_events",
        region=region,
        logGroupName=log_group,
        logStreamName=log_stream,
        startFromHead=start_from_head,
        limit=_limit(limit, 1000),
        **_time_range(start_time, end_time),
        **_optional(nextToken=next_token),
    )


@mcp.tool()
def filter_cloudwatch_log_events(
    log_group: str,
    filter_pattern: str = "",
    region: str = "us-east-1",
    start_time: int | None = None,
    end_time: int | None = None,
    log_stream_prefix: str | None = None,
    limit: int = 100,
    next_token: str | None = None,
) -> str:
    """Search a group using a CloudWatch filter pattern (not Logs Insights SQL).

    Timestamps use Unix milliseconds. Narrow time/stream scope to limit scans.
    Follow nextToken until absent, including on empty pages. Returns original
    events with sensitive fields masked according to AWS data protection policy.
    """
    return _aws_call(
        "logs",
        "filter_log_events",
        region=region,
        logGroupName=log_group,
        filterPattern=filter_pattern,
        limit=_limit(limit, 1000),
        **_time_range(start_time, end_time),
        **_optional(logStreamNamePrefix=log_stream_prefix, nextToken=next_token),
    )


@mcp.tool()
def get_cost_last_30_days(next_page_token: str | None = None) -> str:
    """Get unblended cost for the last 30 UTC days, grouped by service. Excludes today.

    Pass NextPageToken as next_page_token to continue.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)
    return _aws_call(
        "ce",
        "get_cost_and_usage",
        region="us-east-1",
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        **_optional(NextPageToken=next_page_token),
    )


@mcp.tool()
def describe_iam_user(user_name: str) -> str:
    """Get details for a single IAM user (read-only)."""
    return _aws_call("iam", "get_user", UserName=user_name)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
