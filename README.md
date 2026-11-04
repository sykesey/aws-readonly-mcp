# aws-readonly-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for
inspecting AWS accounts. Browse ECS resources, CloudWatch Logs, S3 buckets and objects,
EC2 instances, IAM users, and costs through **25 tools**.

## Read-only boundary

Every AWS request goes through `_aws_call`, which checks an explicit allow-list of
`(service, operation)` pairs **before creating a client**. A name beginning with `get`,
`list`, or `describe` is not sufficient: credential issuance and unreviewed operations
remain blocked. `head_object` and `filter_log_events` are explicitly permitted reads.
There is no generic AWS execution tool.

Use a profile with read-only IAM permissions scoped to the resources you intend to inspect.
The server enforces which operations can run; IAM enforces which resources they can access.
No role assumption or credential issuance is exposed as a tool. The standard SDK credential
chain may still refresh credentials or assume the role configured in your AWS profile.

## Available tools

Regional tools accept `region`, defaulting to `us-east-1`. Account-wide S3 bucket listing,
bucket location discovery, and IAM use the SDK's configured region. Cost Explorer uses
`us-east-1`. Tools return JSON strings with AWS response metadata removed; AWS errors are
returned with the service, operation, and error detail. Invalid limits, timestamps, and
batch sizes are rejected before AWS is contacted.

### ECS

| Tool | Inspection |
|------|------------|
| `list_ecs_clusters` | Cluster ARNs |
| `describe_ecs_clusters` | Counts, capacity providers, configuration, statistics, and tags; up to 100 clusters |
| `list_ecs_services` | Service ARNs in a cluster |
| `describe_ecs_services` | Desired/running counts, deployments, recent events, networking, and tags; up to 10 services |
| `list_ecs_tasks` | Task ARNs, optionally by service, family, or RUNNING/STOPPED desired status |
| `describe_ecs_tasks` | Container status, exit codes, stopped reasons, ENIs, and tags; up to 100 tasks |
| `list_ecs_task_definitions` | Definition ARNs, descending order, optionally by family prefix and status |
| `describe_ecs_task_definition` | Images, CPU/memory, roles, environment, and log configuration |
| `list_ecs_container_instances` | Container instance ARNs for EC2-backed capacity, optionally by status |
| `describe_ecs_container_instances` | EC2 IDs, resources, agent state, health, and tags; up to 100 instances |

Pass cluster/resource names, IDs, or ARNs as indicated in each tool's schema. ECS describe
responses preserve both successful results and `failures`; check both. Fargate tasks do
not appear in container instance listings.

For an investigation, discover the cluster and service, inspect tasks (including `STOPPED`
when relevant), then read the task definition's `logConfiguration`. Its `awslogs-group`,
`awslogs-region`, and `awslogs-stream-prefix` options lead to the CloudWatch tools below.
No ECS Exec, task launch/stop, or deployment actions are available.

### CloudWatch Logs

| Tool | Inspection |
|------|------------|
| `list_cloudwatch_log_groups` | Groups, retention, and stored bytes; optional name prefix |
| `list_cloudwatch_log_streams` | Streams ordered by recent activity, or by name when a prefix is supplied |
| `get_cloudwatch_log_events` | Events from one stream, latest by default, with optional time bounds |
| `filter_cloudwatch_log_events` | Search across a group's streams with an optional stream prefix and time bounds |

Log timestamps are **Unix milliseconds**, not seconds. Event page limits default to 100
and are capped at 1,000; group/stream page limits default to and are capped at 50. A page
limit bounds returned events, not the amount of data searched: specify a time range and
stream prefix when searching large groups. Last-event timestamps can lag ingestion.

Filtering uses [CloudWatch filter patterns](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html),
for example `ERROR` or `{ $.level = "ERROR" }`. Logs Insights queries are not exposed.
Log reads return original events and do not request unmasking of protected fields.

### S3

| Tool | Inspection |
|------|------------|
| `list_s3_buckets` | One page of general purpose buckets |
| `get_s3_bucket_location` | Bucket region (`null` means `us-east-1`; legacy `EU` means `eu-west-1`) |
| `get_s3_bucket_versioning` | Versioning and MFA delete settings |
| `get_s3_bucket_encryption` | Default server-side encryption configuration |
| `get_s3_public_access_block` | Bucket-level Block Public Access settings |
| `list_s3_objects` | Object keys, sizes, dates, and optional folder-like prefixes |
| `head_s3_object` | Size, type, ETag, encryption, and custom metadata without the body |
| `get_s3_object` | Bounded content preview plus metadata |

Discover a bucket's region first, then use it for that bucket's tools. Set `prefix` to
narrow object listings and `delimiter="/"` to browse folder-like `CommonPrefixes`.
Versioning with no `Status` means it has never been enabled. Bucket-level Block Public
Access alone does not establish effective public access; account settings and policies
also matter. Missing configuration and access-denied errors remain visible.

