"""Exercise real stdio MCP startup, discovery, validation, and a stubbed AWS read."""

import asyncio
import json
import sys
from datetime import timedelta
from textwrap import dedent

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_discovery_and_tool_calls():
    # A separate server process uses a real AWS client with dummy credentials,
    # guarded by Stubber so no AWS request can leave the process.
    script = dedent("""
        import boto3
        from botocore.stub import Stubber
        from aws_readonly_mcp import server

        client = boto3.Session().client(
            "ecs", region_name="ap-southeast-2",
            aws_access_key_id="testing", aws_secret_access_key="testing",
        )
        stubber = Stubber(client)
        stubber.add_response("list_clusters", {
            "clusterArns": ["arn:aws:ecs:ap-southeast-2:123456789012:cluster/demo"],
            "nextToken": "more",
        }, {"maxResults": 10})
        def factory(service, *, region_name=None, config=None):
            assert service == "ecs" and region_name == "ap-southeast-2"
            return client
        server.boto3.client = factory
        with stubber:
            server.main()
            stubber.assert_no_pending_responses()
    """)

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", script])
        async with (
            stdio_client(params) as (read, write),
            ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=20)
            ) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 25
            names = {tool.name for tool in tools.tools}
            assert {
                "describe_ecs_tasks",
                "filter_cloudwatch_log_events",
                "get_s3_object",
            } <= names
            invalid = await session.call_tool(
                "get_s3_object",
                {
                    "bucket": "example-bucket",
                    "key": "file.txt",
                    "max_bytes": 0,
                },
            )
            assert invalid.isError
            assert "max_bytes must be between" in invalid.content[0].text
            result = await session.call_tool(
                "list_ecs_clusters",
                {
                    "region": "ap-southeast-2",
                    "limit": 10,
                },
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)
            assert payload["nextToken"] == "more"
            assert payload["clusterArns"][0].endswith("cluster/demo")

    asyncio.run(exercise())
