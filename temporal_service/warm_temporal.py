"""Pre-warms the embedded Temporal CLI binary during Docker image build."""

import asyncio

from temporalio.testing import WorkflowEnvironment


async def main() -> None:
    """Starts and stops the local Temporal server once to cache the binary."""
    env = await WorkflowEnvironment.start_local()
    await env.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