`get_s3_object` reads the object's beginning: 64 KiB by default, with `max_bytes` capped
at 256 KiB. It returns `Content`, `Encoding` (`utf-8` or `base64`), `BytesRead`, and
`Truncated`, and always closes the streaming body. A preview ending within a UTF-8
character uses base64 to preserve the exact bytes. The reader consumes at most
`max_bytes + 1` bytes to detect truncation; it does not decompress or interpret files.
`head_s3_object` and `get_s3_object` accept an optional `version_id`.

S3 content, log messages, and plaintext task-definition environment values are returned
to the MCP client. Secret references in task definitions are returned as configured;
the server does not fetch the referenced secret values. Content access needs the
corresponding IAM permissions (`s3:GetObject`, or `s3:GetObjectVersion` for a specified
version); encrypted objects may also need `kms:Decrypt`. Archived objects must already
be readable; this server never initiates restoration. S3 request/retrieval/data transfer
charges and normal AWS API charges still apply.

### Existing account tools

| Tool | Inspection |
|------|------------|
| `list_ec2_instances` | Instance ID, state, type, AZ, and tags |
| `get_cost_last_30_days` | Unblended cost for the last 30 completed UTC days, grouped by service |
| `describe_iam_user` | Details for a named IAM user |

**EC2 response change:** `list_ec2_instances` now returns an object containing `Instances`
and optional `NextToken`, instead of a bare array or a no-results string. Empty pages
contain `Instances: []`. Its page size defaults to 100 and accepts 5..1,000.

## Pagination

All list/search tools return **one AWS page per call**, not the full account inventory.
Most lists default to 100 items. ECS pages are capped at 100; S3 pages at 1,000.
Repeat the call with the same filters/region and the appropriate token:

| Tools | Response field | Next request parameter | Finished when |
|-------|----------------|------------------------|---------------|
| ECS lists, log group/stream lists, log filtering | `nextToken` | `next_token` | Token absent |
| EC2 instances | `NextToken` | `next_token` | Token absent |
| S3 buckets | `ContinuationToken` | `continuation_token` | Token absent |
| S3 objects | `NextContinuationToken` | `continuation_token` | `IsTruncated` is false |
| Cost Explorer | `NextPageToken` | `next_page_token` | Token absent |
| Stream events, forward | `nextForwardToken` | `next_token`, with `start_from_head=true` | Token equals the input token |
| Stream events, backward | `nextBackwardToken` | `next_token`, with `start_from_head=false` | Token equals the input token |

Empty log pages can still have continuation tokens. Continue until the appropriate end
condition, not merely until a page is empty. For forward reading on active streams, set
`end_time` to keep the window fixed. These rules follow the AWS documentation for
[GetLogEvents](https://docs.aws.amazon.com/boto3/latest/reference/services/logs/client/get_log_events.html)
and [FilterLogEvents](https://docs.aws.amazon.com/boto3/latest/reference/services/logs/client/filter_log_events.html).

## Quick start

Prerequisites: Python 3.10+ and AWS credentials available through the standard SDK chain
(environment, shared config, or `AWS_PROFILE`). The AWS CLI is optional.

```bash
uv sync
export AWS_PROFILE=my-readonly-profile
aws login --profile $AWS_PROFILE
aws sts get-caller-identity  # optional identity check
uv run aws-readonly-mcp
```

The MCP SDK is pinned to the 1.x API used by this server's FastMCP implementation.

### Connect to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` or `~/.claude.json` on macOS:

```json
{
  "mcpServers": {
    "aws-readonly": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/aws-readonly-mcp", "run", "aws-readonly-mcp"],
      "env": { "AWS_PROFILE": "my-readonly-profile" }
    }
  }
}
```

Restart the MCP connection after updating so the client discovers the expanded tool list.

Example prompts:

- "Inspect ECS services in ap-southeast-2 and explain any failed deployments or stopped tasks."
- "Find ERROR messages in /ecs/my-app between these timestamps."
- "List objects under reports/ in this bucket, inspect the metadata, and preview the latest JSON file."
- "Which services drove my AWS bill over the last 30 days?"

## Development

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests use explicit dummy credentials and botocore Stubber; they never require live AWS
access. They validate requests for all 25 MCP tools, continuation behavior, partial ECS
failures, bounded/encoded S3 reads and stream cleanup, errors, and rejection of all
unlisted operations in the supported services' SDK models. Live IAM/resource access
must be verified separately in the target account.

To add a tool, review the AWS operation's semantics, add the exact service/operation
pair to `READONLY_OPERATIONS`, route the call through `_aws_call`, and cover it with an
offline API-model test. Keep list operations paginated and reads bounded where possible.

## License

MIT — see [LICENSE](LICENSE).
